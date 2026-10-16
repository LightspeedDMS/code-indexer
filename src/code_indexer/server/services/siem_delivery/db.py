"""Storage for SIEM delivery: one SQL code path over two dialects.

SQLite: every table lives in ``groups.db`` beside ``audit_logs`` (so the
queue insert can join the audit transaction); write transactions are
``BEGIN EXCLUSIVE`` through ``DatabaseConnectionManager.execute_atomic``.
PostgreSQL: the shared pool; tables come from migration ``054``.

SQL is written once with ``?`` placeholders.  Every lease, backoff, expiry,
window and age uses DATABASE time: each transaction reads the database
clock once (:meth:`SiemTx.now`) and derived instants are passed as
parameters.  SQLite stores ONE fixed ISO-8601 UTC shape
(``YYYY-MM-DDTHH:MM:SS.mmmZ``) and compares only values of that shape.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, TypeVar

T = TypeVar("T")

SQLITE_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"


@dataclass(frozen=True)
class Dialect:
    name: str
    now_sql: str
    for_update: str

    def sql(self, text: str) -> str:
        return text.replace("?", "%s") if self.name == "postgres" else text

    def ts(self, value: datetime) -> Any:
        if self.name == "postgres":
            return value
        utc = value.astimezone(timezone.utc)
        return utc.strftime(SQLITE_TS_FORMAT) + f".{utc.microsecond // 1000:03d}Z"

    @staticmethod
    def parse_ts(value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc)
        text = str(value)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )

    @contextlib.contextmanager
    def savepoint(self, conn: Any, name: str) -> Iterator[None]:
        """A savepoint inside the caller's open transaction."""
        if self.name == "postgres":
            with conn.transaction():
                yield
            return
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        conn.execute(f"RELEASE SAVEPOINT {name}")


SQLITE = Dialect("sqlite", "strftime('%Y-%m-%dT%H:%M:%fZ','now')", "")
POSTGRES = Dialect("postgres", "now()", " FOR UPDATE")


class SiemTx:
    """A connection inside one transaction (or a plain read)."""

    def __init__(self, conn: Any, dialect: Dialect) -> None:
        self.conn = conn
        self.dialect = dialect
        self._now: Optional[datetime] = None

    def _cursor(self, sql: str, params: Sequence[Any]) -> Any:
        if self.dialect.name == "postgres":
            from psycopg.rows import dict_row

            cur = self.conn.cursor(row_factory=dict_row)
            cur.execute(self.dialect.sql(sql), tuple(params))
            return cur
        return self.conn.execute(sql, tuple(params))

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = self._cursor(sql, params)
        count = int(cur.rowcount)
        if self.dialect.name == "postgres":
            cur.close()
        return count

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        cur = self._cursor(sql, params)
        if self.dialect.name == "postgres":
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
            return rows
        names = [d[0] for d in cur.description or ()]
        return [dict(zip(names, row)) for row in cur.fetchall()]

    def one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def now(self) -> datetime:
        """The database clock, read once per transaction."""
        if self._now is None:
            row = self.one(f"SELECT {self.dialect.now_sql} AS now")
            assert row is not None
            parsed = Dialect.parse_ts(row["now"])
            assert parsed is not None
            self._now = parsed
        return self._now

    def ts(self, value: datetime) -> Any:
        return self.dialect.ts(value)


