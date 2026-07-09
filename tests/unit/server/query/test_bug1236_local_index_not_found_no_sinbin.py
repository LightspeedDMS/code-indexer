"""Tests for Bug #1236 — local HNSW index-not-found must NOT sin-bin the embedding provider.

A missing local HNSW index is a storage-layer problem on the current node, not a
provider failure.  Before this fix, ANY exception from a provider task was recorded
as a provider failure, causing voyage-ai/cohere to be sin-binned cluster-wide.

Fix contract (tested here):
1.  `LocalIndexNotFoundError` exists in `code_indexer.storage.filesystem_vector_store`
    and is a subclass of `RuntimeError` (preserves exception hierarchy for code that
    catches RuntimeError today, but is narrower for discrimination).
2.  When `_search_with_provider` raises `LocalIndexNotFoundError`, the parallel-dispatch
    handler must NOT call `record_call(provider_name, success=False)` — provider health
    stays untouched.
3.  When `_search_with_provider` raises any other exception (RuntimeError, httpx error,
    ProviderRateLimitedError), the handler MUST call `record_call(success=False)` so
    sin-binning of a genuinely-down provider is preserved (Bug #678 regression guard).
4.  Both the normal except branch AND the timeout branch are covered.
"""

import shutil
import tempfile
import time
import logging
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.query.semantic_query_manager import (
    SemanticQueryManager,
    QueryResult,
)
from code_indexer.services.provider_health_monitor import ProviderHealthMonitor


# ---------------------------------------------------------------------------
# Helpers — shared with other sinbin tests
# ---------------------------------------------------------------------------


def _make_provider_results(
    provider: str, file_path: str, score: float
) -> List[QueryResult]:
    return [
        QueryResult(
            file_path=file_path,
            line_number=1,
            code_snippet=f"code from {provider}",
            similarity_score=score,
            repository_alias="test-repo",
            source_provider=provider,
        )
    ]


def _fast_search(*args, **kwargs) -> List[QueryResult]:
    provider = kwargs.get("provider_name", "unknown")
    return _make_provider_results(provider, f"src/{provider}.py", 0.75)


def _patch_health_monitor(monitor: ProviderHealthMonitor):
    return patch(
        "code_indexer.server.query.semantic_query_manager.ProviderHealthMonitor",
        get_instance=MagicMock(return_value=monitor),
    )


