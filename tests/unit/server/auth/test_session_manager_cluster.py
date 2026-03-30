"""
Tests for Bug #575: PasswordChangeSessionManager cluster support.

Verifies that set_backend() correctly switches from JSON file mode to
protocol-backed mode (SQLite or PostgreSQL), and that all operations
delegate to the provided backend.
"""

from datetime import datetime, timezone
from typing import Optional

from code_indexer.server.auth.session_manager import PasswordChangeSessionManager


class FakeSessionsBackend:
    """Minimal SessionsBackend protocol implementation for testing.

    Records all calls for assertion without any real storage.
    """

    def __init__(self) -> None:
        self.calls: list = []
        self._timestamps: dict = {}
        self._invalidated: dict = {}

    def invalidate_session(self, username: str, token_id: str) -> None:
        self.calls.append(("invalidate_session", username, token_id))
        self._invalidated.setdefault(username, set()).add(token_id)

    def is_session_invalidated(self, username: str, token_id: str) -> bool:
        self.calls.append(("is_session_invalidated", username, token_id))
        return token_id in self._invalidated.get(username, set())

    def clear_invalidated_sessions(self, username: str) -> None:
        self.calls.append(("clear_invalidated_sessions", username))
        self._invalidated.pop(username, None)

    def set_password_change_timestamp(self, username: str, changed_at: str) -> None:
        self.calls.append(("set_password_change_timestamp", username, changed_at))
        self._timestamps[username] = changed_at

    def get_password_change_timestamp(self, username: str) -> Optional[str]:
        self.calls.append(("get_password_change_timestamp", username))
        return self._timestamps.get(username)

    def cleanup_old_data(self, days_to_keep: int = 30) -> int:
        self.calls.append(("cleanup_old_data", days_to_keep))
        return 0

    def close(self) -> None:
        self.calls.append(("close",))


class TestDefaultJsonFileMode:
    """Verify default singleton uses JSON file mode."""

    def test_default_is_json_file_mode(self) -> None:
        mgr = PasswordChangeSessionManager()
        assert mgr._use_sqlite is False
        assert mgr._sqlite_backend is None


class TestSetBackendSwitchesMode:
    """Verify set_backend() switches internal state."""

    def test_set_backend_switches_to_backend_mode(self) -> None:
        mgr = PasswordChangeSessionManager()
        backend = FakeSessionsBackend()

        mgr.set_backend(backend)

        assert mgr._use_sqlite is True
        assert mgr._sqlite_backend is backend


class TestInvalidateDelegatesToBackend:
    """Verify invalidate_all_user_sessions delegates to backend after set_backend()."""

    def test_invalidate_delegates_to_backend(self) -> None:
        mgr = PasswordChangeSessionManager()
        backend = FakeSessionsBackend()
        mgr.set_backend(backend)

        mgr.invalidate_all_user_sessions("alice")

        # Should have called set_password_change_timestamp and clear_invalidated_sessions
        call_names = [c[0] for c in backend.calls]
        assert "set_password_change_timestamp" in call_names
        assert "clear_invalidated_sessions" in call_names
        # Verify username passed correctly
        ts_call = [c for c in backend.calls if c[0] == "set_password_change_timestamp"][
            0
        ]
        assert ts_call[1] == "alice"


class TestIsSessionInvalidDelegatesToBackend:
    """Verify is_session_invalid delegates to backend after set_backend()."""

    def test_is_session_invalid_delegates_to_backend(self) -> None:
        mgr = PasswordChangeSessionManager()
        backend = FakeSessionsBackend()
        mgr.set_backend(backend)

        # Set a password change timestamp in the future
        future_ts = datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat()
        backend._timestamps["bob"] = future_ts

        # Token issued before the password change -> should be invalid
        token_issued = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = mgr.is_session_invalid("bob", token_issued)

        assert result is True
        call_names = [c[0] for c in backend.calls]
        assert "get_password_change_timestamp" in call_names

    def test_is_session_invalid_returns_false_when_no_change(self) -> None:
        mgr = PasswordChangeSessionManager()
        backend = FakeSessionsBackend()
        mgr.set_backend(backend)

        # No password change timestamp set
        token_issued = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = mgr.is_session_invalid("charlie", token_issued)

        assert result is False


class TestSpecificTokenDelegatesToBackend:
    """Verify token-level operations delegate to backend after set_backend()."""

    def test_invalidate_specific_token_delegates(self) -> None:
        mgr = PasswordChangeSessionManager()
        backend = FakeSessionsBackend()
        mgr.set_backend(backend)

        mgr.invalidate_specific_token("dave", "token-123")

        assert ("invalidate_session", "dave", "token-123") in backend.calls

    def test_is_token_invalidated_delegates(self) -> None:
        mgr = PasswordChangeSessionManager()
        backend = FakeSessionsBackend()
        mgr.set_backend(backend)

        # Not invalidated yet
        assert mgr.is_token_invalidated("dave", "token-456") is False

        # Invalidate and check again
        mgr.invalidate_specific_token("dave", "token-456")
        assert mgr.is_token_invalidated("dave", "token-456") is True


class TestCleanupDelegatesToBackend:
    """Verify cleanup_old_data delegates to backend after set_backend()."""

    def test_cleanup_delegates_to_backend(self) -> None:
        mgr = PasswordChangeSessionManager()
        backend = FakeSessionsBackend()
        mgr.set_backend(backend)

        result = mgr.cleanup_old_data(days_to_keep=7)

        assert result == 0
        assert ("cleanup_old_data", 7) in backend.calls
