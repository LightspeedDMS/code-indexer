"""Tests for Bug #1486 Fix C: FleetMigrationScheduler must auto-stop once
the fleet has no pending work, and must treat a repo whose migration
fails with UnrecoverableConsolidationCorruptionError as a distinct,
non-retryable terminal state -- excluded from the pending set (so it
neither blocks done-detection nor is retried every tick) -- rather than
looping it through the ordinary quarantine-then-retry cycle forever.

Confirmed production incident this closes: the "evolution" golden repo's
chunks.db was corrupt with its legacy source already deleted; the
scheduler retried it every tick (481 consecutive failures) and never
stopped scheduling once done, running a no-op job every tick forever.

Real GoldenRepoMetadataSqliteBackend persistence, real filesystem
collections/chunks.db, real orchestrator/scheduler wiring -- no mocking
of the scheduler's/quarantine's own decision logic.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.fleet_migration.discovery import (
    enumerate_fleet_migration_candidates,
)
from code_indexer.server.services.fleet_migration.quarantine import (
    UNRECOVERABLE_FAILURE_CAUSE,
    QuarantineStateUnavailableError,
    is_permanently_unrecoverable,
    is_quarantined,
    record_unrecoverable_corruption,
)
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator

from tests.unit.server.services.fleet_migration.test_scheduler_1458 import (
    _FakeGoldenRepoManager,
    _RecordingConfigService,
    _build_already_migrated_repo,
    _build_unconsolidated_repo,
    _make_refresh_scheduler,
    _make_scheduler,
)


def _make_backend(tmp_path: Path) -> GoldenRepoMetadataSqliteBackend:
    db_path = str(tmp_path / "golden_repo_metadata.db")
    backend = GoldenRepoMetadataSqliteBackend(db_path)
    backend.ensure_table_exists()
    return backend


def _build_unrecoverable_corrupt_repo(golden_repos_dir: Path, alias: str) -> Path:
    """A real golden repo base clone whose ONE semantic collection is
    genuinely, permanently unrecoverable: the chunks_db discriminator is
    committed (so resolve_chunk_layout reports CHUNKS_DB), chunks.db
    itself is corrupt/unopenable, and the legacy vector_*.json source is
    entirely absent -- reproducing the exact confirmed 'evolution'
    incident state."""
    base_clone = golden_repos_dir / alias
    index_path = base_clone / ".code-indexer" / "index"
    collection_dir = index_path / "semantic_collection"
    collection_dir.mkdir(parents=True)
    (collection_dir / "collection_meta.json").write_text(
        json.dumps({"name": "coll", "vector_size": 2})
    )
    write_chunks_db_discriminator(collection_dir)
    (collection_dir / "chunks.db").write_bytes(b"corrupted, no legacy, no manifest")
    return base_clone


class TestSchedulerAutoStopsOnFullyMigratedFleet:
    """Bug #1486 Fix C item 1: all-migrated fleet -> a scheduler tick
    submits NO job."""

    def test_trigger_now_submits_no_job_when_every_repo_is_migrated(
        self, tmp_path: Path
    ) -> None:
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


class TestUnrecoverableCorruptionIsNotRetriedAndDoesNotBlockAutoStop:
    """Bug #1486 Fix C item 2: a repo marked unrecoverable-corruption is
    not retried and does not prevent the 'all migrated' stop."""

    def test_migration_failure_is_recorded_as_permanently_unrecoverable(
        self, tmp_path: Path
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        corrupt_base = _build_unrecoverable_corrupt_repo(golden_repos_dir, "evolution")
        backend = _make_backend(tmp_path)
        golden = _FakeGoldenRepoManager(
            {"evolution": corrupt_base}, sqlite_backend=backend
        )
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        result = scheduler._run_next_candidate()

        # Never raises UnrecoverableConsolidationCorruptionError through
        # the scheduler -- it is caught, recorded, and the sweep moves on.
        assert result == {"status": "nothing_to_migrate"}
        state = backend.get_fleet_migration_failure_state("evolution")
        assert state is not None
        assert state["failure_cause"] == UNRECOVERABLE_FAILURE_CAUSE
        candidate = next(iter(enumerate_fleet_migration_candidates(golden)))
        assert is_permanently_unrecoverable(golden, "evolution") is True
        # NOT the ordinary quarantine mechanism -- a genuinely distinct
        # classification the scheduler consults separately.
        assert is_quarantined(golden, candidate) is False

    def test_unrecoverable_repo_is_never_retried_on_a_subsequent_tick(
        self, tmp_path: Path
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        corrupt_base = _build_unrecoverable_corrupt_repo(golden_repos_dir, "evolution")
        backend = _make_backend(tmp_path)
        golden = _FakeGoldenRepoManager(
            {"evolution": corrupt_base}, sqlite_backend=backend
        )
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        # First tick: discovers and records the unrecoverable state.
        first_result = scheduler._run_next_candidate()
        assert first_result == {"status": "nothing_to_migrate"}

        # Second tick (simulating the NEXT scheduler tick): must NOT
        # re-attempt migration against the same doomed repo -- still
        # correctly classified as "nothing to migrate", never re-raising
        # UnrecoverableConsolidationCorruptionError again.
        second_result = scheduler._run_next_candidate()
        assert second_result == {"status": "nothing_to_migrate"}

    def test_unrecoverable_repo_does_not_block_all_migrated_auto_stop(
        self, tmp_path: Path
    ) -> None:
        """The combined scenario Bug #1486 explicitly requires: a fleet
        with one genuinely migrated repo and one permanently-
        unrecoverable repo must still be reported as having no pending
        work -- the unrecoverable repo does not count as "still
        pending" forever."""
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        migrated_base = _build_already_migrated_repo(golden_repos_dir, "repo-a-done")
        corrupt_base = _build_unrecoverable_corrupt_repo(
            golden_repos_dir, "repo-b-unrecoverable"
        )
        backend = _make_backend(tmp_path)
        golden = _FakeGoldenRepoManager(
            {"repo-a-done": migrated_base, "repo-b-unrecoverable": corrupt_base},
            sqlite_backend=backend,
        )
        bg_job_manager = MagicMock()
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=bg_job_manager,
            config_service=_RecordingConfigService(enabled=True),
        )

        # First, let the scheduler discover and record the unrecoverable
        # state via a direct _run_next_candidate() call.
        result = scheduler._run_next_candidate()
        assert result == {"status": "nothing_to_migrate"}

        # NOW the fleet is fully "resolved" (one migrated, one
        # permanently unrecoverable) -- trigger_now() must auto-stop,
        # submitting NO job, rather than treating the unrecoverable repo
        # as still-pending forever.
        job_id = scheduler.trigger_now()

        assert job_id is None, (
            "Bug: an unrecoverable-corruption repo was still treated as "
            "'pending work', preventing the fleet from ever reaching "
            "the all-migrated auto-stop state."
        )
        bg_job_manager.submit_job.assert_not_called()

    def test_pending_repo_still_migrates_normally_alongside_an_unrecoverable_one(
        self, tmp_path: Path
    ) -> None:
        """A genuinely pending (not yet migrated, not corrupt) repo must
        still be picked up and migrated normally -- the unrecoverable
        classification must not accidentally suppress real work."""
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        corrupt_base = _build_unrecoverable_corrupt_repo(
            golden_repos_dir, "aaa-unrecoverable"
        )
        pending_base = _build_unconsolidated_repo(golden_repos_dir, "zzz-pending")
        backend = _make_backend(tmp_path)
        golden = _FakeGoldenRepoManager(
            {"aaa-unrecoverable": corrupt_base, "zzz-pending": pending_base},
            sqlite_backend=backend,
        )
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        result = scheduler._run_next_candidate()

        # The alphabetically-first candidate is unrecoverable and is
        # skipped; the scheduler advances to and migrates the pending one.
        assert result["golden_alias"] == "zzz-pending"
        assert result["status"] == "completed"