def _run_parallel_query(manager, repo_path):
    return manager._search_single_repository(
        repo_path=repo_path,
        repository_alias="test-repo",
        query_text="authentication",
        limit=10,
        min_score=None,
        file_extensions=None,
        query_strategy="parallel",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_path():
    path = tempfile.mkdtemp()
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def reset_health_monitor():
    ProviderHealthMonitor.reset_instance()
    yield
    ProviderHealthMonitor.reset_instance()


@pytest.fixture
def health_monitor():
    return ProviderHealthMonitor.get_instance()


@pytest.fixture
def manager():
    m = SemanticQueryManager.__new__(SemanticQueryManager)
    m.data_dir = "/fake/data"
    m.query_timeout_seconds = 30
    m.max_concurrent_queries_per_user = 5
    m.max_results_per_query = 100
    m._active_queries_per_user = {}
    m.logger = logging.getLogger(__name__)
    mock_arm = MagicMock()
    mock_arm.activated_repos_dir = "/fake/data/activated_repos"
    m.activated_repo_manager = mock_arm
    m.background_job_manager = MagicMock()
    return m


# ---------------------------------------------------------------------------
# TestLocalIndexNotFoundErrorType
# ---------------------------------------------------------------------------


class TestLocalIndexNotFoundErrorType:
    """LocalIndexNotFoundError must exist and satisfy the type contract."""

    def test_importable_from_filesystem_vector_store(self):
        """LocalIndexNotFoundError must be importable from the storage module."""
        from code_indexer.storage.filesystem_vector_store import LocalIndexNotFoundError

        assert issubclass(LocalIndexNotFoundError, Exception)

    def test_is_subclass_of_runtime_error(self):
        """LocalIndexNotFoundError must be a subclass of RuntimeError for backward compat."""
        from code_indexer.storage.filesystem_vector_store import LocalIndexNotFoundError

        assert issubclass(LocalIndexNotFoundError, RuntimeError)

    def test_can_be_raised_and_caught(self):
        """LocalIndexNotFoundError must be raiseable and catchable."""
        from code_indexer.storage.filesystem_vector_store import LocalIndexNotFoundError

        with pytest.raises(LocalIndexNotFoundError):
            raise LocalIndexNotFoundError(
                "HNSW index not found for collection 'main'. Run: cidx index --rebuild-index"
            )

    def test_message_preserved(self):
        """The exception message (including remediation) must survive round-trip."""
        from code_indexer.storage.filesystem_vector_store import LocalIndexNotFoundError

        msg = "HNSW index not found for collection 'main'. Run: cidx index --rebuild-index"
        exc = LocalIndexNotFoundError(msg)
        assert "HNSW index not found" in str(exc)
        assert "cidx index" in str(exc)


# ---------------------------------------------------------------------------
# TestLocalIndexErrorDoesNotSinbinProvider
# ---------------------------------------------------------------------------


class TestLocalIndexErrorDoesNotSinbinProvider:
    """When _search_with_provider raises LocalIndexNotFoundError, provider health must be untouched.

    Observable: record_call(success=False) is NOT called; failed_requests stays 0.
    """

    def test_voyage_ai_not_sinbinned_on_local_index_error(
        self, manager, repo_path, health_monitor
    ):
        """voyage-ai raising LocalIndexNotFoundError must not increment its failed_requests."""
        from code_indexer.storage.filesystem_vector_store import LocalIndexNotFoundError

        def raising_voyage(*args, **kwargs):
            if kwargs.get("provider_name") == "voyage-ai":
                raise LocalIndexNotFoundError(
                    "HNSW index not found for collection 'main'. Run: cidx index --rebuild-index"
                )
            return _fast_search(*args, **kwargs)

        with patch.object(manager, "_search_with_provider", side_effect=raising_voyage):
            with _patch_health_monitor(health_monitor):
                _run_parallel_query(manager, repo_path)

        status = health_monitor.get_health("voyage-ai").get("voyage-ai")
        # Provider health must be untouched — no failed_requests recorded
        if status is not None:
            assert status.failed_requests == 0, (
                f"voyage-ai must NOT have failed_requests incremented for a local index error, "
                f"got failed_requests={status.failed_requests}"
            )

    def test_cohere_not_sinbinned_on_local_index_error(
        self, manager, repo_path, health_monitor
    ):
        """cohere raising LocalIndexNotFoundError must not increment its failed_requests."""
        from code_indexer.storage.filesystem_vector_store import LocalIndexNotFoundError

        def raising_cohere(*args, **kwargs):
            if kwargs.get("provider_name") == "cohere":
                raise LocalIndexNotFoundError(
                    "HNSW index not found for collection 'cohere'. Run: cidx index --rebuild-index"
                )
            return _fast_search(*args, **kwargs)

        with patch.object(manager, "_search_with_provider", side_effect=raising_cohere):
            with _patch_health_monitor(health_monitor):
                _run_parallel_query(manager, repo_path)

        status = health_monitor.get_health("cohere").get("cohere")
        if status is not None:
            assert status.failed_requests == 0, (
                f"cohere must NOT have failed_requests incremented for a local index error, "
                f"got failed_requests={status.failed_requests}"
            )

    def test_provider_not_sinbinned_after_local_index_error(
        self, manager, repo_path, health_monitor
    ):
        """After a LocalIndexNotFoundError, voyage-ai must NOT be sin-binned."""
        from code_indexer.storage.filesystem_vector_store import LocalIndexNotFoundError

        def raising_voyage(*args, **kwargs):
            if kwargs.get("provider_name") == "voyage-ai":
                raise LocalIndexNotFoundError("HNSW index not found")
            return _fast_search(*args, **kwargs)

        with patch.object(manager, "_search_with_provider", side_effect=raising_voyage):
            with _patch_health_monitor(health_monitor):
                _run_parallel_query(manager, repo_path)

        assert not health_monitor.is_sinbinned("voyage-ai"), (
            "voyage-ai must NOT be sin-binned after a local-index-not-found error"
        )


# ---------------------------------------------------------------------------
# TestGenuineProviderErrorStillSinbins (regression guard for Bug #678)
# ---------------------------------------------------------------------------


class TestGenuineProviderErrorStillSinbins:
    """Generic RuntimeError / network errors MUST still record failure (Bug #678 regression)."""

    def test_generic_runtime_error_records_failure(
        self, manager, repo_path, health_monitor
    ):
        """A plain RuntimeError from voyage-ai must record failed_requests >= 1."""

        def raising_voyage(*args, **kwargs):
            if kwargs.get("provider_name") == "voyage-ai":
                raise RuntimeError("voyage-ai API unavailable")
            return _fast_search(*args, **kwargs)

        with patch.object(manager, "_search_with_provider", side_effect=raising_voyage):
            with _patch_health_monitor(health_monitor):
                _run_parallel_query(manager, repo_path)

        status = health_monitor.get_health("voyage-ai").get("voyage-ai")
        assert status is not None, (
            "Health status must exist for voyage-ai after generic failure"
        )
        assert status.failed_requests >= 1, (
            f"Generic RuntimeError must record failure; got failed_requests={status.failed_requests}"
        )

    def test_provider_rate_limited_error_records_failure(
        self, manager, repo_path, health_monitor
    ):
        """ProviderRateLimitedError from cohere must record failed_requests >= 1."""
        from code_indexer.services.provider_backoff import ProviderRateLimitedError

        def raising_cohere(*args, **kwargs):
            if kwargs.get("provider_name") == "cohere":
                raise ProviderRateLimitedError("cohere rate limited")
            return _fast_search(*args, **kwargs)

        with patch.object(manager, "_search_with_provider", side_effect=raising_cohere):
            with _patch_health_monitor(health_monitor):
                _run_parallel_query(manager, repo_path)

        status = health_monitor.get_health("cohere").get("cohere")
        assert status is not None, (
            "Health status must exist for cohere after rate-limit"
        )
        assert status.failed_requests >= 1, (
            f"ProviderRateLimitedError must record failure; got failed_requests={status.failed_requests}"
        )


# ---------------------------------------------------------------------------
# TestTimeoutBranchLocalIndexError
# ---------------------------------------------------------------------------


class TestTimeoutBranchLocalIndexError:
    """The timeout branch must also discriminate local-index errors from provider errors.

    Scenario: the provider task eventually raises LocalIndexNotFoundError but takes
    longer than the parallel timeout — the timeout branch fires first.  In the
    timeout branch, timed-out futures are recorded as provider failures (Bug #678).
    A timeout is genuinely ambiguous (we don't know the root cause), so the timeout
    branch records failure regardless of the exception that would have been raised.
    This test documents and guards the CURRENT behavior: timeout IS recorded as a
    failure (the exception was not yet raised when the timeout fired, so discrimination
    is impossible in that branch).

    The key regression guard from Bug #1236 is the NON-timeout except branch above.
    """

    def test_timeout_branch_records_failure(self, manager, repo_path, health_monitor):
        """A timed-out provider IS recorded as failed (timeout = ambiguous cause)."""
        from code_indexer.server.utils.config_manager import QueryOrchestrationConfig

        config_timeout = 0.1  # very short so we reliably time out

        def slow_voyage(*args, **kwargs):
            if kwargs.get("provider_name") == "voyage-ai":
                time.sleep(1.0)  # longer than config_timeout
            return _fast_search(*args, **kwargs)

        orch_cfg = QueryOrchestrationConfig(
            parallel_query_orchestrator_timeout_seconds=config_timeout,
            max_query_latency_budget_seconds=60,
            all_providers_sinbinned_retry_limit=2,
        )
        mock_config = MagicMock()
        mock_config.query_orchestration = orch_cfg
        mock_svc = MagicMock()
        mock_svc.get_config.return_value = mock_config

        with patch.object(manager, "_search_with_provider", side_effect=slow_voyage):
            with _patch_health_monitor(health_monitor):
                with patch(
                    "code_indexer.server.services.config_service.get_config_service",
                    return_value=mock_svc,
                ):
                    _run_parallel_query(manager, repo_path)

        # voyage-ai timed out — failure IS recorded (timeout is ambiguous)
        status = health_monitor.get_health("voyage-ai").get("voyage-ai")
        assert status is not None, "Health status must exist for timed-out voyage-ai"
        assert status.failed_requests >= 1, (
            "Timeout must be recorded as a failure (cause is ambiguous at timeout)"
        )


# ---------------------------------------------------------------------------
# TestCorruptIndexNotSinbinProvider  (GAP A fix — Bug #1236)
# ---------------------------------------------------------------------------


class TestCorruptIndexNotSinbinProvider:
    """A corrupt-but-present HNSW index is a LOCAL storage failure.

    hnswlib raises RuntimeError("Index seems to be corrupted or unsupported")
    when it opens a truncated or garbage .bin file.  Before the GAP A fix,
    that RuntimeError propagated straight to the parallel-dispatch handler,
    which reclassified it as a provider failure and called
    record_call(success=False), sin-binning the embedding provider.

    After the fix, filesystem_vector_store.search() catches the corrupt-index
    RuntimeError on the load_index() path and re-raises it as
    LocalIndexNotFoundError so the dispatch guard skips sin-binning.
    """

    def test_corrupt_index_error_not_sinbinned_voyage(
        self, manager, repo_path, health_monitor
    ):
        """voyage-ai raising corrupt-index RuntimeError must not sin-bin the provider."""
        from code_indexer.storage.filesystem_vector_store import LocalIndexNotFoundError
        from code_indexer.storage.hnsw_index_manager import _is_corrupt_index_error

        # Simulate the error that hnswlib raises on a corrupt .bin
        corrupt_exc = RuntimeError("Index seems to be corrupted or unsupported")
        assert _is_corrupt_index_error(corrupt_exc), (
            "Precondition: _is_corrupt_index_error must recognise this error"
        )

        # The search() method should reclassify this as LocalIndexNotFoundError
        # before it reaches the dispatch handler.  We simulate that by injecting
        # a LocalIndexNotFoundError (the post-fix shape) directly — but we also
        # add a separate test that verifies the search() path itself does the
        # reclassification (test_search_reclassifies_corrupt_index_as_local below).
        def raising_voyage(*args, **kwargs):
            if kwargs.get("provider_name") == "voyage-ai":
                raise LocalIndexNotFoundError(
                    "HNSW index is corrupt for collection 'main'. "
                    "Run: cidx index --rebuild-index"
                )
            return _fast_search(*args, **kwargs)

        with patch.object(manager, "_search_with_provider", side_effect=raising_voyage):
            with _patch_health_monitor(health_monitor):
                _run_parallel_query(manager, repo_path)

        status = health_monitor.get_health("voyage-ai").get("voyage-ai")
        # Either no record at all OR zero failures (provider health untouched)
        if status is not None:
            assert status.failed_requests == 0, (
                f"Corrupt index must NOT sin-bin voyage-ai; "
                f"got failed_requests={status.failed_requests}"
            )

    def test_search_reclassifies_corrupt_index_as_local(self, tmp_path):
        """filesystem_vector_store.search() must raise LocalIndexNotFoundError for corrupt .bin.

        This is the REAL production path test: we exercise FilesystemVectorStore.search()
        with a corrupt hnsw_index.bin on disk and confirm the exception is reclassified
        as LocalIndexNotFoundError (not a bare RuntimeError).
        """
        import json
        from code_indexer.storage.filesystem_vector_store import (
            FilesystemVectorStore,
            LocalIndexNotFoundError,
        )
        from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

        # Build a minimal collection: collection_meta.json + corrupt hnsw_index.bin
        # base_path is the index directory; collections live directly under it
        index_dir = tmp_path / ".code-indexer" / "index"
        coll_path = index_dir / "main"
        coll_path.mkdir(parents=True)
        (coll_path / "collection_meta.json").write_text(
            json.dumps(
                {
                    "collection_name": "main",
                    "vector_size": 4,
                    "hnsw_indexed_count": 1,
                }
            )
        )
        # A corrupt binary (not a valid hnswlib index)
        (coll_path / HNSWIndexManager.INDEX_FILENAME).write_bytes(b"\x00\xff\xde\xad")

        store = FilesystemVectorStore(base_path=index_dir)

        fake_provider = MagicMock()
        fake_provider.generate_embedding.return_value = [0.1, 0.2, 0.3, 0.4]
        fake_provider.get_provider_name.return_value = "voyage-ai"

        with pytest.raises(LocalIndexNotFoundError):
            store.search(
                collection_name="main",
                query="test",
                embedding_provider=fake_provider,
                limit=5,
            )


# ---------------------------------------------------------------------------
# TestMissingCollectionMetaNotSinbinProvider  (GAP A fix — Bug #1236)
# ---------------------------------------------------------------------------


class TestMissingCollectionMetaNotSinbinProvider:
    """A missing collection_meta.json is a LOCAL storage failure.

    FilesystemVectorStore.search() opens collection_meta.json just after
    collection_exists() returns True (the check only looks for the directory).
    If the file is absent (e.g. half-written clone, NFS hiccup), open() raises
    FileNotFoundError.  Before the GAP A fix, that propagated to the dispatch
    handler as a generic exception and sin-binned the provider.

    After the fix, search() catches the FileNotFoundError on that local read
    and re-raises it as LocalIndexNotFoundError.
    """

    def test_missing_collection_meta_not_sinbinned_voyage(
        self, manager, repo_path, health_monitor
    ):
        """voyage-ai raising LocalIndexNotFoundError (from missing meta) must not sin-bin."""
        from code_indexer.storage.filesystem_vector_store import LocalIndexNotFoundError

        def raising_voyage(*args, **kwargs):
            if kwargs.get("provider_name") == "voyage-ai":
                raise LocalIndexNotFoundError(
                    "collection_meta.json missing for collection 'main'. "
                    "Run: cidx index --rebuild-index"
                )
            return _fast_search(*args, **kwargs)

        with patch.object(manager, "_search_with_provider", side_effect=raising_voyage):
            with _patch_health_monitor(health_monitor):
                _run_parallel_query(manager, repo_path)

        status = health_monitor.get_health("voyage-ai").get("voyage-ai")
        if status is not None:
            assert status.failed_requests == 0, (
                f"Missing collection_meta.json must NOT sin-bin voyage-ai; "
                f"got failed_requests={status.failed_requests}"
            )

    def test_search_reclassifies_missing_meta_as_local(self, tmp_path):
        """filesystem_vector_store.search() must raise LocalIndexNotFoundError when
        collection_meta.json is missing at the point search() opens it.

        collection_exists() reads the file, so to simulate the TOCTOU race where
        the file disappears between the exists-check and the open(), we patch
        collection_exists() to return True while keeping the file absent on disk.
        That exercises the open() call in search() which raises FileNotFoundError —
        the fix must catch that and re-raise as LocalIndexNotFoundError.
        """
        from code_indexer.storage.filesystem_vector_store import (
            FilesystemVectorStore,
            LocalIndexNotFoundError,
        )

        index_dir = tmp_path / ".code-indexer" / "index"
        index_dir.mkdir(parents=True)

        store = FilesystemVectorStore(base_path=index_dir)

        fake_provider = MagicMock()
        fake_provider.generate_embedding.return_value = [0.1, 0.2, 0.3, 0.4]
        fake_provider.get_provider_name.return_value = "voyage-ai"

        # Patch collection_exists() so search() proceeds past the early-return guard
        # even though collection_meta.json does not exist on disk.  This simulates
        # the TOCTOU race: the file existed when the check ran but is gone by the
        # time search() calls open().
        with patch.object(store, "collection_exists", return_value=True):
            with pytest.raises(LocalIndexNotFoundError):
                store.search(
                    collection_name="main",
                    query="test",
                    embedding_provider=fake_provider,
                    limit=5,
                )
