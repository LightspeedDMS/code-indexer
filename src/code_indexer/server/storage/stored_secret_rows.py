"""Row operations shared by the stored-secret tables (``ci_tokens``,
``user_git_credentials``) on SQLite and PostgreSQL.

- Rewrites are compare-and-set on the encrypted value, so a value changed
  by someone else in the meantime is never overwritten.
- A value no key on this node can decrypt is marked in the nullable
  ``needs_reentry_at`` column, with the cluster node that failed in
  ``needs_reentry_node`` (NULL in solo); it is never deleted. Saving a new
  value or a successful rewrite clears the mark. PostgreSQL gets the columns
  from migration 076; SQLite adds them in place on first use.
- Listings of marked rows never carry the secret value.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

NEEDS_REENTRY_COLUMN = "needs_reentry_at"
NEEDS_REENTRY_NODE_COLUMN = "needs_reentry_node"
_CLEAR_MARK = f"{NEEDS_REENTRY_COLUMN} = NULL, {NEEDS_REENTRY_NODE_COLUMN} = NULL"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _StoredSecretRowsBase:
    _SECRET_TABLE: str
    _SECRET_PK: str
    _SECRET_LABELS: Tuple[str, ...]
    _PH: str

    def _query(self, sql: str, params: Sequence[Any]) -> List[Tuple[Any, ...]]:
        raise NotImplementedError

    def _update(self, sql: str, params: Sequence[Any]) -> int:
        raise NotImplementedError

    def _select(self, where: str, with_secret: bool) -> List[Dict[str, Any]]:
        names = [
            self._SECRET_PK,
            NEEDS_REENTRY_COLUMN,
            NEEDS_REENTRY_NODE_COLUMN,
            *self._SECRET_LABELS,
        ]
        if with_secret:
            names.append("encrypted_token")
        sql = f"SELECT {', '.join(names)} FROM {self._SECRET_TABLE} {where}"
        keys = ["id", *names[1:]]
        return [dict(zip(keys, row)) for row in self._query(sql, ())]

    def list_secret_rows(self) -> List[Dict[str, Any]]:
        """Every row with its id, encrypted value, mark and labels."""
        return self._select("", with_secret=True)

    def list_needs_reentry(self) -> List[Dict[str, Any]]:
        """Marked rows: id, mark time, failing node and labels -- never the
        value."""
        return self._select(f"WHERE {NEEDS_REENTRY_COLUMN} IS NOT NULL", False)

    def replace_encrypted_token_if_unchanged(
        self, row_id: str, expected: str, new_value: str
    ) -> bool:
        p = self._PH
        return (
            self._update(
                f"UPDATE {self._SECRET_TABLE} SET encrypted_token = {p},"
                f" {_CLEAR_MARK}"
                f" WHERE {self._SECRET_PK} = {p} AND encrypted_token = {p}",
                (new_value, row_id, expected),
            )
            == 1
        )

    def mark_needs_reentry(
        self, row_id: str, expected: str, node_id: Optional[str] = None
    ) -> bool:
        p = self._PH
        return (
            self._update(
                f"UPDATE {self._SECRET_TABLE} SET {NEEDS_REENTRY_COLUMN} = {p},"
                f" {NEEDS_REENTRY_NODE_COLUMN} = {p}"
                f" WHERE {self._SECRET_PK} = {p} AND encrypted_token = {p}"
                f" AND {NEEDS_REENTRY_COLUMN} IS NULL",
                (_now(), node_id, row_id, expected),
            )
            == 1
        )

    def clear_needs_reentry(self, row_id: str, expected: str) -> bool:
        p = self._PH
        return (
            self._update(
                f"UPDATE {self._SECRET_TABLE} SET {_CLEAR_MARK}"
                f" WHERE {self._SECRET_PK} = {p} AND encrypted_token = {p}"
                f" AND {NEEDS_REENTRY_COLUMN} IS NOT NULL",
                (row_id, expected),
            )
            == 1
        )


_ENSURED: Set[Tuple[str, str]] = set()
_ENSURED_LOCK = threading.Lock()


def ensure_sqlite_needs_reentry_column(conn: sqlite3.Connection, table: str) -> None:
    """Add the needs-re-entry columns to *table* when missing (idempotent; a
    concurrent adder is tolerated)."""
    for column in (NEEDS_REENTRY_COLUMN, NEEDS_REENTRY_NODE_COLUMN):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in columns:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                raise


class SqliteStoredSecretRowsMixin(_StoredSecretRowsBase):
    _PH = "?"
    _conn_manager: Any

    def _ensure_column(self) -> None:
        marker = (str(self._conn_manager.db_path), self._SECRET_TABLE)
        with _ENSURED_LOCK:
            if marker in _ENSURED:
                return
            ensure_sqlite_needs_reentry_column(
                self._conn_manager.get_connection(), self._SECRET_TABLE
            )
            _ENSURED.add(marker)

    def _query(self, sql: str, params: Sequence[Any]) -> List[Tuple[Any, ...]]:
        self._ensure_column()
        conn = self._conn_manager.get_connection()
        return [tuple(row) for row in conn.execute(sql, tuple(params)).fetchall()]

    def _update(self, sql: str, params: Sequence[Any]) -> int:
        self._ensure_column()

        def operation(conn: sqlite3.Connection) -> int:
            return int(conn.execute(sql, tuple(params)).rowcount)

        result: int = self._conn_manager.execute_atomic(operation)
        return result


class PostgresStoredSecretRowsMixin(_StoredSecretRowsBase):
    _PH = "%s"
    _pool: Any

    def _query(self, sql: str, params: Sequence[Any]) -> List[Tuple[Any, ...]]:
        from psycopg.rows import tuple_row

        with self._pool.connection() as conn:
            with conn.cursor(row_factory=tuple_row) as cur:
                cur.execute(sql, tuple(params))
                return [tuple(row) for row in cur.fetchall()]

    def _update(self, sql: str, params: Sequence[Any]) -> int:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                count = int(cur.rowcount)
            conn.commit()
        return count
