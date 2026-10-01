"""Story #1586 AC1: search/FTS metrics wired into the MCP search handler.

Proves the WIRING -- a real call into _execute_tracked_search /
_execute_regex_search emits a real cidx.search.*/cidx.fts.* OTEL metric via
ApplicationMetrics -- not just that ApplicationMetrics.record_search_request
works standalone (that isolation-level coverage already exists in
tests/unit/server/telemetry/test_application_metrics.py).

_execute_tracked_search: only its one non-SUT leaf dependency
(app_module.semantic_query_manager._perform_search) is patched, mirroring
the established pattern in
tests/unit/server/mcp/test_bug1219_global_repo_perform_search_unpack.py --
_execute_tracked_search itself runs for real.

_execute_regex_search: driven with a REAL RegexSearchService against a real
temp directory with real files -- real ripgrep subprocess, no mocking of
the search backend at all.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers.search import (
    _execute_tracked_search,
    _execute_regex_search,
    _record_search_metric,
    _record_fts_metric,
)
from code_indexer.server.query.semantic_query_manager import (
    QueryResult,
    SemanticQueryManager,
)
from code_indexer.server.services.config_service import (
    ConfigService,
    reset_config_service,
    set_config_service,
)
from code_indexer.server.utils.config_manager import ServerConfigManager
from code_indexer.server.telemetry.manager import (
    peek_telemetry_manager,
    reset_telemetry_manager,
)
from code_indexer.server.telemetry.metrics_instrumentation import (
    reset_application_metrics,
)
from code_indexer.services.embedding_provider import EmbeddingProvider
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

from .otel_test_support import active_application_metrics_singleton, find_metric

# Arbitrary but valid placeholder values for _record_search_metric/
# _record_fts_metric's required duration arguments below -- their exact
# magnitude is irrelevant to TestRecordMetricHelpersNeverWinTelemetryRace,
# which only asserts that the telemetry manager singleton stays uninitialized.
_PLACEHOLDER_DURATION_MS = 5
_PLACEHOLDER_DURATION_SECONDS = 0.01

# Story #1586 Finding 5: real-backend semantic search test constants.
_REAL_BACKEND_VECTOR_DIM = 8
_REAL_BACKEND_COLLECTION_NAME = "voyage-code-3"
_REAL_BACKEND_INDEXED_TEXT = (
    "def authenticate(user, password): return check(user, password)"
)
_REAL_BACKEND_INDEXED_PATH = "src/auth.py"


class _DeterministicFakeQueryEmbeddingProvider(EmbeddingProvider):
    """Real EmbeddingProvider implementation with deterministic, hash-derived
    vectors -- no network calls, no mocking of the interface under test.
    Mirrors test_search_service_multimodal_real_infra_1480.py's
    DeterministicFakeEmbeddingProvider (Story #1586 Finding 5). This class
    is built incrementally across several edits -- EmbeddingProvider
    declares 8 abstract methods that must all be implemented before this
    class is instantiable; only the first 3 are added here.
    """

    def _vector_for(self, text: str) -> List[float]:
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        rng = np.random.default_rng(int(text_hash[:8], 16))
        vec = rng.random(_REAL_BACKEND_VECTOR_DIM).astype(np.float32)
        return (vec / np.linalg.norm(vec)).tolist()  # type: ignore[no-any-return]

    def get_embedding(self, text, model=None, *, embedding_purpose=None):
        return self._vector_for(text)

    def get_embeddings_batch(
        self, texts, model=None, *, embedding_purpose=None, retry=True
    ):
        return [self._vector_for(t) for t in texts]

    def get_embedding_with_metadata(self, text, model=None, *, embedding_purpose=None):
        raise NotImplementedError("not used by this test's search path")

    def get_embeddings_batch_with_metadata(
        self, texts, model=None, *, embedding_purpose=None
    ):
        raise NotImplementedError("not used by this test's search path")

    def health_check(self, *, test_api: bool = False) -> bool:
        return True

    def get_model_info(self) -> Dict[str, Any]:
        return {"name": "voyage-code-3", "dimensions": _REAL_BACKEND_VECTOR_DIM}

    def get_provider_name(self) -> str:
        return "voyage-ai"

    def get_current_model(self) -> str:
        return "voyage-code-3"

    def supports_batch_processing(self) -> bool:
        return True


def _build_real_indexed_repo(tmp_path):
    """Build a real on-disk FilesystemVectorStore collection containing one
    real indexed chunk (Story #1586 Finding 5). Returns
    (repo_path, store, provider) -- store/provider are real objects, never
    mocked; only the search-service FACTORIES that hand them to
    SemanticSearchService are patched by the caller.
    """
    repo_path = tmp_path
    base_path = repo_path / ".code-indexer" / "index"
    store = FilesystemVectorStore(base_path=base_path, project_root=repo_path)
    provider = _DeterministicFakeQueryEmbeddingProvider()

    store.create_collection(
        _REAL_BACKEND_COLLECTION_NAME, vector_size=_REAL_BACKEND_VECTOR_DIM
    )
    store.upsert_points(
        _REAL_BACKEND_COLLECTION_NAME,
        [
            {
                "id": "pt-1",
                "vector": provider.get_embedding(_REAL_BACKEND_INDEXED_TEXT),
                "payload": {
                    "path": _REAL_BACKEND_INDEXED_PATH,
                    "content": _REAL_BACKEND_INDEXED_TEXT,
                },
            }
        ],
    )
    store.end_indexing(_REAL_BACKEND_COLLECTION_NAME)
    return repo_path, store, provider


@pytest.fixture
def uninitialized_telemetry_singletons():
    """Reset both the TelemetryManager and ApplicationMetrics singletons to
    an uninitialized state before the test, and restore that clean slate
    afterward regardless of outcome."""
    reset_telemetry_manager()
    reset_application_metrics()
    try:
        yield
    finally:
        reset_telemetry_manager()
        reset_application_metrics()


@pytest.fixture
def real_semantic_query_manager(tmp_path):
    """Real repo_path + real SemanticQueryManager wired to a real indexed
    FilesystemVectorStore collection (Story #1586 Finding 5). Only the
    search-service factories/config are patched (external collaborators);
    the vector store and SemanticQueryManager itself are never mocked.
    Yields (repo_path, manager).
    """
    from contextlib import ExitStack

    repo_path, store, provider = _build_real_indexed_repo(tmp_path)
    backend = MagicMock()
    backend.get_vector_store_client.return_value = store
    mock_cfg = MagicMock()
    mock_cfg.embedding_provider = "voyage-ai"
    # Bug #1690: codebase_dir must match repo_path -- ConfigManager
    # .load_verified_config() (which _load_repo_config now routes
    # through) verifies the resolved config.codebase_dir equals the
    # requested target directory.
    mock_cfg.codebase_dir = str(repo_path)

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "code_indexer.server.services.search_service.BackendFactory.create",
                return_value=backend,
            )
        )
        stack.enter_context(
            patch(
                "code_indexer.server.services.search_service."
                "EmbeddingProviderFactory.create",
                return_value=provider,
            )
        )
        mock_cm_cls = stack.enter_context(
            patch(
                "code_indexer.server.services.search_service.ConfigManager"
                ".create_with_backtrack"
            )
        )
        mock_cm = MagicMock()
        mock_cm.get_config.return_value = mock_cfg
        mock_cm_cls.return_value = mock_cm
        stack.enter_context(patch("code_indexer.server.app._server_hnsw_cache", None))

        manager = SemanticQueryManager(
            activated_repo_manager=MagicMock(), background_job_manager=MagicMock()
        )
        yield repo_path, manager


class TestRecordMetricHelpersNeverWinTelemetryRace:
    """Story #1586 Finding 3: _record_search_metric() and
    _record_fts_metric() must use peek_telemetry_manager() (returns None
    pre-init), never get_telemetry_manager() -- the latter fabricates and
    permanently CACHES a disabled TelemetryConfig on first call when config
    is None, poisoning telemetry server-wide if either helper fires from a
    background code path before the real startup config is loaded.
    """

    def test_record_search_metric_never_calls_get_telemetry_manager_when_uninitialized(
        self, uninitialized_telemetry_singletons
    ):
        assert peek_telemetry_manager() is None

        _record_search_metric(
            params={"search_mode": "semantic"},
            user_repos=[],
            duration_ms=_PLACEHOLDER_DURATION_MS,
            results=[],
            status="success",
        )

        assert peek_telemetry_manager() is None, (
            "_record_search_metric() must never call get_telemetry_manager() "
            "before real startup config is loaded -- doing so poisons the "
            "singleton with a disabled fallback for the rest of the process."
        )

    def test_record_fts_metric_never_calls_get_telemetry_manager_when_uninitialized(
        self, uninitialized_telemetry_singletons
    ):
        assert peek_telemetry_manager() is None

        _record_fts_metric(
            repository_alias="myrepo-global",
            duration_seconds=_PLACEHOLDER_DURATION_SECONDS,
            matches_count=0,
            status="success",
        )

        assert peek_telemetry_manager() is None, (
            "_record_fts_metric() must never call get_telemetry_manager() "
            "before real startup config is loaded -- doing so poisons the "
            "singleton with a disabled fallback for the rest of the "
            "process."
        )


def _make_user() -> User:
    user = MagicMock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    user.has_permission = MagicMock(return_value=True)
    return user


def _make_query_result(file_path: str = "src/auth.py") -> QueryResult:
    return QueryResult(
        file_path=file_path,
        line_number=1,
        code_snippet="def authenticate(): pass",
        similarity_score=0.9,
        repository_alias="myrepo-global",
        source_repo=None,
        source_provider="voyage-ai",
    )


class TestExecuteTrackedSearchRecordsSearchMetric:
    """AC1 wiring-shim unit test: _execute_tracked_search must record
    cidx.search.* on success/error. Mocks its one non-SUT leaf dependency
    (app_module.semantic_query_manager._perform_search) to isolate the
    metric-recording wiring itself -- it does NOT exercise a real search
    backend. For the genuine real-backend proof of AC1's semantic path,
    see TestExecuteTrackedSearchRealSemanticBackend below (Story #1586
    Finding 5).
    """

    def test_success_records_search_requests_counter(self, tmp_path):
        fake_results = [_make_query_result()]
        user = _make_user()
        params: Dict[str, Any] = {
            "query_text": "authenticate",
            "search_mode": "semantic",
        }
        user_repos = [
            {
                "user_alias": "myrepo-global",
                "repo_path": str(tmp_path),
                "actual_repo_id": "myrepo",
            }
        ]

        with active_application_metrics_singleton() as (_metrics, reader):
            with patch(
                "code_indexer.server.mcp.handlers._utils.app_module"
            ) as mock_app:
                mock_app.semantic_query_manager._perform_search.return_value = (
                    fake_results,
                    "primary_only",
                )
                _execute_tracked_search(params, user, user_repos, limit=10)

            requests_metric = find_metric(reader, "cidx.search.requests")
            assert requests_metric is not None, "cidx.search.requests was not emitted"
            data_points = list(requests_metric.data.data_points)
            assert len(data_points) == 1
            dp = data_points[0]
            assert dp.value == 1
            assert dp.attributes["search_type"] == "semantic"
            assert dp.attributes["repository"] == "myrepo-global"
            assert dp.attributes["status"] == "success"

            duration_metric = find_metric(reader, "cidx.search.duration")
            assert duration_metric is not None
            assert len(list(duration_metric.data.data_points)) == 1

            results_metric = find_metric(reader, "cidx.search.results_count")
            assert results_metric is not None
            results_dp = list(results_metric.data.data_points)[0]
            assert results_dp.sum == 1  # one result returned

    def test_failure_records_status_error_with_zero_results(self, tmp_path):
        user = _make_user()
        params: Dict[str, Any] = {
            "query_text": "boom",
            "search_mode": "semantic",
        }
        user_repos = [
            {
                "user_alias": "myrepo-global",
                "repo_path": str(tmp_path),
                "actual_repo_id": "myrepo",
            }
        ]

        with active_application_metrics_singleton() as (_metrics, reader):
            with patch(
                "code_indexer.server.mcp.handlers._utils.app_module"
            ) as mock_app:
                mock_app.semantic_query_manager._perform_search.side_effect = (
                    RuntimeError("provider exploded")
                )
                with pytest.raises(Exception):
                    _execute_tracked_search(params, user, user_repos, limit=10)

            requests_metric = find_metric(reader, "cidx.search.requests")
            assert requests_metric is not None
            dp = list(requests_metric.data.data_points)[0]
            assert dp.attributes["status"] == "error"

            duration_metric = find_metric(reader, "cidx.search.duration")
            assert duration_metric is not None, (
                "cidx.search.duration must still be recorded on the error path"
            )
            assert len(list(duration_metric.data.data_points)) == 1

            results_metric = find_metric(reader, "cidx.search.results_count")
            assert results_metric is not None
            results_dp = list(results_metric.data.data_points)[0]
            assert results_dp.sum == 0


@pytest.fixture
def regex_repo_env(tmp_path):
    """Real repo dir + real ConfigService (scoped to tmp_path) for
    _execute_regex_search tests. Yields (repo_path, user); tears down the
    global ConfigService singleton unconditionally.
    """
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "main.py").write_text("def handle_request():\n    pass\n")

    svc = ConfigService(config_manager=ServerConfigManager(str(tmp_path / "cfg")))
    set_config_service(svc)
    try:
        yield repo_path, _make_user()
    finally:
        reset_config_service()


class TestExecuteRegexSearchRecordsFtsMetric:
    """AC1: _execute_regex_search must record cidx.fts.* on success/error."""

    def test_success_records_fts_requests_counter(self, regex_repo_env):
        repo_path, user = regex_repo_env

        with active_application_metrics_singleton() as (_metrics, reader):
            matches, _rerank_meta, search_result = asyncio.run(
                _execute_regex_search(
                    {"pattern": "def handle_request"},
                    repo_path,
                    "test-repo-global",
                    user,
                )
            )
            assert len(matches) == 1
            assert search_result.truncated is False

            requests_metric = find_metric(reader, "cidx.fts.requests")
            assert requests_metric is not None, "cidx.fts.requests was not emitted"
            dp = list(requests_metric.data.data_points)[0]
            assert dp.value == 1
            assert dp.attributes["repository"] == "test-repo-global"
            assert dp.attributes["status"] == "success"

            matches_metric = find_metric(reader, "cidx.fts.matches")
            assert matches_metric is not None
            matches_dp = list(matches_metric.data.data_points)[0]
            assert matches_dp.sum == 1

            duration_metric = find_metric(reader, "cidx.fts.duration")
            assert duration_metric is not None
            assert len(list(duration_metric.data.data_points)) == 1

    def test_failure_records_fts_status_error(self, regex_repo_env):
        repo_path, user = regex_repo_env

        with active_application_metrics_singleton() as (_metrics, reader):
            # A non-existent subdirectory makes RegexSearchService.search()
            # raise ValueError("Path does not exist: ...") deterministically
            # -- a real error path, no mocking of the search backend.
            with pytest.raises(ValueError):
                asyncio.run(
                    _execute_regex_search(
                        {"pattern": "def handle_request", "path": "does-not-exist"},
                        repo_path,
                        "test-repo-global",
                        user,
                    )
                )

            requests_metric = find_metric(reader, "cidx.fts.requests")
            assert requests_metric is not None, "cidx.fts.requests was not emitted"
            dp = list(requests_metric.data.data_points)[0]
            assert dp.attributes["status"] == "error"

            matches_metric = find_metric(reader, "cidx.fts.matches")
            assert matches_metric is not None, (
                "cidx.fts.matches must still be recorded (0) on the error path"
            )
            matches_dp = list(matches_metric.data.data_points)[0]
            assert matches_dp.sum == 0

            duration_metric = find_metric(reader, "cidx.fts.duration")
            assert duration_metric is not None, (
                "cidx.fts.duration must still be recorded on the error path"
            )
            assert len(list(duration_metric.data.data_points)) == 1


class TestExecuteTrackedSearchRealSemanticBackend:
    """Story #1586 Finding 5: a genuine real-backend assertion for AC1's
    semantic search path -- real on-disk FilesystemVectorStore collection,
    real SemanticQueryManager, real _perform_search call (never mocked).
    """

    def test_success_returns_real_result_and_records_search_metric(
        self, real_semantic_query_manager
    ):
        repo_path, real_manager = real_semantic_query_manager
        user = _make_user()
        params: Dict[str, Any] = {
            "query_text": "authenticate",
            "search_mode": "semantic",
        }
        user_repos = [
            {
                "user_alias": "myrepo-global",
                "repo_path": str(repo_path),
                "actual_repo_id": "myrepo",
            }
        ]

        with active_application_metrics_singleton() as (_metrics, reader):
            with patch(
                "code_indexer.server.mcp.handlers._utils.app_module"
                ".semantic_query_manager",
                real_manager,
            ):
                results, _exec_ms, _timeout, _strategy = _execute_tracked_search(
                    params, user, user_repos, limit=10
                )

            result_paths = {r.file_path for r in results}
            assert _REAL_BACKEND_INDEXED_PATH in result_paths, (
                f"real indexed chunk must be returned; got paths: {result_paths}"
            )

            requests_metric = find_metric(reader, "cidx.search.requests")
            assert requests_metric is not None, "cidx.search.requests was not emitted"
            dp = list(requests_metric.data.data_points)[0]
            assert dp.attributes["status"] == "success"
