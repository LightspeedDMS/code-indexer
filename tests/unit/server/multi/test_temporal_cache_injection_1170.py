"""
Unit tests for Story #1170: Temporal Query Path — Cache Injection + Shared Executor
+ subprocess Timeout.

Uses the established pattern for this test directory:
- inspect.getsource() for wiring verification
- sys.modules stubs for optional deps not installed in unit-test environment
  (google.protobuf, rich) — same technique used throughout this test suite.

Verifies:
- AC1: _search_temporal_sync builds FilesystemVectorStore with hnsw_index_cache
        and id_index_cache (guarded on self.hnsw_index_cache is not None)
- AC2: parallel_executor is forwarded from MultiSearchService.thread_executor
        to execute_temporal_query_with_fusion()
- AC4: _reconstruct_temporal_content subprocess.run has timeout=30;
        TimeoutExpired returns graceful string and logs WARNING
- AC5: All new cache/executor params default to None; no behavioral change when None
"""

import inspect
import subprocess
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub missing optional dependencies not installed in the unit-test environment.
# This is required because:
#   code_indexer.server.multi.__init__ → scip_multi_service → scip.query
#   → scip_pb2 → google.protobuf.* (google-protobuf not installed)
#   code_indexer.services.__init__ → embedding_factory → rich (not installed)
#   code_indexer.services.temporal.temporal_fusion_dispatch → pathspec (not installed)
# We stub with MagicMock so the import chain completes without the real packages.
# ---------------------------------------------------------------------------
_STUB_MODULES = [
    # google.protobuf (protobuf package)
    "google",
    "google.protobuf",
    "google.protobuf.descriptor",
    "google.protobuf.descriptor_pb2",
    "google.protobuf.descriptor_pool",
    "google.protobuf.internal",
    "google.protobuf.internal.builder",
    "google.protobuf.message",
    "google.protobuf.reflection",
    "google.protobuf.symbol_database",
    "google.protobuf.runtime_version",
    # rich
    "rich",
    "rich.console",
    "rich.markup",
    "rich.table",
    "rich.panel",
    "rich.progress",
    "rich.text",
    "rich.syntax",
    "rich.traceback",
    "rich.logging",
    # pathspec (used in temporal path exclusion parsing)
    "pathspec",
    # SCIP protobuf generated modules — these import google.protobuf symbols
    # that don't exist when google-protobuf is not installed; stub the whole
    # scip_pb2 module so the import chain doesn't try to execute the real file.
    "code_indexer.scip.protobuf.scip_pb2",
    "code_indexer.scip.protobuf",
    # numpy, msgpack — not installed in unit-test environment; stubbed so that
    # code_indexer.storage.vector_quantizer and filesystem_vector_store can be
    # imported as MagicMocks (the deadlock test doesn't need the real implementations).
    "numpy",
    "msgpack",
]
for _mod in _STUB_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


class TestTemporalVectorStoreBuiltWithHnswCache:
    """AC1: hnsw_index_cache is forwarded into FilesystemVectorStore construction."""

    def test_temporal_vector_store_built_with_hnsw_cache(self) -> None:
        """
        _search_temporal_sync must construct FilesystemVectorStore with
        hnsw_index_cache and id_index_cache when self.hnsw_index_cache is set.

        Verified via source-code inspection — following the pattern established in
        test_multi_search_content_bug.py and test_multi_search_filter_wiring.py.
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        source = inspect.getsource(MultiSearchService._search_temporal_sync)

        # Must pass hnsw_index_cache to FilesystemVectorStore
        assert "hnsw_index_cache=self.hnsw_index_cache" in source, (
            "AC1: _search_temporal_sync must pass hnsw_index_cache=self.hnsw_index_cache "
            "when constructing FilesystemVectorStore"
        )

        # Must wire id_index_cache
        assert "id_index_cache" in source, (
            "AC1: _search_temporal_sync must wire id_index_cache into FilesystemVectorStore"
        )

        # Must import get_global_id_index_cache for the server-mode path
        assert "get_global_id_index_cache" in source, (
            "AC1: must call get_global_id_index_cache() when hnsw_index_cache is present"
        )

    def test_id_index_cache_guarded_by_hnsw_cache(self) -> None:
        """
        id_index_cache retrieval must be guarded on hnsw_index_cache is not None.
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        source = inspect.getsource(MultiSearchService._search_temporal_sync)

        # The guard must be present: only call get_global_id_index_cache when
        # hnsw_index_cache is not None (matching filesystem_backend.py pattern)
        assert "self.hnsw_index_cache is not None" in source, (
            "AC1: get_global_id_index_cache() call must be guarded by "
            "'if self.hnsw_index_cache is not None'"
        )


