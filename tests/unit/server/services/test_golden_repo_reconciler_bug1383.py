"""
Tests for GitHub Issue #1383 (follow-up to Bug #1382): the golden-repo
registry-reconcile circuit-breaker's escalation signal goes silent exactly
when auto-removal fires.

This module covers the `golden_repo_reconciler.py`-side behaviors from
issue #1383:

1. `_reset_breaker_state()` must be called AFTER Pass 2 (the actual
   `remove_golden_repo()` calls) completes, gated on `result.orphans_removed`
   being non-empty -- not BEFORE Pass 2 runs (the pre-#1383 ordering). If
   confirmation is reached but every removal attempt in Pass 2 then fails
   (e.g. a backend outage mid-sweep), the 3-restart/90-minute confirmation
   investment must be preserved so the NEXT restart does not have to start
   counting from zero again.
2. A confirmed auto-removal that DOES succeed must leave a persistent,
   discoverable trace (`golden_repo_reconcile_auto_heal_event`) that
   survives the breaker-state reset -- so an operator who wasn't watching
   `/health` in real time can still discover after the fact that an
   automatic mass-removal occurred and which repos were affected.

Uses the REAL GoldenRepoManager + REAL SQLite backend, matching
test_golden_repo_reconciler_bug1382.py's approach: only BackgroundJobManager
(an external subsystem, not the reconciler or GoldenRepoManager under test)
is stubbed. The "all Pass 2 removals fail" scenario is induced through the
server's REAL MaintenanceState singleton -- entering maintenance mode makes
GoldenRepoManager.remove_golden_repo() raise its actual MaintenanceModeError
for every alias -- rather than mocking remove_golden_repo() itself.
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepoManager,
    GoldenRepo,
)
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.golden_repo_reconciler import (
    reconcile_golden_repo_registry,
    _record_auto_heal_event,
)
from code_indexer.server.services.maintenance_service import (
    get_maintenance_state,
    _reset_maintenance_state,
)

_BREAKER_STATE_SINGLETON_ROW_ID = 1


class TestGoldenRepoReconcilerAutoHealEventBug1383:
    @pytest.fixture
    def temp_data_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            yield temp_dir

    @pytest.fixture
    def manager(self, temp_data_dir):
        mgr = GoldenRepoManager(data_dir=temp_data_dir)

        from code_indexer.server.storage.database_manager import DatabaseSchema

        DatabaseSchema(mgr.db_path).initialize_database()

        captured_funcs = []
        mock_bjm = MagicMock(spec=BackgroundJobManager)

        def _capture_and_run(**kwargs):
            captured_funcs.append(kwargs["func"])
            return f"job-{kwargs.get('repo_alias', 'unknown')}"

        mock_bjm.submit_job.side_effect = _capture_and_run
        mgr.background_job_manager = mock_bjm
        mgr._captured_funcs = captured_funcs  # type: ignore[attr-defined]
        return mgr

    @pytest.fixture(autouse=True)
    def _clean_maintenance_state(self):
        """Guarantee the real, process-wide MaintenanceState singleton is
        never left active across tests, regardless of test outcome."""
        _reset_maintenance_state()
        yield
        get_maintenance_state().exit_maintenance_mode()
        _reset_maintenance_state()

    def _register_repo(
        self, manager: GoldenRepoManager, alias: str, *, create_clone_dir: bool
    ) -> str:
        clone_path = os.path.join(manager.golden_repos_dir, alias)
        if create_clone_dir:
            os.makedirs(clone_path, exist_ok=True)
        golden_repo = GoldenRepo(
            alias=alias,
            repo_url=f"https://github.com/test/{alias}.git",
            default_branch="main",
            clone_path=clone_path,
            created_at=datetime.now(timezone.utc).isoformat(),
            enable_temporal=False,
            temporal_options=None,
        )
        manager.golden_repos[alias] = golden_repo
        manager._sqlite_backend.add_repo(
            alias=golden_repo.alias,
            repo_url=golden_repo.repo_url,
            default_branch=golden_repo.default_branch,
            clone_path=golden_repo.clone_path,
            created_at=golden_repo.created_at,
            enable_temporal=golden_repo.enable_temporal,
            temporal_options=golden_repo.temporal_options,
        )
        return clone_path

    def _backdate_breaker_last_observed(
        self, manager: GoldenRepoManager, delta: timedelta
    ) -> None:
        backdated = (datetime.now(timezone.utc) - delta).isoformat()
        conn = sqlite3.connect(manager.db_path)
        try:
            conn.execute(
                "UPDATE golden_repo_reconcile_breaker_state "
                "SET last_observed_at = ? WHERE id = ?",
                (backdated, _BREAKER_STATE_SINGLETON_ROW_ID),
            )
            conn.commit()
        finally:
            conn.close()

    def _reach_confirmation(self, manager: GoldenRepoManager):
        """Drive two aborted sweeps (count 1, count 2) so the caller's next
        reconcile_golden_repo_registry(manager) call reaches confirmation
        (count 3)."""
        result1 = reconcile_golden_repo_registry(manager)
        assert result1.aborted is True
        assert result1.circuit_breaker_consecutive_count == 1

        self._backdate_breaker_last_observed(manager, timedelta(days=2))
        result2 = reconcile_golden_repo_registry(manager)
        assert result2.aborted is True
        assert result2.circuit_breaker_consecutive_count == 2

        self._backdate_breaker_last_observed(manager, timedelta(days=2))

    # ------------------------------------------------------------------
    # Requirement: reordered _reset_breaker_state -- a confirmed sweep
    # whose Pass 2 removals ALL fail must NOT reset the breaker state.
    # ------------------------------------------------------------------

    def test_confirmed_sweep_with_all_removals_failing_preserves_breaker_state(
        self, manager
    ):
        """If confirmation is reached (3rd consecutive matching sweep) but
        every remove_golden_repo() call in Pass 2 then fails -- here via a
        REAL maintenance-mode outage, not a mock of the method under test --
        the breaker state must NOT be reset: the 3-restart confirmation
        investment must be preserved so the next restart continues from
        where it left off instead of starting over at count 1."""
        self._register_repo(manager, "healthy-companion-1383a", create_clone_dir=True)
        self._register_repo(manager, "fail-orphan-1", create_clone_dir=False)
        self._register_repo(manager, "fail-orphan-2", create_clone_dir=False)

        self._reach_confirmation(manager)

        get_maintenance_state().enter_maintenance_mode()
        result3 = reconcile_golden_repo_registry(manager)
        get_maintenance_state().exit_maintenance_mode()

        assert result3.aborted is False
        assert result3.circuit_breaker_confirmed_proceed is True
        assert result3.orphans_removed == []
        assert sorted(result3.orphans_failed) == ["fail-orphan-1", "fail-orphan-2"]

        # The breaker state must survive -- NOT reset -- because nothing
        # was actually removed.
        state = manager._sqlite_backend.get_reconcile_breaker_state()
        assert state is not None
        assert state["consecutive_count"] >= 3

        # No auto-heal event should have been recorded either -- nothing
        # was actually healed.
        assert manager._sqlite_backend.get_reconcile_auto_heal_event() is None

    def test_next_restart_after_total_failure_does_not_restart_from_zero(self, manager):
        """After a confirmed sweep whose Pass 2 entirely failed (real
        maintenance-mode outage), the VERY NEXT sweep (simulating the next
        restart, outage resolved) observing the SAME orphan-candidate set
        must immediately be confirmed again, not reset to count 1 -- proving
        the investment was genuinely preserved, not just left inert."""
        self._register_repo(manager, "healthy-companion-1383b", create_clone_dir=True)
        self._register_repo(manager, "fail-orphan-3", create_clone_dir=False)
        self._register_repo(manager, "fail-orphan-4", create_clone_dir=False)

        self._reach_confirmation(manager)

        get_maintenance_state().enter_maintenance_mode()
        result3 = reconcile_golden_repo_registry(manager)
        get_maintenance_state().exit_maintenance_mode()
        assert result3.circuit_breaker_confirmed_proceed is True
        assert result3.orphans_removed == []

        # Next restart: same fingerprint, healthy base dir, outage resolved
        # -- must be immediately confirmed (no need to re-accumulate 3
        # fresh confirmations).
        self._backdate_breaker_last_observed(manager, timedelta(days=2))
        result4 = reconcile_golden_repo_registry(manager)

        assert result4.aborted is False
        assert result4.circuit_breaker_confirmed_proceed is True
        assert sorted(result4.orphans_removed) == ["fail-orphan-3", "fail-orphan-4"]

        # This time removal succeeded -- breaker state must now be reset,
        # and the auto-heal event recorded.
        assert manager._sqlite_backend.get_reconcile_breaker_state() is None
        event = manager._sqlite_backend.get_reconcile_auto_heal_event()
        assert event is not None

    # ------------------------------------------------------------------
    # Requirement: discoverable auto-heal trace survives the counter
    # reset.
    # ------------------------------------------------------------------

    def test_confirmed_removal_success_records_auto_heal_event_surviving_reset(
        self, manager
    ):
        """A confirmed sweep whose Pass 2 removals actually succeed must
        (a) reset the breaker state as before, AND (b) persist a
        discoverable auto-heal event record -- queried AFTER the reset --
        showing the correct removed-alias set and a timestamp, so an
        operator who wasn't watching /health in real time can still
        discover the event after the fact."""
        self._register_repo(manager, "healthy-companion-1383c", create_clone_dir=True)
        self._register_repo(manager, "heal-orphan-1", create_clone_dir=False)
        self._register_repo(manager, "heal-orphan-2", create_clone_dir=False)

        self._reach_confirmation(manager)
        result3 = reconcile_golden_repo_registry(manager)

        assert result3.aborted is False
        assert result3.circuit_breaker_confirmed_proceed is True
        assert sorted(result3.orphans_removed) == ["heal-orphan-1", "heal-orphan-2"]

        # Breaker counter reset (unchanged behavior from Bug #1382).
        assert manager._sqlite_backend.get_reconcile_breaker_state() is None

        # But the discoverable auto-heal trace must survive that reset.
        event = manager._sqlite_backend.get_reconcile_auto_heal_event()
        assert event is not None
        assert set(event["removed_aliases"]) == {"heal-orphan-1", "heal-orphan-2"}
        assert event["occurred_at"] is not None

    # ------------------------------------------------------------------
    # Direct unit coverage of the new persistence helper: proves the
    # auto-heal event records EXACTLY the given aliases (the mechanism
    # that guarantees a partial Pass-2 success never records aliases that
    # actually failed removal, since golden_repo_reconciler.py only ever
    # calls this helper with result.orphans_removed -- never
    # orphans_failed).
    # ------------------------------------------------------------------

    def test_record_auto_heal_event_helper_persists_only_given_aliases(self, manager):
        backend = manager._sqlite_backend

        _record_auto_heal_event(backend, ["only-this-one-was-removed"])

        event = backend.get_reconcile_auto_heal_event()
        assert event is not None
        assert event["removed_aliases"] == ["only-this-one-was-removed"]
        assert event["occurred_at"] is not None

    def test_record_auto_heal_event_helper_is_noop_for_empty_list(self, manager):
        backend = manager._sqlite_backend

        _record_auto_heal_event(backend, [])

        assert backend.get_reconcile_auto_heal_event() is None
