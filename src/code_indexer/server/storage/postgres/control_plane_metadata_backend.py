"""PostgreSQL backend for control-plane metadata (content seam step 5a).

Cluster twin of ``ControlPlaneMetadataSqliteBackend``: alias pointers and
write-mode markers shared by every node, over the tables migration 054
creates. One pooled connection per call; the pool commits on a clean exit
and rolls back on an exception, so each method is one transaction.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ..protocols.control_plane_metadata_backend import (
    decode_payload,
    format_updated_at,
    metadata_table,
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
        table = metadata_table(kind)
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT payload FROM {table} WHERE name = %s", (name,)
            ).fetchone()
        return decode_payload(row[0], name) if row is not None else None

    def list_all(self, kind: str, prefix: str = "") -> Dict[str, Dict[str, Any]]:
        table = metadata_table(kind)
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT name, payload FROM {table} "
                "WHERE substr(name, 1, %s) = %s ORDER BY name",
                (len(prefix), prefix),
            ).fetchall()
        return {row[0]: decode_payload(row[1], row[0]) for row in rows}

    def put(self, kind: str, name: str, payload: Dict[str, Any]) -> None:
        table = metadata_table(kind)
        with self._pool.connection() as conn:
            conn.execute(
                f"INSERT INTO {table} (name, payload, updated_at) "
                "VALUES (%s, %s, %s) ON CONFLICT (name) DO UPDATE SET "
                "payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at",
                (name, json.dumps(payload), format_updated_at()),
            )

    def delete(self, kind: str, name: str) -> None:
        table = metadata_table(kind)
        with self._pool.connection() as conn:
            conn.execute(f"DELETE FROM {table} WHERE name = %s", (name,))

    def import_if_newer(
        self, kind: str, name: str, payload: Dict[str, Any], updated_at: str
    ) -> bool:
        table = metadata_table(kind)
        with self._pool.connection() as conn:
            cursor = conn.execute(
                f"INSERT INTO {table} (name, payload, updated_at) "
                "VALUES (%s, %s, %s) ON CONFLICT (name) DO UPDATE SET "
                "payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at "
                f"WHERE EXCLUDED.updated_at > {table}.updated_at",
                (name, json.dumps(payload), updated_at),
            )
            return bool(cursor.rowcount)

    def close(self) -> None:
        """The pool is shared with sibling backends and closed by its owner."""
