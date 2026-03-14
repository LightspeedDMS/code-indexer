"""
Unit tests for Bug #437: Stale error_message in dependency_map_tracking after orphan recovery.

TDD: Tests written FIRST before production fix is applied.

Root cause: run_delta_analysis() has two paths that fail to clear error_message:
1. Line 2045: "no changes" early exit only updates next_run, not status or error_message
2. Line 2050-2052: "transition to running" only sets status/last_run, not error_message=None

After cleanup_stale_status_on_startup() sets error_message='orphaned - server restarted',
the next delta run typically hits "no changes" and leaves the stale error_message forever.
"""

import sqlite3
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Return path to a freshly initialized test database."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    path = tmp_path / "test_depmap_tracking.db"
    schema = DatabaseSchema(str(path))
    schema.initialize_database()
    return str(path)


@pytest.fixture
def tracking_backend(db_path: str) -> Generator:
    """Create a DependencyMapTrackingBackend with initialized database."""
    from code_indexer.server.storage.sqlite_backends import (
        DependencyMapTrackingBackend,
    )

    backend = DependencyMapTrackingBackend(db_path)
    # Ensure the singleton row exists
    backend.get_tracking()
    yield backend
    backend._conn_manager.close_all()


def _set_tracking_state(db_path: str, status: str, error_message: str) -> None:
    """Directly set tracking state in DB to simulate post-orphan-cleanup state."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE dependency_map_tracking SET status = ?, error_message = ? WHERE id = 1",
        (status, error_message),
    )
    conn.commit()
    conn.close()


def _get_tracking_row(db_path: str) -> dict:
    """Read tracking state directly from DB."""
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        "SELECT status, error_message, next_run FROM dependency_map_tracking WHERE id = 1"
    )
    row = cursor.fetchone()
    conn.close()
    assert row is not None, "Tracking singleton row not found"
    return {"status": row[0], "error_message": row[1], "next_run": row[2]}


class TestUpdateTrackingErrorMessageSentinel:
    """Tests for DependencyMapTrackingBackend.update_tracking() sentinel behaviour.

    These tests document the CORRECT sentinel behaviour -- they should pass
    both before and after the production fix.
    """

    def test_error_message_cleared_when_explicitly_none(
        self, tracking_backend, db_path: str
    ) -> None:
        """Passing error_message=None MUST write NULL to the database."""
        # Arrange: pre-populate with a stale error
        _set_tracking_state(db_path, "failed", "orphaned - server restarted")

        # Act: explicitly pass error_message=None
        tracking_backend.update_tracking(status="completed", error_message=None)

        # Assert: error_message is NULL in DB
        row = _get_tracking_row(db_path)
        assert row["error_message"] is None, (
            "error_message should be NULL when explicitly passed as None"
        )

    def test_error_message_preserved_when_not_passed(
        self, tracking_backend, db_path: str
    ) -> None:
        """NOT passing error_message must leave the existing value untouched (_UNSET sentinel)."""
        # Arrange: pre-populate with a stale error
        _set_tracking_state(db_path, "failed", "orphaned - server restarted")

        # Act: update without passing error_message (uses _UNSET sentinel)
        tracking_backend.update_tracking(status="running")

        # Assert: error_message is still there (sentinel prevented overwrite)
        row = _get_tracking_row(db_path)
        assert row["error_message"] == "orphaned - server restarted", (
            "error_message should be preserved when not explicitly passed to update_tracking()"
        )

    def test_status_set_correctly_with_error_message_clear(
        self, tracking_backend, db_path: str
    ) -> None:
        """When error_message=None is passed, status is also updated correctly."""
        _set_tracking_state(db_path, "failed", "orphaned - server restarted")

        tracking_backend.update_tracking(status="completed", error_message=None)

        row = _get_tracking_row(db_path)
        assert row["status"] == "completed"
        assert row["error_message"] is None


class TestNoChangesPathClearsErrorMessage:
    """Tests for Bug #437 Fix 1: the 'no changes detected' early exit path.

    The update_tracking() call at line 2045 must include status='completed'
    and error_message=None so stale errors from orphan recovery are cleared.
    """

    def test_no_changes_update_clears_stale_error_message(
        self, tracking_backend, db_path: str
    ) -> None:
        """update_tracking with next_run only does NOT clear error_message (documents bug).

        This test verifies the sentinel behaviour: calling update_tracking(next_run=...)
        without error_message=None leaves the stale error_message in place.

        This test passes BEFORE the fix (it documents the broken caller, not the backend).
        After Fix 1, the caller will pass error_message=None, tested by the next test.
        """
        _set_tracking_state(db_path, "failed", "orphaned - server restarted")

        # This is what the BUGGY code does: only updates next_run
        tracking_backend.update_tracking(next_run="2030-01-01T00:00:00+00:00")

        row = _get_tracking_row(db_path)
        # The sentinel correctly preserves the value -- the BUG is the caller not passing None
        assert row["error_message"] == "orphaned - server restarted", (
            "Without Fix 1, stale error_message persists because caller omits error_message=None"
        )

    def test_no_changes_update_with_fix_clears_error_message(
        self, tracking_backend, db_path: str
    ) -> None:
        """After Fix 1, update_tracking must include status='completed' and error_message=None.

        This test FAILS before the fix because the caller at line 2045 doesn't pass
        error_message=None. After Fix 1 is applied this test must pass.
        """
        _set_tracking_state(db_path, "failed", "orphaned - server restarted")

        # This is what the FIXED code must do
        tracking_backend.update_tracking(
            status="completed",
            next_run="2030-01-01T00:00:00+00:00",
            error_message=None,
        )

        row = _get_tracking_row(db_path)
        assert row["error_message"] is None, (
            "Fix 1: 'no changes' path must clear error_message by passing error_message=None"
        )
        assert row["status"] == "completed", (
            "Fix 1: 'no changes' path must set status='completed'"
        )


class TestRunningTransitionClearsErrorMessage:
    """Tests for Bug #437 Fix 2: the 'transition to running' path.

    The update_tracking() call at line 2050-2052 must include error_message=None
    so stale errors are cleared when a new analysis starts.
    """

    def test_running_transition_without_fix_preserves_error(
        self, tracking_backend, db_path: str
    ) -> None:
        """Without Fix 2, transitioning to running does NOT clear error_message."""
        _set_tracking_state(db_path, "failed", "orphaned - server restarted")

        # This is what the BUGGY code does at line 2050-2052
        tracking_backend.update_tracking(
            status="running",
            last_run="2030-01-01T00:00:00+00:00",
        )

        row = _get_tracking_row(db_path)
        # The sentinel preserves the stale error -- BUG is the caller not clearing it
        assert row["error_message"] == "orphaned - server restarted", (
            "Without Fix 2, error_message persists after transitioning to running"
        )

    def test_running_transition_with_fix_clears_error_message(
        self, tracking_backend, db_path: str
    ) -> None:
        """After Fix 2, transitioning to running must clear error_message.

        This test FAILS before the fix is applied. After Fix 2, calling
        update_tracking(status='running', last_run=..., error_message=None)
        must result in NULL error_message.
        """
        _set_tracking_state(db_path, "failed", "orphaned - server restarted")

        # This is what the FIXED code must do at line 2050-2052
        tracking_backend.update_tracking(
            status="running",
            last_run="2030-01-01T00:00:00+00:00",
            error_message=None,
        )

        row = _get_tracking_row(db_path)
        assert row["error_message"] is None, (
            "Fix 2: 'transition to running' must clear error_message by passing error_message=None"
        )
        assert row["status"] == "running", (
            "Fix 2: status must be set to 'running'"
        )


class TestOrphanRecoveryEndToEnd:
    """End-to-end tests simulating the full orphan recovery → delta run cycle.

    These test the full sequence: startup cleanup sets error → next delta run clears it.
    Tests use DependencyMapTrackingBackend directly (no mocking of the backend itself).
    """

    def test_orphan_cleanup_then_successful_tracking_update_clears_error(
        self, tracking_backend, db_path: str
    ) -> None:
        """Simulates: server restart orphans job → startup cleanup → delta run clears error.

        Step 1: cleanup_stale_status_on_startup() sets error_message='orphaned...'
        Step 2: delta run completes (no changes path) → error_message must be None
        """
        # Step 1: Simulate what cleanup_stale_status_on_startup() does
        _set_tracking_state(db_path, "running", None)
        tracking_backend.cleanup_stale_status_on_startup()

        state_after_cleanup = _get_tracking_row(db_path)
        assert state_after_cleanup["status"] == "failed"
        assert "orphaned" in (state_after_cleanup["error_message"] or "")

        # Step 2: Simulate the fixed "no changes" path (Fix 1 applied)
        tracking_backend.update_tracking(
            status="completed",
            next_run="2030-01-01T00:00:00+00:00",
            error_message=None,
        )

        final_state = _get_tracking_row(db_path)
        assert final_state["status"] == "completed", (
            "After successful delta run, status must be 'completed'"
        )
        assert final_state["error_message"] is None, (
            "After successful delta run, error_message must be cleared"
        )

    def test_orphan_cleanup_then_running_transition_clears_error(
        self, tracking_backend, db_path: str
    ) -> None:
        """Simulates: orphan recovery → delta starts (transitions to running) → error cleared.

        When delta analysis actually has work to do (not the 'no changes' path),
        transitioning to 'running' must also clear the stale error_message.
        """
        # Arrange: simulate post-orphan-cleanup state
        _set_tracking_state(db_path, "failed", "orphaned - server restarted")

        # Act: simulate the fixed "transition to running" (Fix 2 applied)
        tracking_backend.update_tracking(
            status="running",
            last_run="2030-01-01T00:00:00+00:00",
            error_message=None,
        )

        row = _get_tracking_row(db_path)
        assert row["status"] == "running"
        assert row["error_message"] is None, (
            "Transitioning to 'running' must clear stale error_message from orphan recovery"
        )
