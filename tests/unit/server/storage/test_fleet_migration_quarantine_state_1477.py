"""
Unit tests for the fleet-migration per-repo failure quarantine's persisted
state (Issue #1477).

`FleetMigrationScheduler._run_next_candidate()` always picks the FIRST
not-yet-migrated golden repo (alias-sorted order) with no memory of prior
attempts -- so a repo whose migration throws every single time (e.g.
genuinely corrupt legacy `vector_*.json` data that
`scan_vectors_for_id_map` correctly refuses to auto-resolve) is retried
forever and permanently starves every alphabetically-later repo in the
fleet.

This mirrors the golden-repo registry-reconcile circuit-breaker's persisted
confirmation state (Bug #1382, `golden_repo_reconcile_breaker_state`) --
SAME backend injection convention (`GoldenRepoManager._sqlite_backend`,
SQLite solo / PostgreSQL cluster), new sibling table
`fleet_migration_quarantine_state`, keyed per golden_alias (not a
singleton row, since many repos are tracked independently).
"""

import os
import tempfile

import pytest

from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


@pytest.fixture
def backend():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "test.db")
        be = GoldenRepoMetadataSqliteBackend(db_path)
        be.ensure_table_exists()
        yield be


class TestRecordFleetMigrationFailureFirstObservation:
    def test_first_failure_returns_count_one(self, backend):
        count = backend.record_fleet_migration_failure("click", "sig-1")
        assert count == 1

    def test_get_state_after_first_failure(self, backend):
        backend.record_fleet_migration_failure("click", "sig-1")
        state = backend.get_fleet_migration_failure_state("click")
        assert state is not None
        assert state["golden_alias"] == "click"
        assert state["consecutive_failure_count"] == 1
        assert state["state_signature"] == "sig-1"
        assert state["first_failed_at"] is not None
        assert state["last_failed_at"] is not None

    def test_first_failure_also_stamps_signature_checked_at(self, backend):
        """Finding C (Codex round-3 review): recording a failure ALSO
        establishes 'we just verified the signature as of right now' --
        this is the persisted bookkeeping is_quarantined() throttles its
        expensive full-signature recheck against."""
        backend.record_fleet_migration_failure("click", "sig-1")
        state = backend.get_fleet_migration_failure_state("click")
        assert state is not None
        assert state["signature_checked_at"] is not None

    def test_get_state_returns_none_when_never_failed(self, backend):
        assert backend.get_fleet_migration_failure_state("never-failed") is None


class TestRecordFleetMigrationFailureCause:
    """Finding I (Codex round-5 review): a directory-content signature
    alone cannot observe disk space freed up ELSEWHERE on the filesystem
    -- persisting the FAILURE CAUSE lets `is_quarantined()` distinguish a
    disk-headroom-caused quarantine (clears via a disk-space oracle) from
    a corrupt-data-caused one (clears via the signature)."""

    def test_failure_cause_defaults_to_none_when_omitted(self, backend):
        backend.record_fleet_migration_failure("click", "sig-1")
        state = backend.get_fleet_migration_failure_state("click")
        assert state is not None
        assert state["failure_cause"] is None

    def test_failure_cause_is_persisted_and_returned(self, backend):
        backend.record_fleet_migration_failure(
            "click", "sig-1", failure_cause="disk_headroom"
        )
        state = backend.get_fleet_migration_failure_state("click")
        assert state is not None
        assert state["failure_cause"] == "disk_headroom"

    def test_failure_cause_is_overwritten_on_each_new_failure(self, backend):
        backend.record_fleet_migration_failure(
            "click", "sig-1", failure_cause="disk_headroom"
        )
        backend.record_fleet_migration_failure(
            "click", "sig-2", failure_cause="generic"
        )
        state = backend.get_fleet_migration_failure_state("click")
        assert state is not None
        assert state["failure_cause"] == "generic"

    def test_failure_cause_appears_in_list_states(self, backend):
        backend.record_fleet_migration_failure(
            "click", "sig-1", failure_cause="disk_headroom"
        )
        rows = backend.list_fleet_migration_failure_states()
        by_alias = {row["golden_alias"]: row for row in rows}
        assert by_alias["click"]["failure_cause"] == "disk_headroom"