class TestQuarantinePermanentUnrecoverableClassification:
    """Direct unit tests of quarantine.py's new record_unrecoverable_
    corruption()/is_permanently_unrecoverable() -- distinct from the
    ordinary is_quarantined() auto-clearing breaker."""

    def test_is_permanently_unrecoverable_false_when_never_recorded(
        self, tmp_path: Path
    ) -> None:
        backend = _make_backend(tmp_path)
        golden = _FakeGoldenRepoManager({}, sqlite_backend=backend)

        assert is_permanently_unrecoverable(golden, "never-seen") is False

    def test_record_and_read_round_trips(self, tmp_path: Path) -> None:
        backend = _make_backend(tmp_path)
        golden = _FakeGoldenRepoManager({}, sqlite_backend=backend)

        record_unrecoverable_corruption(golden, "evolution", "corrupt, legacy gone")

        assert is_permanently_unrecoverable(golden, "evolution") is True
        state = backend.get_fleet_migration_failure_state("evolution")
        assert state["failure_cause"] == UNRECOVERABLE_FAILURE_CAUSE

    def test_ordinary_generic_failure_is_not_permanently_unrecoverable(
        self, tmp_path: Path
    ) -> None:
        """An ordinary (recoverable, auto-clearing) quarantine failure
        must never be misclassified as permanently unrecoverable."""
        from code_indexer.server.services.fleet_migration.quarantine import (
            GENERIC_FAILURE_CAUSE,
            record_migration_failure,
        )

        backend = _make_backend(tmp_path)
        golden = _FakeGoldenRepoManager({}, sqlite_backend=backend)

        record_migration_failure(
            golden, "click", "some-signature", failure_cause=GENERIC_FAILURE_CAUSE
        )

        assert is_permanently_unrecoverable(golden, "click") is False

    def test_unrecoverable_state_never_auto_clears_on_directory_change(
        self, tmp_path: Path
    ) -> None:
        """The defining difference from is_quarantined()'s ordinary
        breaker: a genuine on-disk directory-content change (which WOULD
        auto-clear an ordinary quarantine) must NEVER clear the
        permanent unrecoverable classification -- permanent data loss
        has no signature that could ever prove "recovered"; only an
        explicit reset_migration_failure() call may clear it."""
        golden_repos_dir = tmp_path / "golden-repos"
        corrupt_base = _build_unrecoverable_corrupt_repo(golden_repos_dir, "evolution")
        backend = _make_backend(tmp_path)
        golden = _FakeGoldenRepoManager(
            {"evolution": corrupt_base}, sqlite_backend=backend
        )

        record_unrecoverable_corruption(golden, "evolution", "corrupt, legacy gone")
        assert is_permanently_unrecoverable(golden, "evolution") is True

        # Genuinely change the on-disk directory content -- the exact
        # kind of change is_quarantined()'s signature-based auto-clear
        # would react to.
        collection_dir = (
            corrupt_base / ".code-indexer" / "index" / "semantic_collection"
        )
        (collection_dir / "new_file_added_after_the_fact.txt").write_text("changed")

        assert is_permanently_unrecoverable(golden, "evolution") is True, (
            "Bug: the permanent unrecoverable classification was cleared "
            "by a mere on-disk directory change -- it must only clear via "
            "an explicit reset_migration_failure() call."
        )