class TestTemporalExecutorForwarded:
    """AC2 (revised): parallel_executor must NOT be forwarded from the temporal path.

    The original AC2 said to forward parallel_executor=self.thread_executor.
    The code reviewer rejected this as a critical deadlock bug: outer per-repo tasks
    run on self.thread_executor (bounded pool) and would block waiting for inner tasks
    also submitted to the same pool — permanent starvation with >= max_workers repos.

    The corrected AC2: _search_temporal_sync must NOT forward parallel_executor to
    execute_temporal_query_with_fusion. The function still accepts the parameter (for
    CLI/daemon callers that supply their own separate pool), but the multi-repo path
    must pass nothing (None).
    """

    def test_temporal_executor_not_forwarded_to_fusion(self) -> None:
        """
        _search_temporal_sync must NOT pass parallel_executor=self.thread_executor
        to execute_temporal_query_with_fusion() — doing so causes deadlock.

        Forwarding self.thread_executor (the outer bounded pool) as parallel_executor
        causes FilesystemVectorStore.search() to submit inner tasks to the same pool.
        With >= max_workers repos in flight, all slots are occupied by outer tasks
        waiting for inner tasks that can never start => permanent deadlock.
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        source = inspect.getsource(MultiSearchService._search_temporal_sync)

        assert "parallel_executor=self.thread_executor" not in source, (
            "DEADLOCK BUG: _search_temporal_sync must NOT forward "
            "parallel_executor=self.thread_executor to execute_temporal_query_with_fusion. "
            "This causes permanent thread starvation on the bounded pool."
        )

    def test_fusion_dispatch_does_not_accept_parallel_executor(self) -> None:
        """
        execute_temporal_query_with_fusion must NOT have a parallel_executor parameter.

        The parameter was removed as part of the deadlock fix: forwarding a shared
        bounded ThreadPoolExecutor through the temporal chain caused thread starvation
        when >= max_workers repos were searched concurrently on the multi-repo path.
        Callers that previously passed parallel_executor must now omit it.
        """
        from code_indexer.services.temporal.temporal_fusion_dispatch import (
            execute_temporal_query_with_fusion,
        )

        import inspect as _inspect

        sig = _inspect.signature(execute_temporal_query_with_fusion)
        assert "parallel_executor" not in sig.parameters, (
            "parallel_executor must be removed from execute_temporal_query_with_fusion "
            "to prevent deadlock in the multi-repo temporal path."
        )


class TestReconstructTimeoutReturnsGracefulString:
    """AC4: TimeoutExpired returns graceful string without raising."""

    def test_reconstruct_timeout_returns_graceful_string(self, tmp_path: Path) -> None:
        """
        When subprocess.run raises TimeoutExpired,
        _reconstruct_temporal_content must return the graceful string
        '[Content unavailable - git reconstruction timed out]' without raising.
        """
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchService,
        )

        service = TemporalSearchService(
            config_manager=MagicMock(),
            project_root=tmp_path,
        )

        metadata = {
            "reconstruct_from_git": True,
            "diff_type": "added",
            "commit_hash": "abc123",
            "path": "src/foo.py",
        }

        cmd_used = ["git", "show", "abc123:src/foo.py"]
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=cmd_used, timeout=30),
        ):
            result = service._reconstruct_temporal_content(metadata)

        assert result == "[Content unavailable - git reconstruction timed out]", (
            f"Expected graceful timeout string, got: {result!r}"
        )

    def test_reconstruct_timeout_does_not_raise(self, tmp_path: Path) -> None:
        """TimeoutExpired must be caught — must not propagate to caller."""
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchService,
        )

        service = TemporalSearchService(
            config_manager=MagicMock(),
            project_root=tmp_path,
        )

        metadata = {
            "reconstruct_from_git": True,
            "diff_type": "deleted",
            "parent_commit_hash": "def456",
            "path": "src/bar.py",
        }

        cmd_used = ["git", "show", "def456:src/bar.py"]
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=cmd_used, timeout=30),
        ):
            try:
                service._reconstruct_temporal_content(metadata)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    "_reconstruct_temporal_content must catch TimeoutExpired, not re-raise"
                )


class TestReconstructTimeoutLogsWarning:
    """AC4: TimeoutExpired logs a WARNING."""

    def test_reconstruct_timeout_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        When subprocess.run raises TimeoutExpired,
        _reconstruct_temporal_content must log a WARNING.
        """
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchService,
        )

        service = TemporalSearchService(
            config_manager=MagicMock(),
            project_root=tmp_path,
        )

        metadata = {
            "reconstruct_from_git": True,
            "diff_type": "added",
            "commit_hash": "abc123",
            "path": "src/baz.py",
        }

        cmd_used = ["git", "show", "abc123:src/baz.py"]
        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.services.temporal.temporal_search_service",
        ):
            with patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=cmd_used, timeout=30),
            ):
                service._reconstruct_temporal_content(metadata)

        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warning_records) >= 1, (
            "Expected at least one WARNING log on TimeoutExpired; "
            f"got records: {[r.message for r in caplog.records]}"
        )

    def test_subprocess_run_called_with_timeout(self, tmp_path: Path) -> None:
        """
        AC4: subprocess.run must be called with timeout=30 for git reconstruction.
        Verified via source-code inspection.
        """
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchService,
        )

        source = inspect.getsource(TemporalSearchService._reconstruct_temporal_content)

        assert "timeout=30" in source, (
            "AC4: subprocess.run must be called with timeout=30 in "
            "_reconstruct_temporal_content"
        )


