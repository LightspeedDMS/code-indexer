"""PostgreSQL backend for control-plane metadata (content seam step 5a).

Cluster twin of ``ControlPlaneMetadataSqliteBackend``: alias pointers and
write-mode markers shared by every node, plus the dual-write outbox, over the
tables migrations 054 and 055 create. One pooled connection per call; the pool
commits on a clean exit and rolls back on an exception, so each method -- a
registry change together with its outbox row -- is one transaction.

An outbox claim is ``SELECT ... FOR UPDATE SKIP LOCKED`` held for the whole
sweep: a second sweeper skips the claimed rows instead of replaying them.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ..protocols.control_plane_metadata_backend import (
    TOMBSTONE_TABLE,
    OutboxRow,
    OutboxState,
    decode_payload,
    format_updated_at,
    group_claimed_rows,
    metadata_table,
)

_STATE_ROW_ID = 1


def _enqueue(conn: Any, kind: str, name: str) -> None:
    """Add the outbox row for a key -- on the caller's open transaction."""
    conn.execute(
        "INSERT INTO metadata_outbox (kind, name, created_at) VALUES (%s, %s, %s)",
        (kind, name, format_updated_at()),
    )


def _get_on(conn: Any, kind: str, name: str) -> Optional[Dict[str, Any]]:
    table = metadata_table(kind)
    row = conn.execute(
        f"SELECT payload FROM {table} WHERE name = %s", (name,)
    ).fetchone()
    return decode_payload(row[0], name) if row is not None else None


class _PgOutboxClaim:
    """Rows locked FOR UPDATE on one open transaction."""

    def __init__(self, conn: Any, rows: Sequence[Any]) -> None:
        self._conn = conn
        self._keys = group_claimed_rows(rows)

    def keys(self) -> List[Tuple[str, str, int]]:
        return list(self._keys)

    def current(self, kind: str, name: str) -> Optional[Dict[str, Any]]:
        return _get_on(self._conn, kind, name)

    def complete(self, kind: str, name: str, up_to_id: int) -> None:
        # Rows locked by another sweeper are skipped, not waited on.
        self._conn.execute(
            "DELETE FROM metadata_outbox WHERE id IN ("
            "SELECT id FROM metadata_outbox WHERE kind = %s AND name = %s "
            "AND id <= %s FOR UPDATE SKIP LOCKED)",
            (kind, name, up_to_id),
        )


class ControlPlaneMetadataPostgresBackend:
    """Registry storage for alias pointers and write-mode markers (cluster)."""

    def __init__(self, pool: Any) -> None:
        """
        Args:
            pool: A ConnectionPool (psycopg v3) shared with sibling backends.
        """
        self._pool = pool

    def get(self, kind: str, name: str) -> Optional[Dict[str, Any]]:
        with self._pool.connection() as conn:
            return _get_on(conn, kind, name)

    def list_all(self, kind: str, prefix: str = "") -> Dict[str, Dict[str, Any]]:
        table = metadata_table(kind)
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT name, payload FROM {table} "
                "WHERE substr(name, 1, %s) = %s ORDER BY name",
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
        with self._pool.connection() as conn:
            conn.execute(
                f"INSERT INTO {table} (name, payload, updated_at) "
                "VALUES (%s, %s, %s) ON CONFLICT (name) DO UPDATE SET "
                "payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at",
                (name, json.dumps(payload), format_updated_at()),
            )
            if enqueue:
                _enqueue(conn, kind, name)

    def delete(self, kind: str, name: str, *, enqueue: bool = False) -> None:
        table = metadata_table(kind)
        with self._pool.connection() as conn:
            conn.execute(f"DELETE FROM {table} WHERE name = %s", (name,))
            conn.execute(
                f"INSERT INTO {TOMBSTONE_TABLE} (kind, name, deleted_at) "
                "VALUES (%s, %s, %s) ON CONFLICT (kind, name) DO UPDATE SET "
                "deleted_at = EXCLUDED.deleted_at",
                (kind, name, format_updated_at()),
            )
            if enqueue:
                _enqueue(conn, kind, name)

    def import_if_newer(
        self, kind: str, name: str, payload: Dict[str, Any], updated_at: str
    ) -> bool:
        table = metadata_table(kind)
        with self._pool.connection() as conn:
            cursor = conn.execute(
                f"INSERT INTO {table} (name, payload, updated_at) "
                "SELECT %s, %s, %s WHERE NOT EXISTS ("
                f"SELECT 1 FROM {TOMBSTONE_TABLE} "
                "WHERE kind = %s AND name = %s AND deleted_at >= %s) "
                "ON CONFLICT (name) DO UPDATE SET "
                "payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at "
                f"WHERE EXCLUDED.updated_at > {table}.updated_at",
                (name, json.dumps(payload), updated_at, kind, name, updated_at),
            )
            return bool(cursor.rowcount)

    # ------------------------------------------------------------------
    # Outbox
    # ------------------------------------------------------------------

    def outbox_depth(self) -> int:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT count(*) FROM metadata_outbox").fetchone()
        return int(row[0])

    def outbox_rows(self) -> List[OutboxRow]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, kind, name FROM metadata_outbox ORDER BY id"
            ).fetchall()
        return [OutboxRow(int(r[0]), str(r[1]), str(r[2])) for r in rows]

    def delete_outbox_rows(self, ids: Sequence[int]) -> int:
        if not ids:
            return 0
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM metadata_outbox WHERE id = ANY(%s)",
                ([int(i) for i in ids],),
            )
            return int(cursor.rowcount)

    @contextmanager
    def claim_outbox(self, limit: int) -> Iterator[_PgOutboxClaim]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, kind, name FROM metadata_outbox ORDER BY id "
                "LIMIT %s FOR UPDATE SKIP LOCKED",
                (limit,),
            ).fetchall()
            yield _PgOutboxClaim(conn, rows)

    def get_outbox_state(self) -> OutboxState:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT drain_started_epoch, drain_sink, stopped_at, "
                "stopped_reason, stopped_sink FROM metadata_outbox_state "
                "WHERE id = %s",
                (_STATE_ROW_ID,),
            ).fetchone()
        if row is None:
            return OutboxState()
        return OutboxState(*row)

    def save_outbox_state(self, state: OutboxState) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO metadata_outbox_state (id, drain_started_epoch, "
                "drain_sink, stopped_at, stopped_reason, stopped_sink) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
                "drain_started_epoch = EXCLUDED.drain_started_epoch, "
                "drain_sink = EXCLUDED.drain_sink, "
                "stopped_at = EXCLUDED.stopped_at, "
                "stopped_reason = EXCLUDED.stopped_reason, "
                "stopped_sink = EXCLUDED.stopped_sink",
                (
                    _STATE_ROW_ID,
                    state.drain_started_epoch,
                    state.drain_sink,
                    state.stopped_at,
                    state.stopped_reason,
                    state.stopped_sink,
                ),
            )

    def close(self) -> None:
        """The pool is shared with sibling backends and closed by its owner."""
