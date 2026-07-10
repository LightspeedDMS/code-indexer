"""Story #1293 S1b [A1]: activated-parallel dual-embed correlation propagation.

_search_single_repository's "parallel" strategy already wraps each provider
dispatch in contextvars.copy_context() (semantic_query_manager.py) before
submitting to the ThreadPoolExecutor -- this is the SAME pattern Story #1293
S1b [A2] added to multi_search_service.py. This test proves that existing
wiring correctly propagates the request's correlation_id ContextVar into
BOTH the voyage-ai and cohere legs of _search_with_provider, so each leg's
eventual search_embed_event row (emitted deep inside search_service.py /
filesystem_vector_store.py) carries the REAL correlation_id -- not a UUID
fallback -- and that exactly one call is made per provider (no redundant
re-embed, no phantom hit).
"""

import contextvars
import shutil
import tempfile
import threading
from unittest.mock import patch

from code_indexer.server.query.semantic_query_manager import SemanticQueryManager


_probe_var: "contextvars.ContextVar" = contextvars.ContextVar(
    "test_a1_probe_var", default=None
)


def _make_manager() -> SemanticQueryManager:
    import logging
    from unittest.mock import MagicMock

    manager = SemanticQueryManager.__new__(SemanticQueryManager)
    manager.data_dir = "/fake/data"
    manager.query_timeout_seconds = 30
    manager.max_concurrent_queries_per_user = 5
    manager.max_results_per_query = 100
    manager._active_queries_per_user = {}
    manager.logger = logging.getLogger(__name__)
    manager.activated_repo_manager = MagicMock()
    manager.background_job_manager = MagicMock()
    return manager


class TestParallelStrategyPropagatesCorrelationId:
    def setup_method(self):
        self.repo_path = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.repo_path, ignore_errors=True)

    def test_both_provider_legs_see_the_calling_threads_contextvar(self):
        manager = _make_manager()
        observed: dict = {}
        lock = threading.Lock()

        def fake_search_with_provider(*args, **kwargs):
            provider = kwargs.get("provider_name")
            with lock:
                observed[provider] = _probe_var.get()
            return []

        token = _probe_var.set("request-correlation-a1-xyz")
        try:
            with patch.object(
                manager,
                "_search_with_provider",
                side_effect=fake_search_with_provider,
            ):
                manager._search_single_repository(
                    repo_path=self.repo_path,
                    repository_alias="test-repo",
                    query_text="authentication",
                    limit=10,
                    min_score=None,
                    file_extensions=None,
                    query_strategy="parallel",
                )
        finally:
            _probe_var.reset(token)

        assert set(observed.keys()) == {"voyage-ai", "cohere"}, (
            f"expected exactly one call per provider, got {observed}"
        )
        assert observed["voyage-ai"] == "request-correlation-a1-xyz", (
            "voyage-ai leg did not see the calling thread's ContextVar "
            f"(got {observed['voyage-ai']!r}) -- correlation_id would fall "
            "back to a fresh UUID for this leg's search_embed_event row"
        )
        assert observed["cohere"] == "request-correlation-a1-xyz", (
            "cohere leg did not see the calling thread's ContextVar "
            f"(got {observed['cohere']!r}) -- correlation_id would fall "
            "back to a fresh UUID for this leg's search_embed_event row"
        )
