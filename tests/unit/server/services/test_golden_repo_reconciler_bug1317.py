"""
Tests for the golden-repo registry reconciler (Bug #1317, requirement 2).

Detects `golden_repos` registry rows (SQLite in solo mode, PostgreSQL in
cluster mode) whose on-disk clone is absent -- "registry-orphans" -- and
submits their removal via the existing remove_golden_repo() cascade, which
already tears down the row, the alias pointer, the global registry entry,
and any activated-repo cascade consistently.

Also proves the code-review hardening added on top of the initial
implementation:

- A circuit-breaker that refuses to delete anything when an implausibly
  high fraction of registered repos resolve "absent" -- this project has a
  documented reality (project_nfs_host_down_hangs_systemd.md) where a
  stale/hung NFS mount makes `os.path.exists()` return False for EVERY
  repo, which would otherwise look identical to "all orphans" and mass-
  delete the whole registry.
- A positive health check on `golden_repos_dir` itself before sweeping.
- Repair (not deletion) of a healthy, globally-active repo whose alias
  pointer file is missing (the #1315 fallback symptom).
- A single-flight guard so concurrent workers/nodes don't each fire a
  duplicate sweep.

Uses the REAL GoldenRepoManager + REAL SQLite backend (via
list_golden_repos()/get_actual_repo_path()) -- only the BackgroundJobManager
(threading/dispatch boundary) is mocked so removal jobs are captured and can
be executed synchronously to prove the end state.
"""

import os
import tempfile
from datetime import datetime, timezone
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
from code_indexer.server.services.job_tracker import DuplicateJobError


