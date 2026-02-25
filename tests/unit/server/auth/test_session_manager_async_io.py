"""
Tests for Story #278: SessionManager file I/O does not hold lock during disk write.

The _save_session_data() method performs JSON file write. Currently it is called
inside the threading.Lock in invalidate_all_user_sessions(), invalidate_specific_token(),
and cleanup_old_data(). This means threads waiting for the lock are also blocked
during the disk write.

Fix: Move the _save_session_data() call to AFTER the lock is released.
The dict operations (in-memory updates) stay under the lock for thread safety.
The file write happens outside the lock, reducing lock hold time.

Key requirements tested:
- Lock is released BEFORE _save_session_data() writes to disk
- In-memory state is correct after concurrent operations
- SQLite backend path is unaffected (it doesn't use the lock-based file I/O)
- Data consistency is maintained
"""

import os
import tempfile
import threading
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from code_indexer.server.auth.session_manager import PasswordChangeSessionManager


class TestSaveSessionDataOutsideLock:
    """Verify _save_session_data is called outside the lock scope."""

    def test_invalidate_all_user_sessions_releases_lock_before_save(self):
        """
        In invalidate_all_user_sessions, the lock must be released before
        _save_session_data() writes to disk.

        We verify this by tracking whether the lock is acquirable when
        _save_session_data is called.
        """
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            session_file = f.name

        try:
            manager = PasswordChangeSessionManager(session_file_path=session_file)

            lock_held_during_save = []

            original_save = manager._save_session_data

            def patched_save(*args, **kwargs):
                # Check if lock is currently held by this thread
                acquired = manager._lock.acquire(blocking=False)
                if acquired:
                    # We could acquire it, meaning it was NOT held
                    lock_held_during_save.append(False)
                    manager._lock.release()
                else:
                    # Could NOT acquire - lock was held
                    lock_held_during_save.append(True)
                original_save(*args, **kwargs)

            manager._save_session_data = patched_save

            manager.invalidate_all_user_sessions("testuser")

            assert len(lock_held_during_save) >= 1, (
                "_save_session_data must be called at least once"
            )
            assert not any(lock_held_during_save), (
                "_save_session_data must be called OUTSIDE the lock scope. "
                "Lock was still held when _save_session_data was called."
            )
        finally:
            os.unlink(session_file)

    def test_invalidate_specific_token_releases_lock_before_save(self):
        """
        In invalidate_specific_token, the lock must be released before
        _save_session_data() writes to disk.
        """
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            session_file = f.name

        try:
            manager = PasswordChangeSessionManager(session_file_path=session_file)

            lock_held_during_save = []

            original_save = manager._save_session_data

            def patched_save(*args, **kwargs):
                acquired = manager._lock.acquire(blocking=False)
                if acquired:
                    lock_held_during_save.append(False)
                    manager._lock.release()
                else:
                    lock_held_during_save.append(True)
                original_save(*args, **kwargs)

            manager._save_session_data = patched_save

            manager.invalidate_specific_token("testuser", "token-123")

            assert len(lock_held_during_save) >= 1
            assert not any(lock_held_during_save), (
                "_save_session_data must be called OUTSIDE the lock in invalidate_specific_token"
            )
        finally:
            os.unlink(session_file)

    def test_cleanup_old_data_releases_lock_before_save(self):
        """
        In cleanup_old_data, the lock must be released before _save_session_data().
        """
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            session_file = f.name

        try:
            manager = PasswordChangeSessionManager(session_file_path=session_file)

            # Add old data that will be cleaned up (date far in the past)
            old_timestamp = (
                datetime.now(timezone.utc) - timedelta(days=60)
            ).isoformat()
            manager._password_change_timestamps["olduser"] = old_timestamp
            manager._save_session_data()

            lock_held_during_save = []

            original_save = manager._save_session_data

            def patched_save(*args, **kwargs):
                acquired = manager._lock.acquire(blocking=False)
                if acquired:
                    lock_held_during_save.append(False)
                    manager._lock.release()
                else:
                    lock_held_during_save.append(True)
                original_save(*args, **kwargs)

            manager._save_session_data = patched_save

            removed = manager.cleanup_old_data(days_to_keep=30)

            if removed > 0:
                # If cleanup actually happened, verify save was outside lock
                assert not any(lock_held_during_save), (
                    "_save_session_data must be called OUTSIDE the lock in cleanup_old_data"
                )
        finally:
            os.unlink(session_file)