class SiemDb:
    """Transactions on the SIEM tables of one backend."""

    def __init__(
        self,
        dialect: Dialect,
        *,
        conn_manager: Any = None,
        pool: Any = None,
        groups_db_path: Optional[str] = None,
    ) -> None:
        self.dialect = dialect
        self._conn_manager = conn_manager
        self._pool = pool
        self.groups_db_path = groups_db_path

    @classmethod
    def sqlite(cls, groups_db_path: str) -> "SiemDb":
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        return cls(
            SQLITE,
            conn_manager=DatabaseConnectionManager.get_instance(str(groups_db_path)),
            groups_db_path=str(groups_db_path),
        )

    @classmethod
    def postgres(cls, pool: Any) -> "SiemDb":
        return cls(POSTGRES, pool=pool)

    @property
    def pool(self) -> Any:
        """The PostgreSQL pool (None on SQLite)."""
        return self._pool

    def write(self, fn: Callable[[SiemTx], T], *, phase: str = "write") -> T:
        """Run *fn* in ONE short write transaction (rolled back on error)."""
        started = time.monotonic()
        try:
            if self.dialect.name == "postgres":
                with self._pool.connection() as conn:
                    with conn.transaction():
                        return fn(SiemTx(conn, self.dialect))
            result: T = self._conn_manager.execute_atomic(
                lambda conn: fn(SiemTx(conn, self.dialect))
            )
            return result
        finally:
            from code_indexer.server.services.siem_delivery import telemetry

            telemetry.record_seconds(
                "write_tx_seconds", time.monotonic() - started, phase
            )

    def read(self, fn: Callable[[SiemTx], T]) -> T:
        """Run *fn* outside any write transaction."""
        if self.dialect.name == "postgres":
            with self._pool.connection() as conn:
                return fn(SiemTx(conn, self.dialect))
        with self._conn_manager.guarded_connection() as conn:
            return fn(SiemTx(conn, self.dialect))


# --- SQLite schema (PostgreSQL: migrations/sql/054_siem_delivery.sql) --------

SQLITE_SCHEMA: Sequence[str] = (
    """CREATE TABLE IF NOT EXISTS siem_delivery_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_uuid TEXT NOT NULL UNIQUE,
        destination_key TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        action_type TEXT NOT NULL,
        event_payload TEXT,
        projection_error TEXT,
        status TEXT NOT NULL,
        batch_id TEXT,
        batch_ordinal INTEGER,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        mapping_version INTEGER NOT NULL,
        quarantine_reason TEXT,
        quarantine_signature TEXT,
        boundary_kind TEXT,
        created_at TEXT NOT NULL,
        delivered_at TEXT,
        delivered_via TEXT)""",
    """CREATE TABLE IF NOT EXISTS siem_delivery_batches (
        batch_id TEXT PRIMARY KEY,
        destination_key TEXT NOT NULL,
        body BLOB,
        body_sha256 TEXT NOT NULL,
        event_count INTEGER NOT NULL,
        mapping_version INTEGER NOT NULL,
        state TEXT NOT NULL,
        last_class TEXT,
        lease_owner TEXT,
        lease_token INTEGER,
        lease_expires_at TEXT,
        created_at TEXT NOT NULL,
        send_attempts INTEGER NOT NULL DEFAULT 0,
        first_sent_at TEXT,
        last_sent_at TEXT,
        next_attempt_at TEXT NOT NULL,
        outcome_unknown INTEGER NOT NULL DEFAULT 0,
        prior_outcome_unknown INTEGER NOT NULL DEFAULT 0,
        bisect_depth INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS siem_delivery_state (
        id INTEGER PRIMARY KEY,
        fence_counter INTEGER NOT NULL DEFAULT 0,
        halted_class TEXT,
        halted_signature TEXT,
        halted_since TEXT,
        halted_batch_id TEXT,
        halted_mapping_version INTEGER,
        next_probe_at TEXT,
        canary_run_id TEXT,
        canary_destination_key TEXT,
        canary_mapping_version INTEGER,
        canary_expected TEXT,
        canary_sent_at TEXT,
        canary_result TEXT,
        canary_result_signature TEXT,
        canary_actor TEXT,
        canary_confirmed_ids TEXT,
        canary_missing_action_types TEXT,
        canary_visible_confirmed_by TEXT,
        canary_visible_confirmed_at TEXT,
        armed_destination_key TEXT,
        armed_at TEXT,
        armed_config_version INTEGER,
        seen_config_version INTEGER NOT NULL DEFAULT 0,
        quarantine_window_start TEXT,
        quarantine_window_count INTEGER NOT NULL DEFAULT 0,
        delivered_total INTEGER NOT NULL DEFAULT 0,
        resent_after_unknown_outcome INTEGER NOT NULL DEFAULT 0,
        unrecoverable_total INTEGER NOT NULL DEFAULT 0,
        capture_after_boundary_total INTEGER NOT NULL DEFAULT 0,
        capture_after_boundary_late_total INTEGER NOT NULL DEFAULT 0,
        boundary_settled_after_total INTEGER NOT NULL DEFAULT 0,
        boundary_settled_late_total INTEGER NOT NULL DEFAULT 0,
        last_boundary_scanned_id INTEGER NOT NULL DEFAULT 0,
        requeued_mapping_version INTEGER NOT NULL DEFAULT 0,
        stats_json TEXT,
        stats_refreshed_at TEXT,
        canary_config_epoch TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS siem_process_status (
        process_id TEXT PRIMARY KEY,
        node_id TEXT NOT NULL,
        destination_key TEXT,
        probe_result TEXT NOT NULL,
        probed_at TEXT,
        last_seen_at TEXT NOT NULL,
        expires_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS siem_destinations (
        destination_key TEXT PRIMARY KEY,
        region TEXT,
        project_id TEXT,
        location TEXT,
        instance_id TEXT,
        first_seen_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS siem_backlog_samples (
        sampled_at TEXT PRIMARY KEY,
        backlog_rows_estimate INTEGER NOT NULL)""",
    # The SecOps service-account key, encrypted (PostgreSQL: migration 055).
    """CREATE TABLE IF NOT EXISTS siem_delivery_credential (
        id INTEGER PRIMARY KEY,
        credential_id TEXT NOT NULL,
        encrypted_key TEXT NOT NULL,
        key_check TEXT NOT NULL,
        client_email TEXT NOT NULL,
        private_key_id TEXT NOT NULL,
        set_by TEXT NOT NULL,
        set_at TEXT NOT NULL)""",
)

