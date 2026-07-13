"""
Tests for the golden-repo registry reconciler's circuit-breaker
confirmation mechanism (Bug #1382).

Bug #1317 added ORPHAN_FRACTION_ABORT_THRESHOLD (0.5): if more than half of
registered golden repos resolve absent, the sweep refuses to delete
anything, on the theory that such a high ratio usually signals an
infra/mount problem rather than real orphans. A live staging incident
(Bug #1382) proved this circuit-breaker had NO way to recover: 8/14 (57%)
repos were genuine, persistent registry-orphans from a crash-recovery gap
(DB recovered, on-disk clones were not), and the breaker aborted on every
single restart for ~2 months with zero path to resolution.

These tests prove the fix: a persisted, cross-restart confirmation counter
(golden_repo_reconcile_breaker_state, Bug #1382) that lets the sweep
distinguish "the SAME orphan-candidate set observed on multiple consecutive
sweeps, each with a healthy base directory" (real orphans -- eventually
auto-heals) from a genuine one-off blip or real infra flapping (must keep
aborting forever).

Uses the REAL GoldenRepoManager + REAL SQLite backend, exactly like
test_golden_repo_reconciler_bug1317.py -- only BackgroundJobManager is
mocked.
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepoManager,
    GoldenRepo,
)
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.golden_repo_reconciler import (
    reconcile_golden_repo_registry,
)

# golden_repo_reconcile_breaker_state is a singleton-row table
# (id INTEGER PRIMARY KEY CHECK (id = 1)) -- see sqlite_backends.py.
_BREAKER_STATE_SINGLETON_ROW_ID = 1


@pytest.mark.e2e
class TestGoldenRepoReconcilerCircuitBreakerBug1382:
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
        """
        Directly rewrite the persisted `last_observed_at` on the REAL
        sqlite breaker-state row to simulate a genuinely time-separated
        observation (Bug #1382 rolling-deploy hardening tests). Operates
        straight on the DB file (manager.db_path) rather than any
        in-process cache, matching the cluster-aware-state contract the
        gating logic itself relies on.
        """
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

    # ------------------------------------------------------------------
    # Scenario (a): one-off high-ratio event still aborts -- existing
    # Bug #1317 safety must survive this change unmodified.
    # ------------------------------------------------------------------

    def test_first_high_ratio_sweep_still_aborts(self, manager):
        """A genuine one-off high-absence-ratio sweep (healthy base dir)
        must still abort on its FIRST occurrence -- the pre-existing
        Bug #1317 safety is preserved, not weakened."""
        self._register_repo(manager, "healthy-only", create_clone_dir=True)
        self._register_repo(manager, "absent-1", create_clone_dir=False)
        self._register_repo(manager, "absent-2", create_clone_dir=False)

        result = reconcile_golden_repo_registry(manager)

        assert result.aborted is True
        assert result.circuit_breaker_consecutive_count == 1
        assert result.circuit_breaker_confirmed_proceed is False
        assert result.orphans_removed == []
        manager.background_job_manager.submit_job.assert_not_called()

    # ------------------------------------------------------------------
    # Scenario (b): the SAME persistent orphan set across multiple
    # consecutive sweeps eventually auto-heals.
    # ------------------------------------------------------------------

    def test_three_consecutive_matching_sweeps_proceeds_with_removal(self, manager):
        """This reproduces the actual staging incident: the SAME 2 orphan
        aliases (out of 3 total, 66% > 50%) observed on THREE consecutive
        restarts, each with a healthy base directory. The first two sweeps
        must still abort (confirmation not yet reached); the third must
        proceed and actually remove the orphans."""
        self._register_repo(manager, "healthy-companion", create_clone_dir=True)
        self._register_repo(manager, "stuck-orphan-1", create_clone_dir=False)
        self._register_repo(manager, "stuck-orphan-2", create_clone_dir=False)

        result1 = reconcile_golden_repo_registry(manager)
        assert result1.aborted is True
        assert result1.circuit_breaker_consecutive_count == 1

        # Bug #1382 rolling-deploy hardening: same-fingerprint observations
        # only increment the count once genuinely separated by wall-clock
        # time (MIN_BREAKER_OBSERVATION_GAP_SECONDS) -- backdate the
        # persisted last_observed_at to simulate that real separation
        # (matching the ~2-month-spaced real incident, not a rolling
        # deploy's few-minutes window).
        self._backdate_breaker_last_observed(manager, timedelta(days=2))
        result2 = reconcile_golden_repo_registry(manager)
        assert result2.aborted is True
        assert result2.circuit_breaker_consecutive_count == 2

        self._backdate_breaker_last_observed(manager, timedelta(days=2))
        result3 = reconcile_golden_repo_registry(manager)
        assert result3.aborted is False
        assert result3.circuit_breaker_consecutive_count == 3
        assert result3.circuit_breaker_confirmed_proceed is True
        assert sorted(result3.orphans_removed) == [
            "stuck-orphan-1",
            "stuck-orphan-2",
        ]

        # The healthy companion must never have been touched.
        assert manager._sqlite_backend.get_repo("healthy-companion") is not None

    def test_breaker_state_resets_after_confirmed_removal(self, manager):
        """After a confirmed auto-heal removal, the breaker's persisted
        state must be cleared -- a SUBSEQUENT, unrelated high-ratio streak
        must start confirming from scratch (count 1), not inherit leftover
        confirmation progress from the prior incident."""
        self._register_repo(manager, "healthy-companion-2", create_clone_dir=True)
        self._register_repo(manager, "old-orphan-1", create_clone_dir=False)
        self._register_repo(manager, "old-orphan-2", create_clone_dir=False)

        for i in range(3):
            if i > 0:
                # Bug #1382 rolling-deploy hardening: backdate between
                # sweeps so each is treated as a genuinely time-separated
                # observation, not a same-instant rolling-deploy duplicate.
                self._backdate_breaker_last_observed(manager, timedelta(days=2))
            result = reconcile_golden_repo_registry(manager)
        assert result.circuit_breaker_confirmed_proceed is True

        # Removal jobs were SUBMITTED (captured by the mocked
        # BackgroundJobManager) -- execute them now, exactly like
        # test_reconcile_removal_actually_clears_orphan_row does, to prove
        # the end state.
        for captured_func in manager._captured_funcs:  # type: ignore[attr-defined]
            captured_func()
        assert manager._sqlite_backend.get_repo("old-orphan-1") is None
        assert manager._sqlite_backend.get_repo("old-orphan-2") is None

        # A brand-new, unrelated high-ratio incident: only the healthy
        # companion remains, plus two freshly-registered new orphans.
        self._register_repo(manager, "new-orphan-1", create_clone_dir=False)
        self._register_repo(manager, "new-orphan-2", create_clone_dir=False)

        fresh_result = reconcile_golden_repo_registry(manager)
        assert fresh_result.aborted is True
        assert fresh_result.circuit_breaker_consecutive_count == 1
        assert fresh_result.circuit_breaker_confirmed_proceed is False

    # ------------------------------------------------------------------
    # Scenario (b2), Bug #1382 rolling-deploy hardening: the confirmation
    # counter must gate on genuine wall-clock separation, not sweep count
    # alone -- otherwise a single multi-node rolling deploy (node-1,
    # node-2, node-3 each restarting and sweeping within minutes of each
    # other) collapses into "3 consecutive confirmations" on its own.
    # ------------------------------------------------------------------

    def test_rolling_deploy_collapse_is_blocked_by_time_gate(self, manager):
        """Simulates one rolling-deploy event: node-1/node-2/node-3 each
        restart and run the sweep within minutes of each other (no
        wall-clock gap injected between calls). The same orphan-candidate
        fingerprint is observed 3 times back-to-back, but this must NOT
        reach confirmed-proceed -- each call after the first is a same-
        fingerprint observation arriving well under
        MIN_BREAKER_OBSERVATION_GAP_SECONDS, so it must be treated as a
        duplicate no-op: aborted stays True, confirmed_proceed stays
        False, and critically the count must NOT advance past 1."""
        self._register_repo(manager, "healthy-companion-4", create_clone_dir=True)
        self._register_repo(manager, "rolling-orphan-1", create_clone_dir=False)
        self._register_repo(manager, "rolling-orphan-2", create_clone_dir=False)

        result1 = reconcile_golden_repo_registry(manager)
        result2 = reconcile_golden_repo_registry(manager)
        result3 = reconcile_golden_repo_registry(manager)

        for result in (result1, result2, result3):
            assert result.aborted is True
            assert result.circuit_breaker_confirmed_proceed is False
            assert result.circuit_breaker_consecutive_count == 1

        manager.background_job_manager.submit_job.assert_not_called()
        assert manager._sqlite_backend.get_repo("rolling-orphan-1") is not None
        assert manager._sqlite_backend.get_repo("rolling-orphan-2") is not None

    def test_genuinely_time_separated_observations_still_confirm(self, manager):
        """Positive proof that realistic multi-day-spaced restarts (the
        actual ~2-month-persistent staging incident cadence) still reach
        confirmed-proceed and auto-heal correctly under the new wall-clock
        gate -- the gate only blocks observations that arrive too close
        together, it never blocks genuinely separated ones."""
        self._register_repo(manager, "healthy-companion-5", create_clone_dir=True)
        self._register_repo(manager, "spaced-orphan-1", create_clone_dir=False)
        self._register_repo(manager, "spaced-orphan-2", create_clone_dir=False)

        result1 = reconcile_golden_repo_registry(manager)
        assert result1.aborted is True
        assert result1.circuit_breaker_consecutive_count == 1

        self._backdate_breaker_last_observed(manager, timedelta(days=2))
        result2 = reconcile_golden_repo_registry(manager)
        assert result2.aborted is True
        assert result2.circuit_breaker_consecutive_count == 2

        self._backdate_breaker_last_observed(manager, timedelta(days=2))
        result3 = reconcile_golden_repo_registry(manager)
        assert result3.aborted is False
        assert result3.circuit_breaker_consecutive_count == 3
        assert result3.circuit_breaker_confirmed_proceed is True
        assert sorted(result3.orphans_removed) == [
            "spaced-orphan-1",
            "spaced-orphan-2",
        ]

        # The healthy companion must never have been touched.
        assert manager._sqlite_backend.get_repo("healthy-companion-5") is not None

        # Execute the captured removal jobs (mirrors other tests in this
        # file) to prove the orphans are actually gone end-to-end.
        for captured_func in manager._captured_funcs:  # type: ignore[attr-defined]
            captured_func()
        assert manager._sqlite_backend.get_repo("spaced-orphan-1") is None
        assert manager._sqlite_backend.get_repo("spaced-orphan-2") is None

    # ------------------------------------------------------------------
    # Scenario (c): genuine infra flapping must NEVER reach confirmation.
    # ------------------------------------------------------------------

    def test_alternating_base_dir_health_never_confirms_or_deletes(self, manager):
        """A base directory that flips between healthy and unhealthy across
        restarts (real infra instability) must NEVER accumulate enough
        consecutive confirmations to auto-proceed, no matter how many
        sweeps occur or how stable the orphan-candidate set looks on the
        healthy sweeps -- each unhealthy sweep resets the counter, so 3
        consecutive HEALTHY confirmations (interrupted by flapping) can
        never happen."""
        self._register_repo(manager, "healthy-companion-3", create_clone_dir=True)
        self._register_repo(manager, "flappy-orphan-1", create_clone_dir=False)
        self._register_repo(manager, "flappy-orphan-2", create_clone_dir=False)

        health_pattern = [True, False, True, False, True]
        results = []
        with patch(
            "code_indexer.server.services.golden_repo_reconciler.os.path.isdir",
            side_effect=health_pattern,
        ):
            for _ in health_pattern:
                results.append(reconcile_golden_repo_registry(manager))

        assert all(r.aborted is True for r in results)
        assert all(r.circuit_breaker_confirmed_proceed is False for r in results)
        # Healthy sweeps (indices 0, 2, 4) each restart the count at 1 --
        # they can never accumulate because the intervening unhealthy
        # sweeps (indices 1, 3) reset the breaker.
        assert results[0].circuit_breaker_consecutive_count == 1
        assert results[2].circuit_breaker_consecutive_count == 1
        assert results[4].circuit_breaker_consecutive_count == 1
        manager.background_job_manager.submit_job.assert_not_called()
        assert manager._sqlite_backend.get_repo("flappy-orphan-1") is not None
        assert manager._sqlite_backend.get_repo("flappy-orphan-2") is not None
