"""
Unit tests for Bug #1842 AC4: honest write-lock-holder diagnostics in
DependencyMapService's "held by another writer" skip logs.

run_full_analysis(), run_delta_analysis(), and run_refinement_cycle() all
log "Full/Delta dependency-map analysis skipped -- write lock for
'cidx-meta' held by another writer (e.g. an in-progress refresh publish)"
when acquire_write_lock() returns False -- but never named WHO actually
held it. These tests assert the log now includes the real holder's owner
name, via describe_scheduler_lock_holder() (write_lock_manager.py),
resolved from refresh_scheduler.write_lock_manager.

Uses a REAL WriteLockManager (Messi Rule #1 anti-mock) for the holder
lookup; refresh_scheduler itself is a MagicMock (only acquire_write_lock
is stubbed to simulate the failed-acquisition path) since building a
full RefreshScheduler is out of scope for this diagnostic-only unit test.
"""

import logging
from unittest.mock import MagicMock

from code_indexer.global_repos.write_lock_manager import WriteLockManager
from code_indexer.server.services.dependency_map_service import DependencyMapService


def _make_scheduler_with_real_lock_held(lock_dir, owner_name="dependency_map_service"):
    """MagicMock refresh_scheduler whose acquire_write_lock() reports a
    failed acquisition, while .write_lock_manager is a REAL
    WriteLockManager reporting the given real holder."""
    lock_manager = WriteLockManager(lock_dir)
    acquired = lock_manager.acquire("cidx-meta", owner_name=owner_name)
    assert acquired is True

    scheduler = MagicMock()
    scheduler.acquire_write_lock.return_value = False
    scheduler.write_lock_manager = lock_manager
    return scheduler, lock_manager


def _make_service(refresh_scheduler, golden_repos_dir):
    golden_repos_manager = MagicMock()
    golden_repos_manager.golden_repos_dir = str(golden_repos_dir)

    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=MagicMock(),
        tracking_backend=MagicMock(),
        analyzer=MagicMock(),
        refresh_scheduler=refresh_scheduler,
    )


class TestRunFullAnalysisSkipLogNamesRealHolder:
    def test_skip_log_names_the_real_holder(self, tmp_path, caplog):
        scheduler, lock_manager = _make_scheduler_with_real_lock_held(
            tmp_path, owner_name="a_different_real_holder"
        )
        service = _make_service(scheduler, tmp_path)
        try:
            with caplog.at_level(logging.INFO):
                result = service.run_full_analysis()

            assert result == {
                "status": "skipped",
                "message": "Write lock held by another writer; skipping this cycle",
            }
            assert any(
                "a_different_real_holder" in record.message for record in caplog.records
            ), [r.message for r in caplog.records]
        finally:
            lock_manager.release("cidx-meta", owner_name="a_different_real_holder")


class TestRunDeltaAnalysisSkipLogNamesRealHolder:
    def test_skip_log_names_the_real_holder(self, tmp_path, caplog):
        scheduler, lock_manager = _make_scheduler_with_real_lock_held(
            tmp_path, owner_name="a_different_real_holder"
        )
        service = _make_service(scheduler, tmp_path)
        try:
            with caplog.at_level(logging.INFO):
                result = service.run_delta_analysis()

            assert result is None
            assert any(
                "a_different_real_holder" in record.message for record in caplog.records
            ), [r.message for r in caplog.records]
        finally:
            lock_manager.release("cidx-meta", owner_name="a_different_real_holder")


class TestRunRefinementCycleSkipLogNamesRealHolder:
    def test_skip_log_names_the_real_holder(self, tmp_path, caplog):
        scheduler, lock_manager = _make_scheduler_with_real_lock_held(
            tmp_path, owner_name="a_different_real_holder"
        )
        service = _make_service(scheduler, tmp_path)
        try:
            with caplog.at_level(logging.INFO):
                result = service.run_refinement_cycle()

            assert result is None
            assert any(
                "a_different_real_holder" in record.message for record in caplog.records
            ), [r.message for r in caplog.records]
        finally:
            lock_manager.release("cidx-meta", owner_name="a_different_real_holder")
