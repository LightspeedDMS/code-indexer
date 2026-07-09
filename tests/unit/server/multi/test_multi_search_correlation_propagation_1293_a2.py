"""Story #1293 S1b [A2]: MultiSearchService's per-repo fan-out must propagate
the request's correlation_id (and other ContextVars) into the worker thread
via contextvars.copy_context(), mirroring the pattern already used by
semantic_query_manager.py's "parallel" strategy dispatch. Without this, a
digest-mismatch re-embed running inside self.thread_executor loses the
request's correlation_id (ContextVars do not propagate into raw
ThreadPoolExecutor.submit() calls) and falls back to a fresh UUID -- an event
that cannot be joined back to its parent search_event_log row.
"""

import contextvars

from code_indexer.server.multi.multi_search_config import MultiSearchConfig
from code_indexer.server.multi.multi_search_service import MultiSearchService
from code_indexer.server.multi.models import MultiSearchRequest


_probe_var: "contextvars.ContextVar" = contextvars.ContextVar(
    "test_probe_var", default=None
)


def _make_service() -> MultiSearchService:
    config = MultiSearchConfig(max_workers=2, query_timeout_seconds=5)
    return MultiSearchService(config)


def test_execute_parallel_search_propagates_contextvars_into_worker():
    """The ContextVar value set in the calling thread must be visible inside
    the per-repo search_func running in the executor's worker thread."""
    service = _make_service()
    observed: list = []

    def _search_func(repo_id: str, request: MultiSearchRequest):
        observed.append(_probe_var.get())
        return []

    token = _probe_var.set("request-correlation-abc")
    try:
        request = MultiSearchRequest(
            repositories=["repo1", "repo2"],
            query="test query",
            search_type="semantic",
        )
        service._execute_parallel_search(request, _search_func)
    finally:
        _probe_var.reset(token)
        service.shutdown()

    assert len(observed) == 2
    assert all(v == "request-correlation-abc" for v in observed), (
        f"ContextVar did not propagate into worker threads: {observed}"
    )