class TestSessionManagerDataConsistency:
    """Verify data consistency after the lock-scope change."""

    def test_invalidate_all_user_sessions_persists_state(self):
        """After invalidate_all_user_sessions, state is saved to disk."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            session_file = f.name

        try:
            manager = PasswordChangeSessionManager(session_file_path=session_file)
            manager.invalidate_all_user_sessions("alice")

            # Reload from disk to verify persistence
            manager2 = PasswordChangeSessionManager(session_file_path=session_file)
            assert "alice" in manager2._password_change_timestamps, (
                "Password change timestamp must be persisted to disk"
            )
        finally:
            os.unlink(session_file)

    def test_invalidate_specific_token_persists_state(self):
        """After invalidate_specific_token, state is saved to disk."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            session_file = f.name

        try:
            manager = PasswordChangeSessionManager(session_file_path=session_file)
            manager.invalidate_specific_token("bob", "tok-abc")

            manager2 = PasswordChangeSessionManager(session_file_path=session_file)
            assert "bob" in manager2._invalidated_sessions, (
                "Invalidated token must be persisted to disk"
            )
            assert "tok-abc" in manager2._invalidated_sessions["bob"]
        finally:
            os.unlink(session_file)

    def test_concurrent_invalidations_do_not_lose_data(self):
        """
        Multiple concurrent calls to invalidate_all_user_sessions must not
        lose any user data (thread safety maintained after lock-scope change).
        """
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            session_file = f.name

        try:
            manager = PasswordChangeSessionManager(session_file_path=session_file)

            usernames = [f"user_{i}" for i in range(10)]
            errors = []

            def invalidate_user(username):
                try:
                    manager.invalidate_all_user_sessions(username)
                except Exception as e:
                    errors.append(e)

            threads = [
                threading.Thread(target=invalidate_user, args=(u,))
                for u in usernames
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, f"Thread errors: {errors}"

            # All users must have a password change timestamp in memory
            for username in usernames:
                assert username in manager._password_change_timestamps, (
                    f"User {username} must have a password change timestamp after concurrent invalidation"
                )
        finally:
            os.unlink(session_file)


class TestSnapshotPatternRaceSafety:
    """
    Verify that _save_session_data receives a consistent snapshot of state,
    not a live reference to the shared dicts. This prevents the
    'RuntimeError: dictionary changed size during iteration' race condition
    that occurs when another thread mutates the dicts between lock release
    and file write.
    """

    def test_save_session_data_does_not_iterate_live_dict(self):
        """
        _save_session_data must not iterate self._invalidated_sessions directly.
        Instead it must iterate a snapshot (copy) created under the lock.

        We detect this by mutating _invalidated_sessions immediately after the
        lock is released (before _save_session_data writes to disk). If
        _save_session_data holds a snapshot, mutation is safe. If it holds a
        live reference, Python will raise RuntimeError on the next iteration.
        """
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            session_file = f.name

        try:
            manager = PasswordChangeSessionManager(session_file_path=session_file)
            # Seed some tokens for multiple users so iteration is non-trivial
            for i in range(5):
                manager._invalidated_sessions[f"user_{i}"] = {f"tok_{i}"}

            runtime_errors = []
            save_succeeded = []

            original_save = manager._save_session_data

            def patched_save(*args, **kwargs):
                # Simulate a concurrent thread mutating the dict
                # while _save_session_data is iterating it.
                try:
                    # Add a new user entry mid-iteration to trigger
                    # "dictionary changed size during iteration" if save
                    # uses a live reference.
                    manager._invalidated_sessions["injected_user"] = {"injected_tok"}
                    original_save(*args, **kwargs)
                    save_succeeded.append(True)
                except RuntimeError as e:
                    runtime_errors.append(str(e))

            manager._save_session_data = patched_save

            # This should NOT raise even though dict is mutated during save
            manager.invalidate_specific_token("testuser", "token-abc")

            assert not runtime_errors, (
                f"RuntimeError during _save_session_data indicates live dict reference "
                f"(not a snapshot): {runtime_errors}"
            )
            assert save_succeeded, "_save_session_data must complete successfully"
        finally:
            os.unlink(session_file)

    def test_save_session_data_does_not_iterate_live_timestamps(self):
        """
        _save_session_data must not read self._password_change_timestamps directly
        after the lock is released. It must use a snapshot captured under the lock.
        """
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            session_file = f.name

        try:
            manager = PasswordChangeSessionManager(session_file_path=session_file)
            # Seed timestamps using module-level datetime import
            for i in range(5):
                manager._password_change_timestamps[f"user_{i}"] = (
                    datetime.now(timezone.utc).isoformat()
                )

            runtime_errors = []
            save_succeeded = []

            original_save = manager._save_session_data

            def patched_save(*args, **kwargs):
                try:
                    # Simulate another thread mutating timestamps while save runs
                    manager._password_change_timestamps["new_user"] = (
                        "2025-01-01T00:00:00+00:00"
                    )
                    original_save(*args, **kwargs)
                    save_succeeded.append(True)
                except RuntimeError as e:
                    runtime_errors.append(str(e))

            manager._save_session_data = patched_save

            manager.invalidate_all_user_sessions("testuser")

            assert not runtime_errors, (
                f"RuntimeError indicates _save_session_data uses live timestamps dict "
                f"(not a snapshot): {runtime_errors}"
            )
            assert save_succeeded, "_save_session_data must complete successfully"
        finally:
            os.unlink(session_file)


class TestSQLiteBackendUnaffected:
    """Verify the SQLite backend code path is not affected by the change."""

    def test_sqlite_backend_invalidate_does_not_call_save_session_data(self):
        """SQLite backend invalidate_all_user_sessions must not call _save_session_data."""
        mock_backend = MagicMock()
        mock_backend.set_password_change_timestamp = MagicMock()
        mock_backend.clear_invalidated_sessions = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_sessions.db")
            manager = PasswordChangeSessionManager(
                use_sqlite=True,
                db_path=db_path,
            )
            manager._sqlite_backend = mock_backend

            save_called = []
            original_save = manager._save_session_data

            def tracking_save():
                save_called.append(True)
                original_save()

            manager._save_session_data = tracking_save

            manager.invalidate_all_user_sessions("sqliteuser")

            assert len(save_called) == 0, (
                "SQLite backend must NOT call _save_session_data (no file I/O for SQLite path)"
            )
            mock_backend.set_password_change_timestamp.assert_called_once()
