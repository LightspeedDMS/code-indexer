"""SQLite backend for control-plane metadata (content seam step 5a).

Alias pointers and write-mode markers for a solo server, stored in the main
server database next to ``global_repos``, plus the dual-write outbox. Tables
are created by ``DatabaseSchema`` (database_manager.py); the PostgreSQL twin
is ``ControlPlaneMetadataPostgresBackend`` over migrations 054 and 055.

SQLite has no row locks, and a solo server may still run several worker
processes, so an outbox claim is a short lease: a sweep stamps its token on
the rows it takes, replays them without holding the database lock, and drops
only rows carrying its own token. A crashed sweeper's lease expires after
``OUTBOX_CLAIM_LEASE_SECONDS``.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ..database_manager import DatabaseConnectionManager
from ..protocols.control_plane_metadata_backend import (
    OUTBOX_CLAIM_LEASE_SECONDS,
    TOMBSTONE_TABLE,
    OutboxRow,
    OutboxState,
    decode_payload,
    format_updated_at,
    group_claimed_rows,
    metadata_table,
)

_STATE_ROW_ID = 1


def _enqueue(conn: sqlite3.Connection, kind: str, name: str) -> None:
    """Add the outbox row for a key -- on the caller's open transaction."""
    conn.execute(
        "INSERT INTO metadata_outbox (kind, name, created_at) VALUES (?, ?, ?)",
        (kind, name, format_updated_at()),
    )


class _SqliteOutboxClaim:
    def __init__(
        self,
        backend: "ControlPlaneMetadataSqliteBackend",
        conn_manager: DatabaseConnectionManager,
        token: str,
        rows: Sequence[Any],
    ) -> None:
        self._backend = backend
        self._conn_manager = conn_manager
        self._token = token
        self._keys = group_claimed_rows(rows)

    def keys(self) -> List[Tuple[str, str, int]]:
        return list(self._keys)

    def current(self, kind: str, name: str) -> Optional[Dict[str, Any]]:
        return self._backend.get(kind, name)

    def complete(self, kind: str, name: str, up_to_id: int) -> None:
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM metadata_outbox WHERE kind = ? AND name = ? "
                "AND id <= ? AND claim_token = ?",
                (kind, name, up_to_id, self._token),
            )

        self._conn_manager.execute_atomic(operation)