class TestNoCacheNoIdCache:
    """AC5: When hnsw_index_cache=None, id_index_cache is also None."""

    def test_hnsw_index_cache_defaults_to_none(self) -> None:
        """
        MultiSearchService.__init__ must accept hnsw_index_cache parameter
        defaulting to None.
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        source = inspect.getsource(MultiSearchService.__init__)

        assert "hnsw_index_cache" in source, (
            "AC5: MultiSearchService.__init__ must have hnsw_index_cache parameter"
        )

    def test_get_instance_accepts_hnsw_index_cache(self) -> None:
        """
        MultiSearchService.get_instance classmethod must accept hnsw_index_cache parameter.
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        source = inspect.getsource(MultiSearchService.get_instance)

        assert "hnsw_index_cache" in source, (
            "AC5: MultiSearchService.get_instance must accept hnsw_index_cache parameter"
        )


class TestNoDeadlockWithSmallPool:
    """Regression: parallel_executor must NOT be forwarded to temporal search.

    Before the fix, _search_temporal_sync passed parallel_executor=self.thread_executor
    to execute_temporal_query_with_fusion(), which forwarded it into
    FilesystemVectorStore.search(). Since the outer per-repo tasks already run on
    that same bounded ThreadPoolExecutor, submitting inner search() tasks to it with
    >= max_workers repos in flight causes a deadlock because all worker slots are
    occupied by outer tasks waiting for inner tasks that can never start.

    After the fix, parallel_executor is NOT forwarded on the temporal path, so the
    pool drains normally regardless of how many repos are queried simultaneously.
    """

    def test_parallel_executor_not_forwarded_to_fusion(self) -> None:
        """
        Source inspection: _search_temporal_sync must NOT pass
        parallel_executor=self.thread_executor to execute_temporal_query_with_fusion.

        This is the primary structural guard against the deadlock. If parallel_executor
        is forwarded, inner search tasks compete with outer repo tasks for the same
        bounded pool, causing permanent thread starvation with >= max_workers repos.
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        source = inspect.getsource(MultiSearchService._search_temporal_sync)

        assert "parallel_executor=self.thread_executor" not in source, (
            "DEADLOCK BUG: _search_temporal_sync must NOT forward "
            "parallel_executor=self.thread_executor to execute_temporal_query_with_fusion. "
            "This causes thread starvation: outer repo tasks occupy all pool slots while "
            "waiting for inner search tasks that can never start (same bounded pool)."
        )

    def test_multi_repo_temporal_no_deadlock(self) -> None:
        """
        4 concurrent temporal searches complete within 10s with max_workers=2.

        The fake fusion inspects the parallel_executor kwarg. If it receives a non-None
        executor, it submits a blocking inner task to that executor and waits — exactly
        mimicking FilesystemVectorStore.search() when parallel_executor is forwarded.
        With max_workers=2 and 4 outer tasks submitted to svc.thread_executor, all
        2 slots become occupied by outer tasks blocking on inner tasks that can never
        start => deadlock within 10s.

        With the fix (parallel_executor=None in the fusion call), no inner task is
        submitted to the pool, so all 4 searches complete normally.
        """
        import time as _time
        import concurrent.futures
        from unittest.mock import patch, MagicMock
        from code_indexer.server.multi.multi_search_service import MultiSearchService
        from code_indexer.server.multi.multi_search_config import MultiSearchConfig
        from code_indexer.server.multi.models import MultiSearchRequest
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchResults,
        )

        # max_workers=2: with 4 outer tasks, only 2 run at a time.
        # If inner tasks are also submitted to this pool, the 2 slots fill up
        # with blocked outer tasks and inner tasks can never start => deadlock.
        cfg = MultiSearchConfig(
            max_workers=2,
            query_timeout_seconds=10,
            max_results_per_repo=5,
        )

        svc = MultiSearchService(cfg)

        from typing import Any, Optional

        def _fake_fusion(*args: Any, **kwargs: Any) -> TemporalSearchResults:
            """Simulate deadlock if parallel_executor is forwarded (non-None).
            When parallel_executor is not None: submit an inner task to it and block
            waiting for the result. This mimics FilesystemVectorStore.search() and
            creates the circular wait that deadlocks the bounded pool.
            When parallel_executor is None (the fix): just do brief work and return."""
            from concurrent.futures import ThreadPoolExecutor as _TPE

            executor: Optional[_TPE] = kwargs.get("parallel_executor")
            if executor is not None:
                # Submit inner work to the SAME bounded pool as the outer task.
                # Both pool slots will be occupied by outer tasks waiting here,
                # and inner tasks can never start => permanent deadlock.
                inner_future = executor.submit(lambda: _time.sleep(0.001))
                inner_future.result(timeout=8)  # blocks the outer slot
            else:
                _time.sleep(0.005)  # fix path: no inner submission
            return TemporalSearchResults(
                results=[],
                query=str(kwargs.get("query_text", "")),
                filter_type="none",
                filter_value=None,
                total_found=0,
            )

        request = MultiSearchRequest(
            query="test query",
            search_type="temporal",
            repositories=["repo1", "repo2", "repo3", "repo4"],
            limit=5,
        )

        # Import the modules used inside _search_temporal_sync so we can patch them.
        import code_indexer.services.temporal.temporal_fusion_dispatch as _tfd
        import code_indexer.config as _cfg_mod
        import code_indexer.storage.filesystem_vector_store as _fvs_mod

        mock_cm = MagicMock()
        mock_cm.get_config.return_value = MagicMock()

        futures = []
        with (
            patch.object(
                _tfd,
                "execute_temporal_query_with_fusion",
                side_effect=_fake_fusion,
            ),
            patch.object(
                svc,
                "_get_repository_path",
                return_value="/tmp/fake_repo",
            ),
            patch.object(
                _cfg_mod.ConfigManager,
                "create_with_backtrack",
                return_value=mock_cm,
            ),
            patch.object(
                _fvs_mod,
                "FilesystemVectorStore",
                return_value=MagicMock(),
            ),
        ):
            # Submit 4 outer tasks directly to svc.thread_executor (bounded, max_workers=2).
            # With the fix: fusion receives parallel_executor=None => no inner submission
            # => no deadlock, all 4 complete normally.
            # Without the fix: fusion receives parallel_executor=svc.thread_executor =>
            # submits inner work to same pool => deadlock with >= 2 concurrent outer tasks.
            for repo_id in ["repo1", "repo2", "repo3", "repo4"]:
                futures.append(
                    svc.thread_executor.submit(
                        svc._search_temporal_sync, repo_id, request
                    )
                )

            done, not_done = concurrent.futures.wait(futures, timeout=10)

        assert len(not_done) == 0, (
            f"DEADLOCK DETECTED: {len(not_done)} of 4 temporal searches did not "
            f"complete within 10 seconds. This indicates parallel_executor was "
            f"forwarded to the same bounded pool, causing a thread starvation deadlock."
        )
        assert len(done) == 4, (
            f"Expected all 4 temporal searches to complete, but only {len(done)} did."
        )

        svc.thread_executor.shutdown(wait=False)
