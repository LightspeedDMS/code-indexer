"""
Elevated Session Manager (Story #923 AC1).

Tracks TOTP step-up elevation windows. Dual-mode: SQLite (solo) or
PostgreSQL (cluster). The PostgreSQL path mirrors MfaChallengeManager
(mfa_challenge.py) exactly — set_connection_pool() wires the PG pool.
"""

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    from psycopg.rows import dict_row
except ImportError:  # psycopg3 not installed (standalone mode)
    dict_row = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

_DEFAULT_IDLE_TIMEOUT = 300  # 5 minutes
_DEFAULT_MAX_AGE = 1800  # 30 minutes

_VALID_SCOPES = frozenset({"full", "totp_repair"})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS elevated_sessions (
    session_key TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    elevated_at REAL NOT NULL,
    last_touched_at REAL NOT NULL,
    elevated_from_ip TEXT,
    scope TEXT DEFAULT 'full'
);

CREATE INDEX IF NOT EXISTS idx_elevated_sessions_last_touched
    ON elevated_sessions(last_touched_at);

CREATE INDEX IF NOT EXISTS idx_elevated_sessions_username
    ON elevated_sessions(username);
"""

# PostgreSQL equivalents use %s placeholders (psycopg3 style).
_PG_UPSERT = """
INSERT INTO elevated_sessions
    (session_key, username, elevated_at, last_touched_at, elevated_from_ip, scope)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (session_key) DO UPDATE SET
    username = EXCLUDED.username,
    elevated_at = EXCLUDED.elevated_at,
    last_touched_at = EXCLUDED.last_touched_at,
    elevated_from_ip = EXCLUDED.elevated_from_ip,
    scope = EXCLUDED.scope
"""

_PG_TOUCH = """
UPDATE elevated_sessions
SET last_touched_at = %s
WHERE session_key = %s
  AND last_touched_at > %s
  AND elevated_at > %s
RETURNING session_key, username, elevated_at, last_touched_at, elevated_from_ip, scope
"""

_PG_SELECT_VALID = """
SELECT session_key, username, elevated_at, last_touched_at, elevated_from_ip, scope
FROM elevated_sessions
WHERE session_key = %s
  AND last_touched_at > %s
  AND elevated_at > %s
"""

_PG_TOUCH_FOR_USER = """
UPDATE elevated_sessions
SET last_touched_at = %s
WHERE session_key = %s
  AND username = %s
  AND last_touched_at > %s
  AND elevated_at > %s
