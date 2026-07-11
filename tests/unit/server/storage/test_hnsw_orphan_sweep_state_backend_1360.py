"""Tests for the HNSW orphan repair sweep durable state backend
(Story #1360, Epic #1333 S3).

Real SQLite throughout (no mocks) -- this is the "cluster-atomic" cursor/
pass-stats store the scheduler persists to after EVERY item (AC1), so its
durability across process/instance boundaries is the property under test,
not just in-memory bookkeeping.
"""

from pathlib import Path

import pytest

from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import (
    HNSWOrphanSweepStateSqliteBackend,
)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "cidx_server.db")
    DatabaseSchema(path).initialize_database()
    return path


class TestDefaultState:
    def test_fresh_db_returns_zeroed_default_state(self, db_path: str) -> None:
        backend = HNSWOrphanSweepStateSqliteBackend(db_path)
        state = backend.get_state()

        assert state["pass_id"] == 1
        assert state["last_completed_key"] is None
        assert state["pass_indexes_checked"] == 0
        assert state["pass_orphaned_found"] == 0
        assert state["pass_repaired"] == 0
        assert state["pass_errors"] == 0
        assert state["pass_transient_skips"] == 0
        assert state["last_full_pass_completed_at"] is None
        assert state["total_orphans_repaired_lifetime"] == 0


class TestRecordItemProcessed:
    def test_clean_outcome_advances_cursor_and_checked_count(
        self, db_path: str
    ) -> None:
        backend = HNSWOrphanSweepStateSqliteBackend(db_path)
        backend.record_item_processed("golden:alpha:x", "clean")

        state = backend.get_state()
        assert state["last_completed_key"] == "golden:alpha:x"
        assert state["pass_indexes_checked"] == 1
        assert state["pass_orphaned_found"] == 0
        assert state["pass_repaired"] == 0

    def test_repaired_outcome_increments_orphaned_and_repaired_counters(
        self, db_path: str
    ) -> None:
        backend = HNSWOrphanSweepStateSqliteBackend(db_path)
        backend.record_item_processed("golden:alpha:x", "repaired")

        state = backend.get_state()
        assert state["pass_orphaned_found"] == 1
        assert state["pass_repaired"] == 1
        # Lifetime total only accrues at complete_pass(), not per-item.
        assert state["total_orphans_repaired_lifetime"] == 0

    def test_error_outcome_increments_error_counter(self, db_path: str) -> None:
        backend = HNSWOrphanSweepStateSqliteBackend(db_path)
        backend.record_item_processed("golden:alpha:x", "error")

        state = backend.get_state()
        assert state["pass_errors"] == 1

    def test_transient_skip_outcome_increments_transient_counter(
        self, db_path: str
    ) -> None:
        backend = HNSWOrphanSweepStateSqliteBackend(db_path)
        backend.record_item_processed("golden:alpha:x", "transient_skip")

        state = backend.get_state()
        assert state["pass_transient_skips"] == 1

    def test_unknown_outcome_raises_value_error(self, db_path: str) -> None:
        backend = HNSWOrphanSweepStateSqliteBackend(db_path)
        with pytest.raises(ValueError):
            backend.record_item_processed("golden:alpha:x", "bogus")

    def test_multiple_items_accumulate_and_cursor_tracks_latest(
        self, db_path: str
    ) -> None:
        backend = HNSWOrphanSweepStateSqliteBackend(db_path)
        backend.record_item_processed("golden:a:1", "clean")
        backend.record_item_processed("golden:a:2", "repaired")
        backend.record_item_processed("golden:a:3", "error")

        state = backend.get_state()
        assert state["last_completed_key"] == "golden:a:3"
        assert state["pass_indexes_checked"] == 3
        assert state["pass_repaired"] == 1
        assert state["pass_errors"] == 1

    def test_persists_immediately_visible_to_a_second_backend_instance(
        self, db_path: str
    ) -> None:
        """AC1: cursor advances durably after EACH item -- a second backend
        instance over the SAME db file (simulating a restart or another
        process) must see the persisted value, not an in-memory cache."""
        writer = HNSWOrphanSweepStateSqliteBackend(db_path)
        writer.record_item_processed("golden:a:1", "clean")

        reader = HNSWOrphanSweepStateSqliteBackend(db_path)
        state = reader.get_state()
        assert state["last_completed_key"] == "golden:a:1"


class TestCompletePass:
    def test_complete_pass_resets_cursor_and_per_pass_counters(
        self, db_path: str
    ) -> None:
        backend = HNSWOrphanSweepStateSqliteBackend(db_path)
        backend.record_item_processed("golden:a:1", "repaired")
        backend.record_item_processed("golden:a:2", "clean")

        backend.complete_pass()

        state = backend.get_state()
        assert state["last_completed_key"] is None
        assert state["pass_indexes_checked"] == 0
        assert state["pass_orphaned_found"] == 0
        assert state["pass_repaired"] == 0
        assert state["pass_errors"] == 0
        assert state["pass_transient_skips"] == 0

    def test_complete_pass_increments_pass_id(self, db_path: str) -> None:
        backend = HNSWOrphanSweepStateSqliteBackend(db_path)
        backend.complete_pass()
        assert backend.get_state()["pass_id"] == 2
        backend.complete_pass()
        assert backend.get_state()["pass_id"] == 3

    def test_complete_pass_accrues_lifetime_repaired_total(self, db_path: str) -> None:
        backend = HNSWOrphanSweepStateSqliteBackend(db_path)
        backend.record_item_processed("golden:a:1", "repaired")
        backend.record_item_processed("golden:a:2", "repaired")
        backend.complete_pass()

        assert backend.get_state()["total_orphans_repaired_lifetime"] == 2

        backend.record_item_processed("golden:b:1", "repaired")
        backend.complete_pass()

        assert backend.get_state()["total_orphans_repaired_lifetime"] == 3

    def test_complete_pass_records_last_full_pass_completed_at(
        self, db_path: str
    ) -> None:
        backend = HNSWOrphanSweepStateSqliteBackend(db_path)
        assert backend.get_state()["last_full_pass_completed_at"] is None

        backend.complete_pass()

        assert backend.get_state()["last_full_pass_completed_at"] is not None
