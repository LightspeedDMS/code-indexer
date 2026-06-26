"""State token manager for OIDC CSRF protection.

Bug #1224: OIDC transaction state must survive across workers/nodes.

Solo mode (default): persists to SQLite via set_sqlite_path() so all uvicorn
workers on the same node share state through the same cidx_server.db file.
Cluster mode: persists to PostgreSQL via set_connection_pool() so all nodes
in the cluster share state.

Security properties preserved:
- PKCE code_verifier stored and round-tripped intact (never logged).
- State token is single-use: deleted atomically on validate_state().
- TTL enforced at read time (expires_at <= now) and via prune_expired().
- Open-redirect validation of redirect_to is the caller's responsibility.
"""

import json
import logging
import os
import secrets
import sqlite3
from datetime import datetime, timezone, timedelta
from threading import Lock
from typing import Any, Optional

logger = logging.getLogger(__name__)

STATE_TTL_SECONDS = 300  # 5 minutes

# ---------------------------------------------------------------------------
# Module-level configuration
# ---------------------------------------------------------------------------

# Written once by configure_sqlite_path() in service_init.py, before lifespan
# creates any StateManager instance.  Read-only after that point.
_configured_sqlite_path: Optional[str] = None
_config_lock = Lock()


def configure_sqlite_path(db_path: str) -> None:
    """Configure the default SQLite path for all future StateManager instances.

    Called once by service_init.py (before OIDC routes are initialised in
    lifespan) so that StateManager() calls in lifespan automatically use
    cidx_server.db instead of the per-instance default path.

    Thread-safe via _config_lock.  No-op if db_path is falsy.
    """
    global _configured_sqlite_path
    if db_path and isinstance(db_path, str):
        with _config_lock:
            _configured_sqlite_path = db_path


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS oidc_state_tokens (
    state_token TEXT PRIMARY KEY,
    state_data  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
"""


class StateManager:
    """OIDC state manager with shared-store backends.

    Dual-mode:
    - Solo (default): SQLite via set_sqlite_path() — all workers on one node
      share the same cidx_server.db file.
    - Cluster: PostgreSQL via set_connection_pool() — all nodes in the cluster
      share the same oidc_state_tokens table.

    Call set_sqlite_path() immediately after construction, OR call the
    module-level configure_sqlite_path() before constructing (mirrors
    ElevatedSessionManager and TokenBlacklist wiring in service_init.py).
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._pool: Any = None
        # Use the module-level configured path when set (service_init.py wiring),
        # otherwise fall back to a per-instance default.
        with _config_lock:
            configured = _configured_sqlite_path
        if configured:
            self._db_path = configured
        else:
            data_dir = os.environ.get(
                "CIDX_DATA_DIR", os.path.expanduser("~/.cidx-server")
            )
            os.makedirs(data_dir, exist_ok=True)
            self._db_path = os.path.join(data_dir, "cidx_oidc_state.db")
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Wiring API
    # ------------------------------------------------------------------

    def set_connection_pool(self, pool: Any) -> None:
        """Enable PostgreSQL for cluster mode."""
        self._pool = pool
        logger.info("OIDC StateManager: using PostgreSQL (cluster mode)")

    def set_sqlite_path(self, db_path: str) -> None:
        """Redirect SQLite storage to the given shared database path (Bug #1224).

        Mirrors ElevatedSessionManager.set_sqlite_path() and
        TokenBlacklist.set_sqlite_path(). Called by service_init.py to wire
        the manager to cidx_server.db so all uvicorn workers on the same
        node share OIDC state via the same SQLite file.

        Creates the oidc_state_tokens schema in the target DB if absent.
        No-op if a PostgreSQL pool is already wired (PG takes precedence).
        """
        if self._pool is not None:
            return
        if not isinstance(db_path, str) or not db_path.strip():
            raise ValueError("db_path must be a non-empty string")
        with self._lock:
            self._db_path = db_path
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_state(self, data: Any) -> str:
        """Generate a new state token, persist data, return the token."""
        state_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=STATE_TTL_SECONDS)

        if self._pool is not None:
            self._pg_create(state_token, data, expires_at)
        else:
            self._sqlite_create(state_token, data, expires_at)
        return state_token

    def update_state_data(self, state_token: str, data: Any) -> bool:
        """Replace data for an existing state token. Returns True if found."""
        if self._pool is not None:
            return self._pg_update(state_token, data)
        return self._sqlite_update(state_token, data)

    def validate_state(self, state_token: str) -> Optional[Any]:
        """Atomically fetch-and-delete the state entry.

        Returns the data dict, or None if absent or expired.
        Single-use: the row is deleted on the first successful retrieval.
        """
        if self._pool is not None:
            return self._pg_validate(state_token)
        return self._sqlite_validate(state_token)

    def prune_expired(self, ttl_seconds: int = STATE_TTL_SECONDS) -> int:
        """Delete rows whose expires_at is in the past. Returns rows deleted.

        expires_at is stored as an absolute ISO timestamp (created_at +
        STATE_TTL_SECONDS), so any row with expires_at <= now is safely
        prunable. The ttl_seconds parameter is accepted for API consistency
        with ElevatedSessionManager but is not used in the deletion predicate
        — the stored expires_at is authoritative.

        Mirrors ElevatedSessionManager.prune_expired() — wired into
        DataRetentionScheduler to prevent unbounded table growth.
        """
        if self._pool is not None:
            return self._pg_prune()
        return self._sqlite_prune()

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

    def _sqlite_create(self, state_token: str, data: Any, expires_at: datetime) -> None:
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT INTO oidc_state_tokens (state_token, state_data, expires_at) "
                    "VALUES (?, ?, ?)",
                    (state_token, json.dumps(data), expires_at.isoformat()),
                )
                conn.commit()
            finally:
                conn.close()

    def _sqlite_update(self, state_token: str, data: Any) -> bool:
        with self._lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "UPDATE oidc_state_tokens SET state_data = ? WHERE state_token = ?",
                    (json.dumps(data), state_token),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def _sqlite_validate(self, state_token: str) -> Optional[Any]:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN EXCLUSIVE")
                row = conn.execute(
                    "SELECT state_data FROM oidc_state_tokens "
                    "WHERE state_token = ? AND expires_at > ?",
                    (state_token, now_iso),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                conn.execute(
                    "DELETE FROM oidc_state_tokens WHERE state_token = ?",
                    (state_token,),
                )
                conn.commit()
                data_str = row["state_data"]
            finally:
                conn.close()
        return json.loads(data_str) if isinstance(data_str, str) else data_str

    def _sqlite_prune(self) -> int:
        """Delete rows with expires_at <= now. Returns count deleted."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT state_token FROM oidc_state_tokens WHERE expires_at <= ?",
                    (now_iso,),
                ).fetchall()
                conn.execute(
                    "DELETE FROM oidc_state_tokens WHERE expires_at <= ?",
                    (now_iso,),
                )
                conn.commit()
                return len(rows)
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # PostgreSQL backend methods
    # ------------------------------------------------------------------

    def _pg_create(self, state_token: str, data: Any, expires_at: datetime) -> None:
        assert self._pool is not None
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO oidc_state_tokens (state_token, state_data, expires_at) "
                "VALUES (%s, %s, %s)",
                (state_token, json.dumps(data), expires_at),
            )
            conn.commit()

    def _pg_update(self, state_token: str, data: Any) -> bool:
        assert self._pool is not None
        with self._pool.connection() as conn:
            result = conn.execute(
                "UPDATE oidc_state_tokens SET state_data = %s WHERE state_token = %s",
                (json.dumps(data), state_token),
            )
            conn.commit()
            return bool(result.rowcount > 0)

    def _pg_validate(self, state_token: str) -> Optional[Any]:
        assert self._pool is not None
        with self._pool.connection() as conn:
            from psycopg.rows import dict_row

            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "DELETE FROM oidc_state_tokens WHERE state_token = %s "
                    "AND expires_at > NOW() RETURNING state_data",
                    (state_token,),
                )
                row = cur.fetchone()
            conn.commit()
        if row is None:
            return None
        data_str = row["state_data"]
        return json.loads(data_str) if isinstance(data_str, str) else data_str

    def _pg_prune(self) -> int:
        """Delete rows with expires_at <= NOW(). Returns count deleted."""
        assert self._pool is not None
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM oidc_state_tokens WHERE expires_at <= NOW() "
                "RETURNING state_token",
            )
            conn.commit()
            rows = cursor.fetchall()
        return len(rows) if rows else 0
