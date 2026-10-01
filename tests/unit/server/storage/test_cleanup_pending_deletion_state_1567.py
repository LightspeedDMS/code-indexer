"""Bug #1567: durable pending-deletion queue backing GoldenRepoMetadataBackend.

CleanupManager's pending-deletion queue (global_repos/cleanup_manager.py)
used to live ONLY in per-process dictionaries keyed by time.monotonic() --
any restart/worker-recycle silently discarded a scheduled deletion, and
nothing ever reaped the orphan afterward. These tests exercise the new
schedule_cleanup_deletion/list_cleanup_pending_deletions/
remove_cleanup_pending_deletion methods on GoldenRepoMetadataSqliteBackend
against a REAL SQLite file (no mocking of the store) to prove the queue is
now durable and keyed on a WALL-CLOCK timestamp that has meaning across
process restarts.
"""

from __future__ import annotations

from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


def _make_backend(tmp_path):
    db_path = str(tmp_path / "golden_repos_metadata.db")
    backend = GoldenRepoMetadataSqliteBackend(db_path)
    backend.ensure_table_exists()
    return backend


def test_schedule_cleanup_deletion_inserts_new_row(tmp_path):
    backend = _make_backend(tmp_path)

    returned = backend.schedule_cleanup_deletion("/versioned/repo/v_1000", 1000.0)

    assert returned == 1000.0
    rows = backend.list_cleanup_pending_deletions()
    assert rows == [{"index_path": "/versioned/repo/v_1000", "scheduled_at": 1000.0}]


def test_schedule_cleanup_deletion_is_idempotent_and_preserves_original_scheduled_at(
    tmp_path,
):
    """A re-schedule of an already-queued path must NOT reset its age --
    the ORIGINAL supersession moment is what scheduled_at must mean."""
    backend = _make_backend(tmp_path)

    first = backend.schedule_cleanup_deletion("/versioned/repo/v_1000", 1000.0)
    second = backend.schedule_cleanup_deletion("/versioned/repo/v_1000", 9999.0)

    assert first == 1000.0
    assert second == 1000.0, "re-scheduling must return the ORIGINAL scheduled_at"
    rows = backend.list_cleanup_pending_deletions()
    assert len(rows) == 1
    assert rows[0]["scheduled_at"] == 1000.0


def test_list_cleanup_pending_deletions_returns_every_row(tmp_path):
    backend = _make_backend(tmp_path)

    backend.schedule_cleanup_deletion("/versioned/repo/v_1", 1.0)
    backend.schedule_cleanup_deletion("/versioned/repo/v_2", 2.0)
    backend.schedule_cleanup_deletion("/versioned/repo/v_3", 3.0)

    rows = backend.list_cleanup_pending_deletions()
    paths = {row["index_path"] for row in rows}
    assert paths == {
        "/versioned/repo/v_1",
        "/versioned/repo/v_2",
        "/versioned/repo/v_3",
    }


def test_remove_cleanup_pending_deletion_deletes_the_row(tmp_path):
    backend = _make_backend(tmp_path)
    backend.schedule_cleanup_deletion("/versioned/repo/v_1000", 1000.0)

    backend.remove_cleanup_pending_deletion("/versioned/repo/v_1000")

    assert backend.list_cleanup_pending_deletions() == []


def test_remove_cleanup_pending_deletion_is_idempotent_when_absent(tmp_path):
    backend = _make_backend(tmp_path)

    # Must not raise even though the row was never scheduled.
    backend.remove_cleanup_pending_deletion("/versioned/repo/never-scheduled")

    assert backend.list_cleanup_pending_deletions() == []


def test_pending_deletions_survive_a_fresh_backend_instance_against_the_same_db(
    tmp_path,
):
    """Simulates a process restart: a second backend instance opened
    against the SAME on-disk SQLite file must see rows written by the
    first instance."""
    db_path = str(tmp_path / "golden_repos_metadata.db")

    backend_before_restart = GoldenRepoMetadataSqliteBackend(db_path)
    backend_before_restart.ensure_table_exists()
    backend_before_restart.schedule_cleanup_deletion("/versioned/repo/v_42", 42.0)

    backend_after_restart = GoldenRepoMetadataSqliteBackend(db_path)
    backend_after_restart.ensure_table_exists()

    rows = backend_after_restart.list_cleanup_pending_deletions()
    assert rows == [{"index_path": "/versioned/repo/v_42", "scheduled_at": 42.0}]
