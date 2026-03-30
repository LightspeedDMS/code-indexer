"""State token manager for OIDC CSRF protection."""

import json
import logging
import secrets
from datetime import datetime, timezone, timedelta
from threading import Lock
from typing import Any, Optional

logger = logging.getLogger(__name__)

STATE_TTL_SECONDS = 300  # 5 minutes


class StateManager:
    def __init__(self):
        self._states = {}
        self._lock = Lock()
        self._pool: Any = None

    def set_connection_pool(self, pool: Any) -> None:
        """Enable PostgreSQL for cluster mode."""
        self._pool = pool
        logger.info("OIDC StateManager: using PostgreSQL (cluster mode)")

    def create_state(self, data):
        state_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=STATE_TTL_SECONDS)

        if self._pool is not None:
            self._pg_create(state_token, data, expires_at)
        else:
            with self._lock:
                self._states[state_token] = {
                    "data": data,
                    "expires_at": expires_at,
                }
        return state_token

    def update_state_data(self, state_token, data):
        if self._pool is not None:
            return self._pg_update(state_token, data)
        with self._lock:
            if state_token in self._states:
                self._states[state_token]["data"] = data
                return True
            return False

    def validate_state(self, state_token):
        if self._pool is not None:
            return self._pg_validate(state_token)
        with self._lock:
            if state_token not in self._states:
                return None
            state_entry = self._states[state_token]
            if datetime.now(timezone.utc) > state_entry["expires_at"]:
                del self._states[state_token]
                return None
            data = state_entry["data"]
            del self._states[state_token]
            return data

    # -- PostgreSQL backend methods --

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
            # Atomic: SELECT + DELETE in one transaction
            from psycopg.rows import dict_row

            conn.row_factory = dict_row
            row = conn.execute(
                "DELETE FROM oidc_state_tokens WHERE state_token = %s "
                "AND expires_at > NOW() RETURNING state_data",
                (state_token,),
            ).fetchone()
            conn.commit()
        if row is None:
            return None
        data_str = row["state_data"]
        return json.loads(data_str) if isinstance(data_str, str) else data_str
