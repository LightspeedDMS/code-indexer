"""SQLite backend for control-plane metadata (content seam step 5a).

Alias pointers and write-mode markers for a solo server, stored in the main
server database next to ``global_repos``. Tables are created by
``DatabaseSchema`` (database_manager.py); the PostgreSQL twin is
``ControlPlaneMetadataPostgresBackend`` over migration 054.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Optional

from ..database_manager import DatabaseConnectionManager
from ..protocols.control_plane_metadata_backend import (
    decode_payload,
    format_updated_at,
    metadata_table,
)


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

    def put(self, kind: str, name: str, payload: Dict[str, Any]) -> None:
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

        self._conn_manager.execute_atomic(operation)

    def delete(self, kind: str, name: str) -> None:
        table = metadata_table(kind)

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(f"DELETE FROM {table} WHERE name = ?", (name,))

        self._conn_manager.execute_atomic(operation)

    def import_if_newer(
        self, kind: str, name: str, payload: Dict[str, Any], updated_at: str
    ) -> bool:
        table = metadata_table(kind)
        encoded = json.dumps(payload)

        def operation(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                f"INSERT INTO {table} (name, payload, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "payload = excluded.payload, updated_at = excluded.updated_at "
                f"WHERE excluded.updated_at > {table}.updated_at",
                (name, encoded, updated_at),
            )
            return bool(cursor.rowcount)

        return self._conn_manager.execute_atomic(operation)

    def close(self) -> None:
        """Connections are owned by the shared DatabaseConnectionManager."""
