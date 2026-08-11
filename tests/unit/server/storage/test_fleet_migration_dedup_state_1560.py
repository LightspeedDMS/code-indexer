"""
Unit tests for the Story #1560 duplicate-point-id auto-resolution
outcome persistence methods on `GoldenRepoMetadataSqliteBackend`
(AC6/AC7/AC8/AC9/AC10/AC18).

Mirrors `test_fleet_migration_quarantine_state_1477.py`'s exact fixture
pattern -- a REAL SQLite database file, no mocking of the backend.
`contextlib.closing()` guarantees every backend's connection is closed
via `__exit__`, regardless of what happens inside the block.
"""

import os
import tempfile
from contextlib import closing
from typing import Any, Dict

import pytest

from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend

_DEFAULT_OUTCOME: Dict[str, Any] = {
    "duplicate_groups": 3,
    "records_before": 500,
    "records_deleted": 4,
    "winner_kept_groups": 2,
    "whole_group_deleted_groups": 1,
    "collection_total": 500,
}


def _record(backend, golden_alias: str, **overrides: Any) -> Dict[str, Any]:
    """Record one dedup outcome, defaulting every field from
    `_DEFAULT_OUTCOME` except whatever the caller overrides."""
    kwargs = {**_DEFAULT_OUTCOME, **overrides}
    return backend.record_dedup_outcome(golden_alias, **kwargs)  # type: ignore[no-any-return]


@pytest.fixture
def backend():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "test.db")
        with closing(GoldenRepoMetadataSqliteBackend(db_path)) as be:
            be.ensure_table_exists()
            yield be


@pytest.fixture
def db_path_factory():
    """Yields the underlying db_path so a test can open a SECOND, fresh
    backend instance against the SAME file (AC7's "write through one
    instance, read from a new instance" verification clause)."""
    with tempfile.TemporaryDirectory() as temp_dir:
        path = os.path.join(temp_dir, "test.db")
        with closing(GoldenRepoMetadataSqliteBackend(path)) as initializer:
            initializer.ensure_table_exists()
        yield path


class TestRecordDedupOutcomeFirstObservation:
    def test_first_record_returns_the_recorded_row(self, backend):
        row = _record(
            backend,
            "click",
            duplicate_groups=33,
            records_before=343604,
            records_deleted=43,
            winner_kept_groups=23,
            whole_group_deleted_groups=10,
            collection_total=343604,
        )
        assert row["golden_alias"] == "click"
        assert row["duplicate_groups"] == 33
        assert row["records_before"] == 343604
        assert row["records_deleted"] == 43
        assert row["winner_kept_groups"] == 23
        assert row["whole_group_deleted_groups"] == 10
        assert row["collection_total"] == 343604
        assert row["first_dropped_at"] is not None
        assert row["dropped_at"] is not None
        assert row["cleared_at"] is None
        assert row["cleared_reason"] is None

    def test_get_dedup_state_returns_none_when_never_recorded(self, backend):
        assert backend.get_dedup_state("never-had-a-duplicate") is None


