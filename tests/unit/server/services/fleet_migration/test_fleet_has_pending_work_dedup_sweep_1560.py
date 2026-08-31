"""
Codex review Finding F2: the fleet-wide dedup-outcome sweep
(sweep_pending_dedup_outcomes_for_candidate) is only ever REACHED
through `FleetMigrationScheduler._run_next_candidate()`, and that
method is only ever invoked (via a submitted job) when `trigger_now()`
decides `_fleet_has_pending_work()` is True. Once a candidate's
collection has already flipped to CHUNKS_DB (is_repo_already_migrated
== True), the ORIGINAL `_fleet_has_pending_work()` returned False as
soon as every candidate looked migrated -- even when one of them still
has a real, un-swept dedup-outcome journal sitting on disk. Once THAT
happens, `trigger_now()` submits NO job, ever again, for that fleet
state, so the journal (and the loss it records) is never reported to
/health.

This is not a rare crash-only edge case: it is the NORMAL timing gap
between a repair pass completing (chunks.db flips to CHUNKS_DB,
`is_repo_already_migrated()` becomes True) and the NEXT scheduler tick
sweeping the journal -- if that repo happens to be the LAST one needing
migration in the whole fleet, no later tick is ever scheduled to sweep
it.

This test drives the REAL production call path end-to-end
(`scheduler.trigger_now()`, with a `_RealGateBackgroundJobManager` that
synchronously executes the submitted job -- mirroring
test_scheduler_unrecoverable_1486.py's own fixture conventions) rather
than calling the sweep function directly, per the coordinator's
explicit instruction that this class of finding must be proven via the
actual production call path.
"""

from pathlib import Path
from unittest.mock import MagicMock

from code_indexer.server.services.fleet_migration.dedup_state import get_dedup_state
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend
from code_indexer.storage.shared.collection_dedup_repair import (
    read_pending_dedup_outcome,
)

from tests.unit.server.services.fleet_migration.test_scheduler_1458 import (
    _FakeGoldenRepoManager,
    _RealGateBackgroundJobManager,
    _build_already_migrated_repo,
    _make_refresh_scheduler,
    _make_scheduler,
    job_tracker,  # noqa: F401 -- pytest fixture, imported for use below
)


def _write_completed_dedup_journal(collection_dir: Path) -> None:
    """A real, filesystem-proven "completed"-phase dedup-outcome journal
    -- the exact shape a genuinely successful repair pass leaves behind
    (see collection_dedup_repair.py's `_mark_pending_outcome_completed_durably`),
    written directly here to simulate the timing gap between that
    success and the next scheduler tick's sweep."""
    from code_indexer.storage.shared.collection_dedup_repair import (
        _write_pending_outcome_durably,
    )

    _write_pending_outcome_durably(
        collection_dir,
        {
            "phase": "completed",
            "duplicate_groups": 1,
            "records_before": 2,
            "records_deleted": 1,
            "winner_kept_groups": 1,
            "whole_group_deleted_groups": 0,
            "collection_total": 2,
        },
    )


class TestFleetHasPendingWorkSweepsLeftoverJournalOnMigratedRepo:
    def test_trigger_now_submits_a_job_and_sweeps_the_leftover_journal(
        self,
        tmp_path: Path,
        job_tracker,  # noqa: F811
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        base_clone = _build_already_migrated_repo(golden_repos_dir, "repo-a")
        collection_dir = base_clone / ".code-indexer" / "index" / "semantic_collection"
        _write_completed_dedup_journal(collection_dir)

        sqlite_backend = GoldenRepoMetadataSqliteBackend(
            str(tmp_path / "golden_repo_metadata.db")
        )
        sqlite_backend.ensure_table_exists()
        golden = _FakeGoldenRepoManager(
            {"repo-a": base_clone}, sqlite_backend=sqlite_backend
        )
        bg_job_manager = _RealGateBackgroundJobManager(job_tracker)
        scheduler = _make_scheduler(
            tmp_path, golden, refresh_scheduler, background_job_manager=bg_job_manager
        )

        job_id = scheduler.trigger_now()

        assert job_id is not None, (
            "_fleet_has_pending_work() must recognize a leftover "
            "un-swept dedup-outcome journal as pending work, even when "
            "every candidate's collection has already migrated"
        )
        assert read_pending_dedup_outcome(collection_dir) is None, (
            "the leftover journal must have been genuinely swept "
            "(persisted + cleared) via the real production call path"
        )
        state = get_dedup_state(golden, "repo-a")
        assert state is not None
        assert state["records_deleted"] == 1

    def test_trigger_now_submits_no_job_when_fully_migrated_and_no_journal(
        self,
        tmp_path: Path,
        job_tracker,  # noqa: F811
    ) -> None:
        """Regression: the ORIGINAL Bug #1486 auto-stop behavior (no
        job submitted once the fleet is genuinely fully migrated with
        NO leftover journal) is unchanged."""
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        base_clone = _build_already_migrated_repo(golden_repos_dir, "repo-a")
        golden = _FakeGoldenRepoManager({"repo-a": base_clone})
        bg_job_manager = MagicMock()
        scheduler = _make_scheduler(
            tmp_path, golden, refresh_scheduler, background_job_manager=bg_job_manager
        )

        job_id = scheduler.trigger_now()

        assert job_id is None
        bg_job_manager.submit_job.assert_not_called()