class TestRecordFleetMigrationFailureAccumulates:
    def test_repeated_failures_increment_count(self, backend):
        assert backend.record_fleet_migration_failure("click", "sig-1") == 1
        assert backend.record_fleet_migration_failure("click", "sig-1") == 2
        assert backend.record_fleet_migration_failure("click", "sig-1") == 3

    def test_signature_is_always_overwritten_to_the_latest(self, backend):
        """The stored signature always reflects the MOST RECENT failure's
        on-disk state -- this is what lets the caller detect a genuine
        state change between the last failure and the next scheduling
        attempt, mirroring description_refresh_scheduler.py's commit-based
        auto-clear gate."""
        backend.record_fleet_migration_failure("click", "sig-1")
        backend.record_fleet_migration_failure("click", "sig-2")
        state = backend.get_fleet_migration_failure_state("click")
        assert state["consecutive_failure_count"] == 2
        assert state["state_signature"] == "sig-2"

    def test_first_failed_at_is_preserved_across_repeated_failures(self, backend):
        backend.record_fleet_migration_failure("click", "sig-1")
        state1 = backend.get_fleet_migration_failure_state("click")
        backend.record_fleet_migration_failure("click", "sig-2")
        state2 = backend.get_fleet_migration_failure_state("click")
        assert state2["first_failed_at"] == state1["first_failed_at"]


class TestFleetMigrationFailureStatesAreIndependentPerAlias:
    def test_two_aliases_track_independent_counters(self, backend):
        backend.record_fleet_migration_failure("click", "sig-1")
        backend.record_fleet_migration_failure("click", "sig-1")
        backend.record_fleet_migration_failure("evolution", "sig-e")

        click_state = backend.get_fleet_migration_failure_state("click")
        evolution_state = backend.get_fleet_migration_failure_state("evolution")
        assert click_state["consecutive_failure_count"] == 2
        assert evolution_state["consecutive_failure_count"] == 1


class TestResetFleetMigrationFailure:
    def test_reset_clears_state_completely(self, backend):
        backend.record_fleet_migration_failure("click", "sig-1")
        backend.record_fleet_migration_failure("click", "sig-1")
        assert (
            backend.get_fleet_migration_failure_state("click")[
                "consecutive_failure_count"
            ]
            == 2
        )

        backend.reset_fleet_migration_failure("click")

        assert backend.get_fleet_migration_failure_state("click") is None
        assert backend.record_fleet_migration_failure("click", "sig-1") == 1

    def test_reset_is_idempotent_when_no_state_exists(self, backend):
        backend.reset_fleet_migration_failure("never-failed")
        assert backend.get_fleet_migration_failure_state("never-failed") is None

    def test_reset_only_affects_the_named_alias(self, backend):
        backend.record_fleet_migration_failure("click", "sig-1")
        backend.record_fleet_migration_failure("evolution", "sig-e")

        backend.reset_fleet_migration_failure("click")

        assert backend.get_fleet_migration_failure_state("click") is None
        assert backend.get_fleet_migration_failure_state("evolution") is not None