class TestRecordDedupOutcomeCumulativeSemantics:
    """Story #1560 AC9: duplicate_groups/records_deleted/
    winner_kept_groups/whole_group_deleted_groups are CUMULATIVE across
    repeated repair passes; records_before/collection_total are a
    SNAPSHOT of the most recent pass, never summed."""

    def test_second_pass_adds_to_cumulative_counts(self, backend):
        _record(
            backend,
            "evolution",
            duplicate_groups=5,
            records_before=1000,
            records_deleted=6,
            winner_kept_groups=4,
            whole_group_deleted_groups=1,
            collection_total=1000,
        )
        second = _record(
            backend,
            "evolution",
            duplicate_groups=2,
            records_before=1050,
            records_deleted=3,
            winner_kept_groups=1,
            whole_group_deleted_groups=1,
            collection_total=1050,
        )

        assert second["duplicate_groups"] == 7
        assert second["records_deleted"] == 9
        assert second["winner_kept_groups"] == 5
        assert second["whole_group_deleted_groups"] == 2

    def test_second_pass_overwrites_the_snapshot_fields(self, backend):
        _record(
            backend,
            "evolution",
            records_before=1000,
            collection_total=1000,
        )
        second = _record(
            backend,
            "evolution",
            records_before=1050,
            collection_total=1050,
        )

        # NOT 1000 + 1050 -- these are snapshots of the CURRENT collection
        # size, not cumulative across passes.
        assert second["records_before"] == 1050
        assert second["collection_total"] == 1050

    def test_clean_repeat_pass_records_nothing_new(self, backend):
        """AC10: re-running on an already-clean collection (zero new
        duplicates this pass) must not change the persisted counts."""
        first = _record(backend, "click")
        second = _record(
            backend,
            "click",
            duplicate_groups=0,
            records_deleted=0,
            winner_kept_groups=0,
            whole_group_deleted_groups=0,
            records_before=496,
            collection_total=496,
        )

        assert second["duplicate_groups"] == first["duplicate_groups"]
        assert second["records_deleted"] == first["records_deleted"]
        assert second["winner_kept_groups"] == first["winner_kept_groups"]
        assert (
            second["whole_group_deleted_groups"] == first["whole_group_deleted_groups"]
        )
        # The snapshot fields still reflect the latest (post-deletion)
        # collection size.
        assert second["records_before"] == 496
        assert second["collection_total"] == 496


class TestListDedupStates:
    def test_lists_every_recorded_alias(self, backend):
        _record(backend, "repo-a")
        _record(backend, "repo-b")

        rows = backend.list_dedup_states()
        aliases = {row["golden_alias"] for row in rows}
        assert aliases == {"repo-a", "repo-b"}

    def test_empty_when_nothing_recorded(self, backend):
        assert backend.list_dedup_states() == []


class TestClearDedupState:
    def test_clear_sets_cleared_at_and_reason(self, backend):
        _record(backend, "click")

        backend.clear_dedup_state("click", "successful full re-index")

        state = backend.get_dedup_state("click")
        assert state is not None
        assert state["cleared_at"] is not None
        assert state["cleared_reason"] == "successful full re-index"
        # AC8: the counts themselves are NOT erased -- the historical
        # record persists, merely marked cleared.
        assert state["duplicate_groups"] == 3

    def test_clear_on_absent_alias_is_a_no_op(self, backend):
        backend.clear_dedup_state("never-recorded", "n/a")
        assert backend.get_dedup_state("never-recorded") is None

    def test_recording_a_new_outcome_after_clear_reactivates_it(self, backend):
        _record(backend, "click")
        backend.clear_dedup_state("click", "successful full re-index")

        reactivated = _record(backend, "click", duplicate_groups=1)

        assert reactivated["cleared_at"] is None
        assert reactivated["cleared_reason"] is None


class TestWriteThroughOneInstanceReadFromAnother:
    """AC7's explicit verification clause: write through one backend
    instance and read from a NEW instance against the same file."""

    def test_write_then_read_from_a_fresh_instance(self, db_path_factory):
        with closing(GoldenRepoMetadataSqliteBackend(db_path_factory)) as writer:
            _record(
                writer,
                "click",
                duplicate_groups=33,
                records_before=343604,
                records_deleted=43,
                winner_kept_groups=23,
                whole_group_deleted_groups=10,
                collection_total=343604,
            )

        with closing(GoldenRepoMetadataSqliteBackend(db_path_factory)) as reader:
            state = reader.get_dedup_state("click")

        assert state is not None
        assert state["duplicate_groups"] == 33
        assert state["records_deleted"] == 43
        assert state["winner_kept_groups"] == 23
        assert state["whole_group_deleted_groups"] == 10


class TestInputValidation:
    def test_record_rejects_empty_alias(self, backend):
        with pytest.raises(ValueError):
            _record(backend, "")

    def test_get_rejects_non_string_alias(self, backend):
        with pytest.raises(ValueError):
            backend.get_dedup_state(123)  # type: ignore[arg-type]

    def test_clear_rejects_empty_reason(self, backend):
        with pytest.raises(ValueError):
            backend.clear_dedup_state("click", "")