class ControlPlaneMetadataSqliteBackend:
    """Registry storage for alias pointers and write-mode markers (solo)."""

    def __init__(self, db_path: str) -> None:
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def get(self, kind: str, name: str) -> Optional[Dict[str, Any]]:
        table = metadata_table(kind)
        with self._conn_manager.guarded_connection() as conn:
            row = conn.execute(
                f"SELECT payload FROM {table} WHERE name = ?", (name,)
            ).fetchone()
        return decode_payload(row[0], name) if row is not None else None

    def list_all(self, kind: str, prefix: str = "") -> Dict[str, Dict[str, Any]]:
        table = metadata_table(kind)
        with self._conn_manager.guarded_connection() as conn:
            rows = conn.execute(
                f"SELECT name, payload FROM {table} "
                "WHERE substr(name, 1, ?) = ? ORDER BY name",
                (len(prefix), prefix),
            ).fetchall()
        return {row[0]: decode_payload(row[1], row[0]) for row in rows}

    def put(
        self,
        kind: str,
        name: str,
        payload: Dict[str, Any],
        *,
        enqueue: bool = False,
    ) -> None:
        table = metadata_table(kind)
        encoded = json.dumps(payload)
        updated_at = format_updated_at()

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"INSERT INTO {table} (name, payload, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "payload = excluded.payload, updated_at = excluded.updated_at",
                (name, encoded, updated_at),
            )
            if enqueue:
                _enqueue(conn, kind, name)

        self._conn_manager.execute_atomic(operation)

    def delete(self, kind: str, name: str, *, enqueue: bool = False) -> None:
        table = metadata_table(kind)

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(f"DELETE FROM {table} WHERE name = ?", (name,))
            conn.execute(
                f"INSERT INTO {TOMBSTONE_TABLE} (kind, name, deleted_at) "
                "VALUES (?, ?, ?) ON CONFLICT(kind, name) DO UPDATE SET "
                "deleted_at = excluded.deleted_at",
                (kind, name, format_updated_at()),
            )
            if enqueue:
                _enqueue(conn, kind, name)

        self._conn_manager.execute_atomic(operation)

    def import_if_newer(
        self, kind: str, name: str, payload: Dict[str, Any], updated_at: str
    ) -> bool:
        table = metadata_table(kind)
        encoded = json.dumps(payload)

        def operation(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                f"INSERT INTO {table} (name, payload, updated_at) "
                "SELECT ?, ?, ? WHERE NOT EXISTS ("
                f"SELECT 1 FROM {TOMBSTONE_TABLE} "
                "WHERE kind = ? AND name = ? AND deleted_at >= ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "payload = excluded.payload, updated_at = excluded.updated_at "
                f"WHERE excluded.updated_at > {table}.updated_at",
                (name, encoded, updated_at, kind, name, updated_at),
            )
            return bool(cursor.rowcount)

        return self._conn_manager.execute_atomic(operation)

    # ------------------------------------------------------------------
    # Outbox
    # ------------------------------------------------------------------

    def outbox_depth(self) -> int:
        with self._conn_manager.guarded_connection() as conn:
            row = conn.execute("SELECT count(*) FROM metadata_outbox").fetchone()
        return int(row[0])

    def outbox_rows(self) -> List[OutboxRow]:
        with self._conn_manager.guarded_connection() as conn:
            rows = conn.execute(
                "SELECT id, kind, name FROM metadata_outbox ORDER BY id"
            ).fetchall()
        return [OutboxRow(int(r[0]), str(r[1]), str(r[2])) for r in rows]

    def delete_outbox_rows(self, ids: Sequence[int]) -> int:
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)

        def operation(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                f"DELETE FROM metadata_outbox WHERE id IN ({placeholders})",
                tuple(int(i) for i in ids),
            )
            return int(cursor.rowcount)

        return self._conn_manager.execute_atomic(operation)

    @contextmanager
    def claim_outbox(self, limit: int) -> Iterator[_SqliteOutboxClaim]:
        token = uuid.uuid4().hex
        now = time.time()

        def claim(conn: sqlite3.Connection) -> List[Any]:
            rows = conn.execute(
                "SELECT id, kind, name FROM metadata_outbox "
                "WHERE claim_token IS NULL OR claimed_until < ? "
                "ORDER BY id LIMIT ?",
                (now, limit),
            ).fetchall()
            if rows:
                placeholders = ",".join("?" for _ in rows)
                conn.execute(
                    "UPDATE metadata_outbox SET claim_token = ?, claimed_until = ? "
                    f"WHERE id IN ({placeholders})",
                    (token, now + OUTBOX_CLAIM_LEASE_SECONDS)
                    + tuple(r[0] for r in rows),
                )
            return list(rows)

        rows = self._conn_manager.execute_atomic(claim)
        try:
            yield _SqliteOutboxClaim(self, self._conn_manager, token, rows)
        finally:

            def release(conn: sqlite3.Connection) -> None:
                conn.execute(
                    "UPDATE metadata_outbox SET claim_token = NULL, "
                    "claimed_until = NULL WHERE claim_token = ?",
                    (token,),
                )

            self._conn_manager.execute_atomic(release)

    def get_outbox_state(self) -> OutboxState:
        with self._conn_manager.guarded_connection() as conn:
            row = conn.execute(
                "SELECT drain_started_epoch, drain_sink, stopped_at, "
                "stopped_reason, stopped_sink FROM metadata_outbox_state "
                "WHERE id = ?",
                (_STATE_ROW_ID,),
            ).fetchone()
        if row is None:
            return OutboxState()
        return OutboxState(*row)

    def save_outbox_state(self, state: OutboxState) -> None:
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO metadata_outbox_state (id, drain_started_epoch, "
                "drain_sink, stopped_at, stopped_reason, stopped_sink) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "drain_started_epoch = excluded.drain_started_epoch, "
                "drain_sink = excluded.drain_sink, "
                "stopped_at = excluded.stopped_at, "
                "stopped_reason = excluded.stopped_reason, "
                "stopped_sink = excluded.stopped_sink",
                (
                    _STATE_ROW_ID,
                    state.drain_started_epoch,
                    state.drain_sink,
                    state.stopped_at,
                    state.stopped_reason,
                    state.stopped_sink,
                ),
            )

        self._conn_manager.execute_atomic(operation)

    def close(self) -> None:
        """Connections are owned by the shared DatabaseConnectionManager."""