@pytest.mark.e2e
class TestGoldenRepoReconcilerBug1317:
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

    def _make_globally_active(self, manager: GoldenRepoManager, alias: str) -> None:
        """Register a `global_repos` row for `{alias}-global` WITHOUT writing
        an alias pointer file -- reproduces the #1315 fallback symptom
        (registry says global, pointer missing) for repair-path tests."""
        from code_indexer.server.storage.sqlite_backends import (
            GlobalReposSqliteBackend,
        )

        golden_repo = manager.golden_repos[alias]
        backend = GlobalReposSqliteBackend(manager.db_path)
        backend.register_repo(
            alias_name=f"{alias}-global",
            repo_name=alias,
            repo_url=golden_repo.repo_url,
            index_path=golden_repo.clone_path,
        )

    # ------------------------------------------------------------------
    # Baseline behavior (orphan found/removed, healthy untouched)
    # ------------------------------------------------------------------

    def test_reconcile_finds_orphan_and_leaves_healthy_repo_untouched(self, manager):
        """A repo with no on-disk clone is detected as an orphan and
        submitted for removal; a healthy repo (clone present) is left
        completely alone. 1 orphan / 2 total = 50%, at (not over) the
        circuit-breaker threshold, so the sweep proceeds.
        """
        self._register_repo(manager, "healthy-repo", create_clone_dir=True)
        self._register_repo(manager, "orphan-repo", create_clone_dir=False)

        result = reconcile_golden_repo_registry(manager)

        assert result.aborted is False
        assert result.orphans_found == ["orphan-repo"]
        assert result.orphans_removed == ["orphan-repo"]
        assert result.orphans_failed == []
        assert result.healthy_count == 1

        submitted_aliases = [
            call.kwargs["repo_alias"]
            for call in manager.background_job_manager.submit_job.call_args_list
        ]
        assert submitted_aliases == ["orphan-repo"]
        assert manager._sqlite_backend.get_repo("healthy-repo") is not None

    def test_reconcile_removal_actually_clears_orphan_row(self, manager):
        """Executing the submitted removal job for an orphan actually
        removes it from the shared registry backend. A companion healthy
        repo keeps the absent-fraction (1/2 = 50%) at the threshold, not
        over it, so the circuit-breaker does not interfere.
        """
        self._register_repo(manager, "healthy-companion", create_clone_dir=True)
        self._register_repo(manager, "orphan-repo-2", create_clone_dir=False)

        result = reconcile_golden_repo_registry(manager)
        assert result.aborted is False
        assert result.orphans_removed == ["orphan-repo-2"]

        assert len(manager._captured_funcs) == 1  # type: ignore[attr-defined]
        manager._captured_funcs[0]()  # type: ignore[attr-defined]

        assert manager._sqlite_backend.get_repo("orphan-repo-2") is None

    def test_reconcile_with_no_orphans_removes_nothing(self, manager):
        """All repos healthy -> reconcile is a complete no-op."""
        self._register_repo(manager, "healthy-a", create_clone_dir=True)
        self._register_repo(manager, "healthy-b", create_clone_dir=True)

        result = reconcile_golden_repo_registry(manager)

        assert result.aborted is False
        assert result.orphans_found == []
        assert result.orphans_removed == []
        assert result.healthy_count == 2
        manager.background_job_manager.submit_job.assert_not_called()

    def test_reconcile_records_orphan_as_failed_when_removal_submission_errors(
        self, manager
    ):
        """If submitting the removal itself raises (e.g. maintenance mode,
        transient error), the orphan is recorded as failed -- never silently
        dropped -- and the reconcile pass continues without crashing. A
        companion healthy repo keeps the absent-fraction at the (non-
        tripping) 50% threshold.
        """
        self._register_repo(manager, "healthy-companion-2", create_clone_dir=True)
        self._register_repo(manager, "orphan-repo-3", create_clone_dir=False)
        manager.background_job_manager.submit_job.side_effect = RuntimeError(
            "simulated job-submission failure"
        )

        result = reconcile_golden_repo_registry(manager)

        assert result.aborted is False
        assert result.orphans_found == ["orphan-repo-3"]
        assert result.orphans_removed == []
        assert result.orphans_failed == ["orphan-repo-3"]

    # ------------------------------------------------------------------
    # Finding 1: circuit-breaker against mass-deletion on infra/mount blips
    # ------------------------------------------------------------------

    def test_circuit_breaker_aborts_when_all_repos_resolve_absent(self, manager):
        """100% of registered repos resolve absent -> refuse to delete
        anything (this is the exact staging-outage shape: a stale/hung NFS
        mount makes every repo look gone, not orphaned)."""
        self._register_repo(manager, "repo-a", create_clone_dir=False)
        self._register_repo(manager, "repo-b", create_clone_dir=False)
        self._register_repo(manager, "repo-c", create_clone_dir=False)

        result = reconcile_golden_repo_registry(manager)

        assert result.aborted is True
        assert result.abort_reason is not None
        assert "3/3" in result.abort_reason
        assert result.orphans_removed == []
        manager.background_job_manager.submit_job.assert_not_called()

        # Nothing was destroyed -- all three rows remain registered.
        assert manager._sqlite_backend.get_repo("repo-a") is not None
        assert manager._sqlite_backend.get_repo("repo-b") is not None
        assert manager._sqlite_backend.get_repo("repo-c") is not None

    def test_circuit_breaker_aborts_when_majority_resolve_absent(self, manager):
        """75% absent (3 of 4) is well over the 50% threshold -> abort."""
        self._register_repo(manager, "healthy-only", create_clone_dir=True)
        self._register_repo(manager, "absent-1", create_clone_dir=False)
        self._register_repo(manager, "absent-2", create_clone_dir=False)
        self._register_repo(manager, "absent-3", create_clone_dir=False)

        result = reconcile_golden_repo_registry(manager)

        assert result.aborted is True
        assert result.orphans_removed == []
        manager.background_job_manager.submit_job.assert_not_called()

    def test_circuit_breaker_does_not_trip_for_minority_orphan(self, manager):
        """20% absent (1 of 5) is a plausible real minority -> proceeds
        normally, removing only the genuine orphan."""
        self._register_repo(manager, "healthy-1", create_clone_dir=True)
        self._register_repo(manager, "healthy-2", create_clone_dir=True)
        self._register_repo(manager, "healthy-3", create_clone_dir=True)
        self._register_repo(manager, "healthy-4", create_clone_dir=True)
        self._register_repo(manager, "lone-orphan", create_clone_dir=False)

        result = reconcile_golden_repo_registry(manager)

        assert result.aborted is False
        assert result.orphans_found == ["lone-orphan"]
        assert result.orphans_removed == ["lone-orphan"]
        assert result.healthy_count == 4

    def test_versioned_snapshot_repo_not_flagged_orphan(self, manager):
        """A repo whose flat metadata clone_path is absent but whose
        `.versioned/{alias}/v_*/` snapshot IS present resolves via Priority
        2 of get_actual_repo_path() -- it is healthy, not an orphan, and
        must never be removed or counted toward the absent-fraction."""
        alias = "versioned-repo"
        clone_path = os.path.join(manager.golden_repos_dir, alias)
        # Intentionally do NOT create clone_path -- flat metadata path is
        # absent by design for this test.
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
        versioned_dir = os.path.join(
            manager.golden_repos_dir, ".versioned", alias, "v_1"
        )
        os.makedirs(versioned_dir, exist_ok=True)

        result = reconcile_golden_repo_registry(manager)

        assert result.aborted is False
        assert result.orphans_found == []
        assert result.healthy_count == 1
        manager.background_job_manager.submit_job.assert_not_called()
        assert manager._sqlite_backend.get_repo(alias) is not None

    def test_unhealthy_golden_repos_dir_aborts_sweep(self, manager):
        """If the base golden_repos_dir itself fails a positive health
        check (simulating a fully unmounted/inaccessible volume), the
        sweep must abort BEFORE evaluating any individual repo -- even
        genuine orphans must survive an infra outage."""
        self._register_repo(manager, "would-be-orphan", create_clone_dir=False)

        with patch(
            "code_indexer.server.services.golden_repo_reconciler.os.path.isdir",
            return_value=False,
        ):
            result = reconcile_golden_repo_registry(manager)

        assert result.aborted is True
        assert result.abort_reason is not None
        assert result.orphans_removed == []
        manager.background_job_manager.submit_job.assert_not_called()
        assert manager._sqlite_backend.get_repo("would-be-orphan") is not None

    # ------------------------------------------------------------------
    # Finding 2: repair (not delete) a healthy global repo missing its
    # alias pointer
    # ------------------------------------------------------------------

    def test_pointer_repair_for_healthy_global_repo_missing_pointer(self, manager):
        """Clone present + registry says globally-active + pointer file
        missing (the #1315 fallback symptom) -> the pointer is repaired,
        the repo is NEVER deleted."""
        from code_indexer.global_repos.alias_manager import AliasManager

        clone_path = self._register_repo(
            manager, "needs-pointer", create_clone_dir=True
        )
        self._make_globally_active(manager, "needs-pointer")

        alias_manager = AliasManager(os.path.join(manager.golden_repos_dir, "aliases"))
        assert alias_manager.alias_exists("needs-pointer-global") is False

        result = reconcile_golden_repo_registry(manager)

        assert result.aborted is False
        assert result.orphans_found == []
        assert result.pointers_repaired == ["needs-pointer"]
        assert result.pointers_repair_failed == []
        manager.background_job_manager.submit_job.assert_not_called()

        assert alias_manager.alias_exists("needs-pointer-global") is True
        assert alias_manager.read_alias("needs-pointer-global") == clone_path

    def test_pointer_repair_skipped_for_non_global_repo(self, manager):
        """Healthy repo that was never globally activated -- no global
        registry row -- must NOT get a pointer fabricated for it."""
        from code_indexer.global_repos.alias_manager import AliasManager

        self._register_repo(manager, "never-global", create_clone_dir=True)

        result = reconcile_golden_repo_registry(manager)

        assert result.pointers_repaired == []
        assert result.pointers_repair_failed == []

        alias_manager = AliasManager(os.path.join(manager.golden_repos_dir, "aliases"))
        assert alias_manager.alias_exists("never-global-global") is False

    def test_pointer_repair_failure_recorded(self, manager):
        """If the pointer re-write itself fails, it is recorded in
        pointers_repair_failed -- never a silent no-op, never a crash."""
        self._register_repo(manager, "repair-fails", create_clone_dir=True)
        self._make_globally_active(manager, "repair-fails")

        with patch(
            "code_indexer.global_repos.alias_manager.AliasManager.create_alias",
            side_effect=RuntimeError("simulated disk-full on pointer write"),
        ):
            result = reconcile_golden_repo_registry(manager)

        assert result.aborted is False
        assert result.pointers_repaired == []
        assert result.pointers_repair_failed == ["repair-fails"]

    # ------------------------------------------------------------------
    # Finding 3 (cheap cleanup): single-flight guard across workers/nodes
    # ------------------------------------------------------------------

    def test_single_flight_guard_skips_when_job_tracker_reports_duplicate(
        self, manager
    ):
        """Another worker/node already claimed the sweep -> this call must
        abort WITHOUT touching anything, even though a genuine orphan is
        present."""
        self._register_repo(manager, "healthy-x", create_clone_dir=True)
        self._register_repo(manager, "orphan-x", create_clone_dir=False)

        fake_tracker = MagicMock()
        fake_tracker.register_job_if_no_conflict.side_effect = DuplicateJobError(
            operation_type="golden_repo_reconcile_sweep",
            repo_alias="__golden_repo_reconcile_sweep__",
            existing_job_id="other-worker-job-id",
        )
        manager.job_tracker = fake_tracker

        result = reconcile_golden_repo_registry(manager)

        assert result.aborted is True
        assert result.orphans_removed == []
        manager.background_job_manager.submit_job.assert_not_called()
        fake_tracker.complete_job.assert_not_called()
        assert manager._sqlite_backend.get_repo("orphan-x") is not None

    def test_single_flight_guard_allows_when_no_conflict(self, manager):
        """No conflicting sweep -> proceeds normally and marks the
        coordination job complete on success."""
        self._register_repo(manager, "healthy-y", create_clone_dir=True)
        self._register_repo(manager, "orphan-y", create_clone_dir=False)

        fake_tracker = MagicMock()
        fake_tracker.register_job_if_no_conflict.return_value = None
        manager.job_tracker = fake_tracker

        result = reconcile_golden_repo_registry(manager)

        assert result.aborted is False
        assert result.orphans_removed == ["orphan-y"]
        fake_tracker.register_job_if_no_conflict.assert_called_once()
        fake_tracker.complete_job.assert_called_once()
        fake_tracker.fail_job.assert_not_called()

    # ------------------------------------------------------------------
    # Coverage backfill: remaining defensive branches
    # ------------------------------------------------------------------

    def test_health_check_treats_raised_oserror_as_unhealthy(self, manager):
        """A raised OSError (not just a False return) from the health
        check must also be treated as unhealthy -- this is the exact
        stale/hung-NFS shape (ESTALE/EIO/ETIMEDOUT), not merely a clean
        'path does not exist'."""
        self._register_repo(manager, "would-be-orphan-2", create_clone_dir=False)

        with patch(
            "code_indexer.server.services.golden_repo_reconciler.os.path.isdir",
            side_effect=OSError("simulated ESTALE"),
        ):
            result = reconcile_golden_repo_registry(manager)

        assert result.aborted is True
        assert result.orphans_removed == []
        manager.background_job_manager.submit_job.assert_not_called()

    def test_unexpected_sweep_error_is_caught_and_fails_coordination_job(self, manager):
        """An unexpected exception during the sweep (not a per-alias
        failure) must be caught, surfaced via abort_reason, and reported
        to the single-flight coordination job as failed -- never crash the
        caller (startup lifespan)."""
        self._register_repo(manager, "some-repo", create_clone_dir=True)

        fake_tracker = MagicMock()
        fake_tracker.register_job_if_no_conflict.return_value = None
        manager.job_tracker = fake_tracker

        with patch.object(
            manager,
            "list_golden_repos",
            side_effect=RuntimeError("simulated unexpected DB error"),
        ):
            result = reconcile_golden_repo_registry(manager)

        assert result.aborted is True
        assert result.abort_reason is not None
        assert "unexpected" in result.abort_reason
        fake_tracker.fail_job.assert_called_once()
        fake_tracker.complete_job.assert_not_called()

    def test_complete_job_failure_does_not_affect_returned_result(self, manager):
        """If marking the coordination job complete itself raises, the
        sweep's own result must still be returned intact -- this is a
        best-effort bookkeeping step, not part of the sweep's correctness."""
        self._register_repo(manager, "healthy-z", create_clone_dir=True)
        self._register_repo(manager, "orphan-z", create_clone_dir=False)

        fake_tracker = MagicMock()
        fake_tracker.register_job_if_no_conflict.return_value = None
        fake_tracker.complete_job.side_effect = RuntimeError(
            "simulated tracker DB error"
        )
        manager.job_tracker = fake_tracker

        result = reconcile_golden_repo_registry(manager)

        assert result.aborted is False
        assert result.orphans_removed == ["orphan-z"]

    def test_pointer_repair_noop_when_pointer_already_present(self, manager):
        """A healthy, globally-active repo whose pointer is ALREADY present
        (the normal steady-state case) must be a true no-op: not repaired,
        not touched, not an error."""
        from code_indexer.global_repos.alias_manager import AliasManager

        clone_path = self._register_repo(manager, "already-fine", create_clone_dir=True)
        self._make_globally_active(manager, "already-fine")

        alias_manager = AliasManager(os.path.join(manager.golden_repos_dir, "aliases"))
        alias_manager.create_alias(
            alias_name="already-fine-global",
            target_path=clone_path,
            repo_name="already-fine",
        )

        result = reconcile_golden_repo_registry(manager)

        assert result.aborted is False
        assert result.pointers_repaired == []
        assert result.pointers_repair_failed == []
        assert alias_manager.read_alias("already-fine-global") == clone_path