class TestRecordUnrecoverableFailsClosed:
    """Bug #1486 High Finding 5: record_unrecoverable_corruption() must
    FAIL CLOSED when no backend is available -- unlike ordinary
    record_migration_failure() (which deliberately no-ops for the
    "tracking disabled" case), silently no-op'ing here would let the
    scheduler believe the terminal state was durably persisted and
    retry the same doomed repo forever."""

    def test_record_unrecoverable_corruption_raises_when_no_backend_configured(
        self,
    ) -> None:
        golden = _FakeGoldenRepoManager({})  # sqlite_backend=None (default)

        with pytest.raises(QuarantineStateUnavailableError):
            record_unrecoverable_corruption(golden, "evolution", "corrupt, legacy gone")

    def test_scheduler_aborts_tick_rather_than_silently_proceeding_when_recording_fails(
        self, tmp_path: Path
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        corrupt_base = _build_unrecoverable_corrupt_repo(golden_repos_dir, "evolution")
        # Deliberately NO backend -- the exact fail-closed scenario.
        golden = _FakeGoldenRepoManager({"evolution": corrupt_base})
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        result = scheduler._run_next_candidate()

        assert result["status"] == "quarantine_state_unavailable", (
            "Bug: the scheduler silently proceeded (e.g. reporting "
            "'nothing_to_migrate') as if the unrecoverable state had "
            "been durably recorded, even though recording it genuinely "
            "failed -- the NEXT tick would then retry this doomed repo "
            "forever, believing nothing was ever recorded."
        )


class TestGetStatsExposesUnrecoverableRepos:
    """Bug #1486 High Finding 4: get_stats() must expose an
    unrecoverable_repos count and exclude unrecoverable repos from
    pending_repos -- dashboard visibility distinct from ordinary
    quarantined_repos."""

    def test_unrecoverable_repo_counted_and_excluded_from_pending(
        self, tmp_path: Path
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        migrated_base = _build_already_migrated_repo(golden_repos_dir, "repo-a-done")
        corrupt_base = _build_unrecoverable_corrupt_repo(
            golden_repos_dir, "repo-b-unrecoverable"
        )
        backend = _make_backend(tmp_path)
        golden = _FakeGoldenRepoManager(
            {"repo-a-done": migrated_base, "repo-b-unrecoverable": corrupt_base},
            sqlite_backend=backend,
        )
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )
        # Discover and record the unrecoverable state first.
        scheduler._run_next_candidate()

        stats = scheduler.get_stats()

        assert stats["total_repos"] == 2
        assert stats["migrated_repos"] == 1
        assert stats.get("unrecoverable_repos") == 1, (
            "Bug: get_stats() does not expose an unrecoverable_repos "
            "count -- a permanently-unrecoverable repo is invisible on "
            "the dashboard."
        )
        assert stats["pending_repos"] == 0, (
            "Bug: the unrecoverable repo was still counted as pending -- "
            "it can never migrate via automatic retry, so it must be "
            "excluded from pending_repos."
        )

    def test_no_unrecoverable_repos_reports_zero(self, tmp_path: Path) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        base_clone = _build_already_migrated_repo(golden_repos_dir, "repo-a")
        golden = _FakeGoldenRepoManager({"repo-a": base_clone})
        scheduler = _make_scheduler(
            tmp_path, golden, refresh_scheduler, background_job_manager=MagicMock()
        )

        stats = scheduler.get_stats()

        assert stats.get("unrecoverable_repos", 0) == 0


class TestCompleteLogMentionsUnrecoverableCount:
    """Bug #1486 High Finding 4: the scheduler's dormant-tick log
    message must distinguish "genuinely all migrated" from "no
    automatically runnable work because N repos are permanently
    unrecoverable" -- an operator reading logs alone should not
    conclude the fleet is fully healthy when repos actually need manual
    intervention."""

    def test_dormant_tick_log_names_unrecoverable_count_when_present(
        self, tmp_path: Path, caplog
    ) -> None:
        import logging

        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        migrated_base = _build_already_migrated_repo(golden_repos_dir, "repo-a-done")
        corrupt_base = _build_unrecoverable_corrupt_repo(
            golden_repos_dir, "repo-b-unrecoverable"
        )
        backend = _make_backend(tmp_path)
        golden = _FakeGoldenRepoManager(
            {"repo-a-done": migrated_base, "repo-b-unrecoverable": corrupt_base},
            sqlite_backend=backend,
        )
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )
        scheduler._run_next_candidate()  # discover + record unrecoverable

        with caplog.at_level(logging.INFO):
            job_id = scheduler.trigger_now()

        assert job_id is None
        assert any(
            "1 repo(s) permanently unrecoverable" in record.message
            for record in caplog.records
        ), (
            "Bug: the dormant-tick log message does not name the exact "
            "unrecoverable repo count -- an operator reading logs alone "
            "cannot distinguish 'all done' from 'stuck, needs manual "
            "recovery'."
        )