RETURNING session_key, username, elevated_at, last_touched_at, elevated_from_ip, scope
"""

_PG_DELETE_KEY = "DELETE FROM elevated_sessions WHERE session_key = %s"
_PG_DELETE_USER = "DELETE FROM elevated_sessions WHERE username = %s"


@dataclass
class ElevatedSession:
    """An active step-up elevation window."""

    session_key: str
    username: str
    elevated_at: float
    last_touched_at: float
    elevated_from_ip: Optional[str] = None
    scope: str = field(default="full")


def _row_to_elevated_session(row: Any) -> ElevatedSession:
    """Convert a row-like object (sqlite3.Row or psycopg3 row) to ElevatedSession.

    Both backends return objects supporting dict-style key access, so this
    single helper eliminates duplication between the SQLite and PG paths.
    """
    return ElevatedSession(
        session_key=row["session_key"],
        username=row["username"],
        elevated_at=float(row["elevated_at"]),
        last_touched_at=float(row["last_touched_at"]),
        elevated_from_ip=row["elevated_from_ip"],
        scope=row["scope"] or "full",
    )


class _PgBackend:
    """PostgreSQL operations for ElevatedSessionManager.

    Uses psycopg3-style connection pool (pool.connection() context manager,
    %s placeholders). Rows are accessed by column name via dict-style access.
    """

    def __init__(self, pool: Any, idle_timeout: int, max_age: int) -> None:
        # pool type is Any — psycopg3 ConnectionPool not available at import time
        self._pool = pool
        self._idle_timeout = idle_timeout
        self._max_age = max_age

    def create(
        self,
        session_key: str,
        username: str,
        elevated_from_ip: Optional[str],
        scope: str,
    ) -> None:
        now = time.time()
        with self._pool.connection() as conn:
            conn.execute(
                _PG_UPSERT,
                (session_key, username, now, now, elevated_from_ip, scope),
            )
            conn.commit()

    def touch_atomic(self, session_key: str) -> Optional[ElevatedSession]:
        now = time.time()
        idle_cutoff = now - self._idle_timeout
        abs_cutoff = now - self._max_age
        with self._pool.connection() as conn:
            conn.row_factory = dict_row
            row = conn.execute(
                _PG_TOUCH, (now, session_key, idle_cutoff, abs_cutoff)
            ).fetchone()
            conn.commit()
        return _row_to_elevated_session(row) if row else None

    def touch_atomic_for_user(
        self, session_key: str, username: str
    ) -> Optional[ElevatedSession]:
        now = time.time()
        idle_cutoff = now - self._idle_timeout
        abs_cutoff = now - self._max_age
        with self._pool.connection() as conn:
            conn.row_factory = dict_row
            row = conn.execute(
                _PG_TOUCH_FOR_USER,
                (now, session_key, username, idle_cutoff, abs_cutoff),
            ).fetchone()
            conn.commit()
        return _row_to_elevated_session(row) if row else None

    def revoke(self, session_key: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(_PG_DELETE_KEY, (session_key,))
            conn.commit()

    def revoke_all_for_username(self, username: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(_PG_DELETE_USER, (username,))
            conn.commit()

    def get_status(self, session_key: str) -> Optional[ElevatedSession]:
        now = time.time()
        idle_cutoff = now - self._idle_timeout
        abs_cutoff = now - self._max_age
        with self._pool.connection() as conn:
            conn.row_factory = dict_row
            row = conn.execute(
                _PG_SELECT_VALID, (session_key, idle_cutoff, abs_cutoff)
            ).fetchone()
        return _row_to_elevated_session(row) if row else None


class ElevatedSessionManager:
    """Manages TOTP step-up elevation windows.

    Dual-mode: SQLite (solo) or PostgreSQL (cluster).
    Call set_connection_pool() to switch to PostgreSQL.
    Thread-safe for SQLite mode via threading.Lock.
    """

    def __init__(
        self,
        idle_timeout_seconds: int = _DEFAULT_IDLE_TIMEOUT,
        max_age_seconds: int = _DEFAULT_MAX_AGE,
        db_path: Optional[str] = None,
    ) -> None:
        if not isinstance(idle_timeout_seconds, int) or idle_timeout_seconds <= 0:
            raise ValueError(
                f"idle_timeout_seconds must be a positive integer, got {idle_timeout_seconds!r}"
            )
        if not isinstance(max_age_seconds, int) or max_age_seconds <= 0:
            raise ValueError(
                f"max_age_seconds must be a positive integer, got {max_age_seconds!r}"
            )
        if max_age_seconds < idle_timeout_seconds:
            raise ValueError(
                f"max_age_seconds ({max_age_seconds}) must be >= "
                f"idle_timeout_seconds ({idle_timeout_seconds})"
            )

        self._idle_timeout = idle_timeout_seconds
        self._max_age = max_age_seconds
        self._lock = threading.Lock()
        self._pool: Optional[Any] = None  # set by set_connection_pool() in cluster mode

        if db_path is not None:
            self._db_path = db_path
            parent = os.path.dirname(db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        else:
            data_dir = os.environ.get(
                "CIDX_DATA_DIR", os.path.expanduser("~/.cidx-server")
            )
            os.makedirs(data_dir, exist_ok=True)
            self._db_path = os.path.join(data_dir, "elevated_sessions.db")

        self._ensure_schema()

    # ------------------------------------------------------------------
    # Cluster wiring (mirrors MfaChallengeManager.set_connection_pool)
    # ------------------------------------------------------------------

    # Bounds matching ConfigService._TOTP_IDLE_MIN/MAX and _TOTP_MAX_AGE_MIN/MAX
    _UPDATE_IDLE_MIN: int = 60
    _UPDATE_IDLE_MAX: int = 3600
    _UPDATE_MAX_AGE_MIN: int = 300
    _UPDATE_MAX_AGE_MAX: int = 7200

    def update_timeouts(self, idle_timeout_seconds: int, max_age_seconds: int) -> None:
        """Hot-reload timeout parameters into the live manager (Bug #943 Fix #2).

        Called after a successful totp_elevation atomic save so operators see
        new idle/max_age values take effect immediately without a restart.

        Raises ValueError for out-of-range or cross-field violations.
        Both fields are updated under self._lock so no reader sees a half-update.
        """
        if not (self._UPDATE_IDLE_MIN <= idle_timeout_seconds <= self._UPDATE_IDLE_MAX):
            raise ValueError(
                f"idle_timeout_seconds must be between "
                f"{self._UPDATE_IDLE_MIN} and {self._UPDATE_IDLE_MAX}, "
                f"got {idle_timeout_seconds}"
            )
        if not (
            self._UPDATE_MAX_AGE_MIN <= max_age_seconds <= self._UPDATE_MAX_AGE_MAX
        ):
            raise ValueError(
                f"max_age_seconds must be between "
                f"{self._UPDATE_MAX_AGE_MIN} and {self._UPDATE_MAX_AGE_MAX}, "
                f"got {max_age_seconds}"
            )
        if max_age_seconds < idle_timeout_seconds:
            raise ValueError(
                f"max_age_seconds ({max_age_seconds}) must be >= "
                f"idle_timeout_seconds ({idle_timeout_seconds})"
            )
        with self._lock:
            self._idle_timeout = idle_timeout_seconds
            self._max_age = max_age_seconds
        logger.info(
            "ElevatedSessionManager timeouts updated: idle=%ds max_age=%ds",
            idle_timeout_seconds,
            max_age_seconds,
        )

    def set_connection_pool(self, pool: Any) -> None:
        """Set PostgreSQL connection pool for cluster mode.

        When set, all operations use PostgreSQL instead of SQLite,
        enabling cross-node elevation tracking.
        """
        self._pool = pool
        logger.info(
            "ElevatedSessionManager: using PostgreSQL connection pool (cluster mode)"
        )

    # ------------------------------------------------------------------
    # SQLite helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        conn = self._get_conn()
        try:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
        finally:
            conn.close()

    def _cleanup_expired(self) -> None:
        """Remove expired rows. Must be called while holding self._lock."""
        now = time.time()
        idle_cutoff = now - self._idle_timeout
        abs_cutoff = now - self._max_age
        conn = self._get_conn()
        try:
            conn.execute(
                "DELETE FROM elevated_sessions "
                "WHERE last_touched_at <= ? OR elevated_at <= ?",
                (idle_cutoff, abs_cutoff),
            )
            conn.commit()
        finally:
            conn.close()

    def _pg(self) -> _PgBackend:
        return _PgBackend(self._pool, self._idle_timeout, self._max_age)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(
        self,
        session_key: str,
        username: str,
        elevated_from_ip: Optional[str],
        scope: str = "full",
    ) -> None:
        """Open (or replace) an elevation window for session_key."""
        if not isinstance(session_key, str) or not session_key.strip():
            raise ValueError("session_key must be a non-empty string")
        if not isinstance(username, str) or not username.strip():
            raise ValueError("username must be a non-empty string")
        if not isinstance(scope, str) or scope not in _VALID_SCOPES:
            raise ValueError(
                f"scope must be one of {sorted(_VALID_SCOPES)}, got {scope!r}"
            )
        if elevated_from_ip is not None and not isinstance(elevated_from_ip, str):
            raise ValueError("elevated_from_ip must be a string or None")

        if self._pool is not None:
            self._pg().create(session_key, username, elevated_from_ip, scope)
        else:
            now = time.time()
            with self._lock:
                self._cleanup_expired()
                conn = self._get_conn()
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO elevated_sessions "
                        "(session_key, username, elevated_at, last_touched_at, "
                        " elevated_from_ip, scope) VALUES (?, ?, ?, ?, ?, ?)",
                        (session_key, username, now, now, elevated_from_ip, scope),
                    )
                    conn.commit()
                finally:
                    conn.close()
        logger.debug(
            "Elevation window created for %s (session_key=%.8s scope=%s)",
            username,
            session_key,
            scope,
        )

    def touch_atomic(self, session_key: str) -> Optional[ElevatedSession]:
        """Advance last_touched_at if the window is still valid."""
        if not isinstance(session_key, str) or not session_key.strip():
            raise ValueError("session_key must be a non-empty string")

        if self._pool is not None:
            return self._pg().touch_atomic(session_key)

        now = time.time()
        idle_cutoff = now - self._idle_timeout
        abs_cutoff = now - self._max_age
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN EXCLUSIVE")
                row = conn.execute(
                    "SELECT * FROM elevated_sessions "
                    "WHERE session_key = ? AND last_touched_at > ? AND elevated_at > ?",
                    (session_key, idle_cutoff, abs_cutoff),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                conn.execute(
                    "UPDATE elevated_sessions SET last_touched_at = ? WHERE session_key = ?",
                    (now, session_key),
                )
                conn.commit()
                updated = conn.execute(
                    "SELECT * FROM elevated_sessions WHERE session_key = ?",
                    (session_key,),
                ).fetchone()
                return _row_to_elevated_session(updated) if updated else None
            finally:
                conn.close()

    def touch_atomic_for_user(
        self, session_key: str, username: str
    ) -> Optional[ElevatedSession]:
        """Advance last_touched_at only when session_key is owned by username."""
        if not isinstance(session_key, str) or not session_key.strip():
            raise ValueError("session_key must be a non-empty string")
        if not isinstance(username, str) or not username.strip():
            raise ValueError("username must be a non-empty string")

        if self._pool is not None:
            return self._pg().touch_atomic_for_user(session_key, username)

        now = time.time()
        idle_cutoff = now - self._idle_timeout
        abs_cutoff = now - self._max_age
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN EXCLUSIVE")
                row = conn.execute(
                    "SELECT * FROM elevated_sessions "
                    "WHERE session_key = ? AND username = ? AND last_touched_at > ? AND elevated_at > ?",
                    (session_key, username, idle_cutoff, abs_cutoff),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                conn.execute(
                    "UPDATE elevated_sessions SET last_touched_at = ? "
                    "WHERE session_key = ? AND username = ?",
                    (now, session_key, username),
                )
                conn.commit()
                updated = conn.execute(
                    "SELECT * FROM elevated_sessions WHERE session_key = ? AND username = ?",
                    (session_key, username),
                ).fetchone()
                return _row_to_elevated_session(updated) if updated else None
            finally:
                conn.close()

    def revoke(self, session_key: str) -> None:
        """Remove a single elevation window immediately."""
        if not isinstance(session_key, str) or not session_key.strip():
            raise ValueError("session_key must be a non-empty string")

        if self._pool is not None:
            self._pg().revoke(session_key)
        else:
            with self._lock:
                conn = self._get_conn()
                try:
                    conn.execute(
                        "DELETE FROM elevated_sessions WHERE session_key = ?",
                        (session_key,),
                    )
                    conn.commit()
                finally:
                    conn.close()
        logger.debug("Elevation window revoked for session_key=%.8s", session_key)

    def revoke_all_for_username(self, username: str) -> None:
        """Remove all elevation windows for a given username."""
        if not isinstance(username, str) or not username.strip():
            raise ValueError("username must be a non-empty string")

        if self._pool is not None:
            self._pg().revoke_all_for_username(username)
        else:
            with self._lock:
                conn = self._get_conn()
                try:
                    conn.execute(
                        "DELETE FROM elevated_sessions WHERE username = ?",
                        (username,),
                    )
                    conn.commit()
                finally:
                    conn.close()
        logger.debug("All elevation windows revoked for username=%s", username)

    def get_status(self, session_key: str) -> Optional[ElevatedSession]:
        """Return the current elevation window without touching it (read-only)."""
        if not isinstance(session_key, str) or not session_key.strip():
            raise ValueError("session_key must be a non-empty string")

        if self._pool is not None:
            return self._pg().get_status(session_key)

        now = time.time()
        idle_cutoff = now - self._idle_timeout
        abs_cutoff = now - self._max_age
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM elevated_sessions "
                "WHERE session_key = ? AND last_touched_at > ? AND elevated_at > ?",
                (session_key, idle_cutoff, abs_cutoff),
            ).fetchone()
            return _row_to_elevated_session(row) if row else None
        finally:
            conn.close()


# Module-level singleton (Story #923 AC1).
# Mirrors mfa_challenge_manager pattern in src/code_indexer/server/auth/mfa_challenge.py.
# Wired into PostgreSQL connection pool via lifespan.set_connection_pool().
elevated_session_manager: ElevatedSessionManager = ElevatedSessionManager()