# (index name, table, columns) -- identical on both backends.
SIEM_INDEXES: Sequence[Sequence[str]] = (
    # candidate reads walk ids in order (no sort); next_attempt_at is checked
    # from the index entry
    (
        "idx_siem_queue_status_dest_id",
        "siem_delivery_queue",
        "status, destination_key, id, next_attempt_at",
    ),
    ("idx_siem_queue_status_id", "siem_delivery_queue", "status, id"),
    (
        "idx_siem_queue_dest_created",
        "siem_delivery_queue",
        "destination_key, created_at",
    ),
    ("idx_siem_queue_boundary", "siem_delivery_queue", "boundary_kind, id"),
    ("idx_siem_queue_status_created", "siem_delivery_queue", "status, created_at"),
    ("idx_siem_queue_status_delivered", "siem_delivery_queue", "status, delivered_at"),
    ("idx_siem_queue_batch", "siem_delivery_queue", "batch_id, batch_ordinal"),
    (
        "idx_siem_batches_dest_state_next",
        "siem_delivery_batches",
        "destination_key, state, next_attempt_at",
    ),
    ("idx_siem_batches_state_created", "siem_delivery_batches", "state, created_at"),
    ("idx_siem_process_expires", "siem_process_status", "expires_at"),
    ("idx_siem_process_node_expires", "siem_process_status", "node_id, expires_at"),
)

STATE_ROW_INSERT = (
    "INSERT INTO siem_delivery_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING"
)


def ensure_sqlite_schema(conn: sqlite3.Connection) -> None:
    """Create the SIEM tables in groups.db (inside the caller's transaction)."""
    for statement in SQLITE_SCHEMA:
        conn.execute(statement)
    state_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(siem_delivery_state)")
    }
    # Bug #2018 (PostgreSQL: migrations 056, 057): one idempotent path for
    # fresh and upgraded files
    for column, ddl in (
        ("canary_config_epoch", "TEXT NOT NULL DEFAULT ''"),
        ("canary_issued_seq", "INTEGER NOT NULL DEFAULT 0"),
        ("canary_run_seq", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in state_columns:
            conn.execute(f"ALTER TABLE siem_delivery_state ADD COLUMN {column} {ddl}")
    for name, table, columns in SIEM_INDEXES:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({columns})")
    conn.execute(STATE_ROW_INSERT)
