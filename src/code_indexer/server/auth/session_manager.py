"""
Session management for JWT token invalidation after password changes.

Implements secure session invalidation to prevent unauthorized access after password changes.
Following CLAUDE.md principles: NO MOCKS - Real session management implementation.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Set, Dict, Optional
from threading import Lock


class PasswordChangeSessionManager:
    """
    Session manager for invalidating JWT tokens after password changes.

    Security requirements:
    - Invalidate all user sessions after password change
    - Maintain blacklist of invalidated tokens
    - Thread-safe implementation
    - Persistent storage for session invalidation data

    Supports both SQLite backend (Story #702) and JSON file storage (backward compatible).
    """

    def __init__(
        self,
        session_file_path: Optional[str] = None,
        use_sqlite: bool = False,
        db_path: Optional[str] = None,
    ):
        """
        Initialize session manager.

        Args:
            session_file_path: Optional custom path for session data file
            use_sqlite: If True, use SQLite backend instead of JSON file (Story #702)
            db_path: Path to SQLite database file (required when use_sqlite=True)
        """
        self._use_sqlite = use_sqlite
        self._sqlite_backend: Optional[Any] = None

        if use_sqlite:
            if db_path is None:
                raise ValueError("db_path is required when use_sqlite=True")
            from code_indexer.server.storage.sqlite_backends import (
                SessionsSqliteBackend,
            )

            self._sqlite_backend = SessionsSqliteBackend(db_path)
        else:
            # JSON file storage (backward compatible)
            if session_file_path:
                self.session_file_path = session_file_path
            else:
                # Default session data location
                server_dir = Path.home() / ".cidx-server"
                server_dir.mkdir(exist_ok=True)
                self.session_file_path = str(server_dir / "invalidated_sessions.json")

            # In-memory cache of invalidated sessions
            self._invalidated_sessions: Dict[str, Set[str]] = {}
            self._password_change_timestamps: Dict[str, str] = {}

            # Load existing session data
            self._load_session_data()

        self._lock = Lock()

    def _load_session_data(self) -> None:
        """Load session invalidation data from persistent storage."""
        try:
            if Path(self.session_file_path).exists():
                with open(self.session_file_path, "r") as f:
                    data = json.load(f)

                # Convert sets from lists (JSON doesn't support sets)
                for username, token_list in data.get(
                    "invalidated_sessions", {}
                ).items():
                    self._invalidated_sessions[username] = set(token_list)

                self._password_change_timestamps = data.get(
                    "password_change_timestamps", {}
                )
        except Exception:
            # If there's any error loading, start with empty data
            self._invalidated_sessions = {}
            self._password_change_timestamps = {}

    def _save_session_data(
        self,
        sessions_snapshot: Optional[Dict[str, Set[str]]] = None,
        timestamps_snapshot: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Save session invalidation data to persistent storage.

        Accepts optional snapshots of the shared dicts captured under the lock.
        When snapshots are provided the method operates entirely on them rather
        than reading the live shared dicts, eliminating the race condition where
        another thread mutates the dicts between lock release and file write
        (RuntimeError: dictionary changed size during iteration).

        Args:
            sessions_snapshot: Pre-captured copy of _invalidated_sessions (under lock)
            timestamps_snapshot: Pre-captured copy of _password_change_timestamps (under lock)
        """
        try:
            # Use provided snapshots, or fall back to live dicts for backward
            # compatibility (e.g., direct internal calls during load).
            sessions = (
                sessions_snapshot
                if sessions_snapshot is not None
                else self._invalidated_sessions
            )
            timestamps = (
                timestamps_snapshot
                if timestamps_snapshot is not None
                else self._password_change_timestamps
            )

            # Convert sets to lists for JSON serialization
            serializable_sessions = {
                username: list(token_set) for username, token_set in sessions.items()
            }

            data = {
                "invalidated_sessions": serializable_sessions,
                "password_change_timestamps": timestamps,
            }

            with open(self.session_file_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            # Log error but don't raise - session invalidation shouldn't break the app
            print(f"Warning: Failed to save session invalidation data: {e}")

    def _snapshot_state(self) -> tuple:
        """
        Capture immutable copies of both shared dicts under the lock.

        Must be called while self._lock is already held.  Returns a
        (sessions_snapshot, timestamps_snapshot) tuple that can be passed
        directly to _save_session_data() outside the lock.
        """
        sessions_snap: Dict[str, Set[str]] = {
            u: set(toks) for u, toks in self._invalidated_sessions.items()
        }
        timestamps_snap: Dict[str, str] = dict(self._password_change_timestamps)
        return sessions_snap, timestamps_snap

    def invalidate_all_user_sessions(self, username: str) -> None:
        """
        Invalidate all sessions for a user after password change.

        Args:
            username: Username whose sessions should be invalidated
        """
        if self._use_sqlite and self._sqlite_backend is not None:
            # SQLite backend (Story #702)
            timestamp = datetime.now(timezone.utc).isoformat()
            self._sqlite_backend.set_password_change_timestamp(username, timestamp)
            self._sqlite_backend.clear_invalidated_sessions(username)
        else:
            # JSON file storage (backward compatible)
            # Mutate in-memory state and snapshot both dicts while holding the
            # lock. The file I/O runs outside the lock using immutable snapshots
            # to prevent "dictionary changed size during iteration" races.
            with self._lock:
                self._password_change_timestamps[username] = datetime.now(
                    timezone.utc
                ).isoformat()
                if username in self._invalidated_sessions:
                    del self._invalidated_sessions[username]
                sessions_snap, timestamps_snap = self._snapshot_state()
            self._save_session_data(sessions_snap, timestamps_snap)

    def is_session_invalid(self, username: str, token_issued_at: datetime) -> bool:
        """
        Check if a session token is invalid due to password change.

        Args:
            username: Username from the token
            token_issued_at: When the token was issued

        Returns:
            True if session is invalid, False otherwise
        """
        if self._use_sqlite and self._sqlite_backend is not None:
            # SQLite backend (Story #702)
            password_change_time_str = (
                self._sqlite_backend.get_password_change_timestamp(username)
            )
            if password_change_time_str:
                try:
                    password_change_time = datetime.fromisoformat(
                        password_change_time_str
                    )
                    return token_issued_at < password_change_time
                except ValueError:
                    return False
            return False
        else:
            # JSON file storage (backward compatible)
            with self._lock:
                if username in self._password_change_timestamps:
                    password_change_time_str = self._password_change_timestamps[
                        username
                    ]
                    try:
                        password_change_time = datetime.fromisoformat(
                            password_change_time_str
                        )
                        return token_issued_at < password_change_time
                    except ValueError:
                        return False
                return False

    def invalidate_specific_token(self, username: str, token_id: str) -> None:
        """
        Invalidate a specific token (for individual session management).

        Args:
            username: Username who owns the token
            token_id: Unique identifier for the token
        """
        if self._use_sqlite and self._sqlite_backend is not None:
            # SQLite backend (Story #702)
            self._sqlite_backend.invalidate_session(username, token_id)
        else:
            # JSON file storage (backward compatible)
            # Mutate in-memory state and snapshot both dicts while holding the
            # lock. File I/O runs outside the lock using immutable snapshots.
            with self._lock:
                if username not in self._invalidated_sessions:
                    self._invalidated_sessions[username] = set()
                self._invalidated_sessions[username].add(token_id)
                sessions_snap, timestamps_snap = self._snapshot_state()
            self._save_session_data(sessions_snap, timestamps_snap)

    def is_token_invalidated(self, username: str, token_id: str) -> bool:
        """
        Check if a specific token has been invalidated.

        Args:
            username: Username from the token
            token_id: Unique identifier for the token

        Returns:
            True if token is invalidated, False otherwise
        """
        if self._use_sqlite and self._sqlite_backend is not None:
            # SQLite backend (Story #702)
            result: bool = self._sqlite_backend.is_session_invalidated(
                username, token_id
            )
            return result
        else:
            # JSON file storage (backward compatible)
            with self._lock:
                if username not in self._invalidated_sessions:
                    return False

                return token_id in self._invalidated_sessions[username]

    def cleanup_old_data(self, days_to_keep: int = 30) -> int:
        """
        Clean up old session invalidation data.

        Args:
            days_to_keep: Number of days of data to keep

        Returns:
            Number of user records cleaned up
        """
        # Story #702 SQLite migration: Add SQLite backend support
        if self._use_sqlite and self._sqlite_backend is not None:
            cleanup_count: int = self._sqlite_backend.cleanup_old_data(days_to_keep)
            return cleanup_count

        # JSON file storage (backward compatible)
        # Mutate in-memory state and snapshot while holding the lock.
        # File I/O runs outside the lock using immutable snapshots.
        sessions_snap: Optional[Dict[str, Set[str]]] = None
        timestamps_snap: Optional[Dict[str, str]] = None
        users_to_remove: list = []

        with self._lock:
            cutoff_time = datetime.now(timezone.utc).timestamp() - (
                days_to_keep * 24 * 3600
            )

            # Check password change timestamps
            for username, timestamp_str in self._password_change_timestamps.items():
                try:
                    change_time = datetime.fromisoformat(timestamp_str)
                    if change_time.timestamp() < cutoff_time:
                        users_to_remove.append(username)
                except ValueError:
                    # Invalid timestamp, remove it
                    users_to_remove.append(username)

            # Remove old data
            for username in users_to_remove:
                if username in self._password_change_timestamps:
                    del self._password_change_timestamps[username]
                if username in self._invalidated_sessions:
                    del self._invalidated_sessions[username]

            # Snapshot after cleanup while still under the lock
            if users_to_remove:
                sessions_snap, timestamps_snap = self._snapshot_state()

        # Write to disk outside the lock using immutable snapshots
        if users_to_remove and sessions_snap is not None and timestamps_snap is not None:
            self._save_session_data(sessions_snap, timestamps_snap)

        return len(users_to_remove)


# Global session manager instance
session_manager = PasswordChangeSessionManager()