class TestSoftResetFleetMigrationFailureCount:
    """Finding N (Codex round-7 review): a fallback used when the full
    reset (DELETE) fails but a plain UPDATE still works -- zeroes
    `consecutive_failure_count` while KEEPING the row (unlike
    `reset_fleet_migration_failure`, which deletes it)."""

    def test_soft_reset_zeroes_count_but_keeps_the_row(self, backend):
        backend.record_fleet_migration_failure("click", "sig-1")
        backend.record_fleet_migration_failure("click", "sig-1")
        assert (
            backend.get_fleet_migration_failure_state("click")[
                "consecutive_failure_count"
            ]
            == 2
        )

        backend.soft_reset_fleet_migration_failure_count("click")

        state = backend.get_fleet_migration_failure_state("click")
        assert state is not None, (
            "soft reset must KEEP the row -- unlike reset_fleet_migration_failure"
        )
        assert state["consecutive_failure_count"] == 0
        assert state["state_signature"] == "sig-1"

    def test_next_failure_after_soft_reset_starts_from_one(self, backend):
        backend.record_fleet_migration_failure("click", "sig-1")
        backend.record_fleet_migration_failure("click", "sig-1")
        backend.record_fleet_migration_failure("click", "sig-1")

        backend.soft_reset_fleet_migration_failure_count("click")

        assert backend.record_fleet_migration_failure("click", "sig-2") == 1

    def test_soft_reset_is_idempotent_when_no_state_exists(self, backend):
        backend.soft_reset_fleet_migration_failure_count("never-failed")
        assert backend.get_fleet_migration_failure_state("never-failed") is None

    def test_soft_reset_only_affects_the_named_alias(self, backend):
        backend.record_fleet_migration_failure("click", "sig-1")
        backend.record_fleet_migration_failure("evolution", "sig-e")
        backend.record_fleet_migration_failure("evolution", "sig-e")

        backend.soft_reset_fleet_migration_failure_count("click")

        assert (
            backend.get_fleet_migration_failure_state("click")[
                "consecutive_failure_count"
            ]
            == 0
        )
        assert (
            backend.get_fleet_migration_failure_state("evolution")[
                "consecutive_failure_count"
            ]
            == 2
        )


class TestTouchFleetMigrationFailureCheck:
    """Finding C (Codex round-3 review): a cheap, standalone way to update
    ONLY the throttle bookkeeping timestamp -- used when `is_quarantined()`
    re-verifies an unchanged signature, so the NEXT recheck window starts
    fresh WITHOUT touching `consecutive_failure_count` or `state_signature`
    (those must only ever change via a genuine new failure or an actual
    detected on-disk change)."""

    def test_touch_updates_signature_checked_at_without_changing_count_or_signature(
        self, backend
    ) -> None:
        backend.record_fleet_migration_failure("click", "sig-1")
        state_before = backend.get_fleet_migration_failure_state("click")
        assert state_before is not None

        backend.touch_fleet_migration_failure_check("click")

        state_after = backend.get_fleet_migration_failure_state("click")
        assert state_after is not None
        assert state_after["consecutive_failure_count"] == 1
        assert state_after["state_signature"] == "sig-1"
        assert state_after["signature_checked_at"] is not None

    def test_touch_is_a_noop_when_no_row_exists(self, backend) -> None:
        # Must never raise -- a missing row is not an error condition.
        backend.touch_fleet_migration_failure_check("never-failed")
        assert backend.get_fleet_migration_failure_state("never-failed") is None


class TestListFleetMigrationFailureStates:
    def test_returns_empty_list_when_nothing_recorded(self, backend):
        assert backend.list_fleet_migration_failure_states() == []

    def test_returns_every_tracked_alias(self, backend):
        backend.record_fleet_migration_failure("click", "sig-1")
        backend.record_fleet_migration_failure("click", "sig-1")
        backend.record_fleet_migration_failure("evolution", "sig-e")

        rows = backend.list_fleet_migration_failure_states()
        by_alias = {row["golden_alias"]: row for row in rows}
        assert set(by_alias) == {"click", "evolution"}
        assert by_alias["click"]["consecutive_failure_count"] == 2
        assert by_alias["evolution"]["consecutive_failure_count"] == 1

    def test_reset_alias_no_longer_appears_in_the_list(self, backend):
        backend.record_fleet_migration_failure("click", "sig-1")
        backend.record_fleet_migration_failure("evolution", "sig-e")

        backend.reset_fleet_migration_failure("click")

        rows = backend.list_fleet_migration_failure_states()
        aliases = {row["golden_alias"] for row in rows}
        assert aliases == {"evolution"}
