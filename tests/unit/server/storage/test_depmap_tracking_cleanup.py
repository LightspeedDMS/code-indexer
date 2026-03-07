"""Tests for DependencyMapTrackingBackend.cleanup_stale_status_on_startup().

Bug #381: Dependency map tracking status not reset on server restart.

After a server restart, a singleton row with status 'running' or 'pending'
represents an orphaned job from the previous process. This module tests that
cleanup_stale_status_on_startup() correctly resets stale statuses to 'failed'
so new jobs can be triggered, without disturbing terminal statuses.

Uses real SQLite via tempfile - no mocks.
"""

import os
import tempfile

from code_indexer.server.storage.sqlite_backends import DependencyMapTrackingBackend


def _make_backend_with_status(db_path: str, status: str) -> DependencyMapTrackingBackend:
    """Create a backend and seed the singleton row with the given status."""
    backend = DependencyMapTrackingBackend(db_path)
    # get_tracking() auto-creates the singleton row with status='pending'
    backend.get_tracking()
    # Update to the desired status
    backend.update_tracking(status=status)
    return backend


class TestCleanupStaleStatusOnStartup:
    """Tests for DependencyMapTrackingBackend.cleanup_stale_status_on_startup()."""

    def test_cleanup_resets_running_status(self):
        """Singleton row with status='running' must be reset to 'failed' on startup."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            backend = _make_backend_with_status(db_path, "running")

            backend.cleanup_stale_status_on_startup()

            tracking = backend.get_tracking()
            assert tracking["status"] == "failed", (
                f"Expected 'failed' after cleanup of 'running', got '{tracking['status']}'"
            )
        finally:
            os.unlink(db_path)

    def test_cleanup_sets_error_message_for_running(self):
        """Cleanup of 'running' status must set error_message to indicate server restart."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            backend = _make_backend_with_status(db_path, "running")

            backend.cleanup_stale_status_on_startup()

            tracking = backend.get_tracking()
            assert tracking["error_message"] is not None, "error_message should be set"
            assert "server restarted" in tracking["error_message"].lower(), (
                f"error_message should mention 'server restarted', got: '{tracking['error_message']}'"
            )
        finally:
            os.unlink(db_path)

    def test_cleanup_resets_pending_status(self):
        """Singleton row with status='pending' must be reset to 'failed' on startup."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            backend = _make_backend_with_status(db_path, "pending")

            backend.cleanup_stale_status_on_startup()

            tracking = backend.get_tracking()
            assert tracking["status"] == "failed", (
                f"Expected 'failed' after cleanup of 'pending', got '{tracking['status']}'"
            )
        finally:
            os.unlink(db_path)

    def test_cleanup_sets_error_message_for_pending(self):
        """Cleanup of 'pending' status must set error_message to indicate server restart."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            backend = _make_backend_with_status(db_path, "pending")

            backend.cleanup_stale_status_on_startup()

            tracking = backend.get_tracking()
            assert tracking["error_message"] is not None, "error_message should be set"
            assert "server restarted" in tracking["error_message"].lower(), (
                f"error_message should mention 'server restarted', got: '{tracking['error_message']}'"
            )
        finally:
            os.unlink(db_path)

    def test_cleanup_leaves_completed_alone(self):
        """Singleton row with status='completed' must not be modified."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            backend = _make_backend_with_status(db_path, "completed")

            backend.cleanup_stale_status_on_startup()

            tracking = backend.get_tracking()
            assert tracking["status"] == "completed", (
                f"Expected 'completed' to be unchanged, got '{tracking['status']}'"
            )
        finally:
            os.unlink(db_path)

    def test_cleanup_leaves_failed_alone(self):
        """Singleton row with status='failed' must not be modified."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            backend = _make_backend_with_status(db_path, "failed")
            # Set a specific error message to verify it is not overwritten
            backend.update_tracking(error_message="original error")

            backend.cleanup_stale_status_on_startup()

            tracking = backend.get_tracking()
            assert tracking["status"] == "failed", (
                f"Expected 'failed' to remain unchanged, got '{tracking['status']}'"
            )
            assert tracking["error_message"] == "original error", (
                "error_message should not be overwritten for already-failed row"
            )
        finally:
            os.unlink(db_path)

    def test_cleanup_returns_true_when_running_cleaned(self):
        """cleanup_stale_status_on_startup() must return True when it resets a stale status."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            backend = _make_backend_with_status(db_path, "running")

            result = backend.cleanup_stale_status_on_startup()

            assert result is True, "Should return True when stale 'running' status was cleaned"
        finally:
            os.unlink(db_path)

    def test_cleanup_returns_true_when_pending_cleaned(self):
        """cleanup_stale_status_on_startup() must return True when it resets a stale status."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            backend = _make_backend_with_status(db_path, "pending")

            result = backend.cleanup_stale_status_on_startup()

            assert result is True, "Should return True when stale 'pending' status was cleaned"
        finally:
            os.unlink(db_path)

    def test_cleanup_returns_false_for_completed(self):
        """cleanup_stale_status_on_startup() must return False for terminal 'completed' status."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            backend = _make_backend_with_status(db_path, "completed")

            result = backend.cleanup_stale_status_on_startup()

            assert result is False, "Should return False when status is already 'completed'"
        finally:
            os.unlink(db_path)

    def test_cleanup_returns_false_for_failed(self):
        """cleanup_stale_status_on_startup() must return False for terminal 'failed' status."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            backend = _make_backend_with_status(db_path, "failed")

            result = backend.cleanup_stale_status_on_startup()

            assert result is False, "Should return False when status is already 'failed'"
        finally:
            os.unlink(db_path)

    def test_cleanup_returns_false_when_no_singleton_row(self):
        """cleanup_stale_status_on_startup() must return False when no singleton row exists yet."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            # Create backend but do NOT call get_tracking() - no row in DB
            backend = DependencyMapTrackingBackend(db_path)
            # Ensure the table exists so the SELECT doesn't error
            backend._ensure_run_history_table()

            result = backend.cleanup_stale_status_on_startup()

            assert result is False, "Should return False when no singleton row exists"
        finally:
            os.unlink(db_path)


class TestAppStartupCallsCleanup:
    """Source inspection test: verify app.py calls cleanup after creating tracking backend."""

    def test_startup_calls_cleanup_after_tracking_backend_creation(self):
        """app.py must call cleanup_stale_status_on_startup() after creating DependencyMapTrackingBackend.

        This is a source inspection test - reads app.py and verifies the call
        exists in the correct position relative to the backend creation.
        """
        app_path = os.path.join(
            os.path.dirname(__file__),
            "..", "..", "..", "..", "src", "code_indexer", "server", "app.py"
        )
        app_path = os.path.normpath(app_path)
        assert os.path.isfile(app_path), f"app.py not found at {app_path}"

        with open(app_path, "r") as f:
            source = f.read()

        # Verify both the backend creation and cleanup call exist
        assert "DependencyMapTrackingBackend(db_path)" in source, (
            "app.py must create DependencyMapTrackingBackend(db_path)"
        )
        assert "cleanup_stale_status_on_startup()" in source, (
            "app.py must call cleanup_stale_status_on_startup() on startup"
        )

        # Verify cleanup is called AFTER backend creation (positional check)
        backend_creation_pos = source.index("DependencyMapTrackingBackend(db_path)")
        cleanup_call_pos = source.index("cleanup_stale_status_on_startup()")
        assert cleanup_call_pos > backend_creation_pos, (
            "cleanup_stale_status_on_startup() must appear after DependencyMapTrackingBackend creation in app.py"
        )
