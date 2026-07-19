"""Embedding & reranker call tracking -- Story #1418.

Defines the shared call-record shape (``EmbeddingCallRecord``) plus
dual-backend (SQLite solo / PostgreSQL cluster) storage for the
``embedding_call_stats`` table (vendor cost reconciliation). Mirrors the
established SearchEmbedEventRecord / SearchEmbedEventSqliteBackend /
...PostgresBackend pattern (search_embed_event_writer.py, Story #1293) and
the Bug #1181 one-transaction-per-batch insert convention (see CLAUDE.md's
"Per-query batch commit (store_batch)" section).

Full architecture (writer registry, 10 instrumentation injection points,
dual-mode cross-process bootstrap, config tunables, retention sweep):
docs/architecture-invariants.md#embedding--reranker-call-tracking.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    # Bug #1441: this is a type-annotation-only need (all annotations in
    # this module are lazy strings via `from __future__ import
    # annotations` above) -- ConnectionPool is used ONLY as the type
    # annotation on EmbeddingCallStatsPostgresBackend.__init__, never
    # constructed or isinstance-checked here. A real top-level import
    # would pull in `import psycopg` unconditionally (connection_pool.py),
    # even for callers that only need the SQLite backend or NoOpWriter's
    # zero-dependency fallback path -- see cli.py's
    # _install_embedding_stats_writer_for_index().
    from code_indexer.server.storage.postgres.connection_pool import (
        ConnectionPool,
    )

logger = logging.getLogger(__name__)

VALID_PROVIDERS = frozenset({"voyageai", "cohere"})
VALID_CALL_TYPES = frozenset({"embed", "embed_multimodal", "rerank"})
VALID_PURPOSES = frozenset(
    {"index", "refresh", "query", "temporal", "key_test", "cache_shadow_audit"}
)


@dataclass
class EmbeddingCallRecord:
    """One real (non-cached, non-suppressed) embedding/reranker call.

    Only real vendor-billed HTTP calls are ever recorded as one of these --
    cache hits and coalesced-away duplicate requests must NEVER produce a
    record (enforced by call sites in later phases, not by this shape).
    """

    provider: str  # "voyageai" | "cohere"
    call_type: str  # "embed" | "embed_multimodal" | "rerank"
    model: str
    item_count: int
    token_count: int
    batch_size: int
    purpose: str  # "index" | "refresh" | "query" | "temporal" | "key_test" | "cache_shadow_audit"
    success: bool
    latency_ms: int
    occurred_at: float
    golden_repo_alias: Optional[str] = None
    job_id: Optional[str] = None
    node_id: Optional[str] = None  # cluster node id; nullable in solo mode

    def __post_init__(self) -> None:
        if self.provider not in VALID_PROVIDERS:
            raise ValueError(
                f"Invalid provider {self.provider!r}. "
                f"Must be one of: {sorted(VALID_PROVIDERS)}"
            )
        if self.call_type not in VALID_CALL_TYPES:
            raise ValueError(
                f"Invalid call_type {self.call_type!r}. "
                f"Must be one of: {sorted(VALID_CALL_TYPES)}"
            )
        if self.purpose not in VALID_PURPOSES:
            raise ValueError(
                f"Invalid purpose {self.purpose!r}. "
                f"Must be one of: {sorted(VALID_PURPOSES)}"
            )
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError(f"model must be a non-empty string, got {self.model!r}")
        for field_name in ("item_count", "token_count", "batch_size", "latency_ms"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(
                    f"{field_name} must be a non-negative int, got {value!r}"
                )
        if not isinstance(self.success, bool):
            raise ValueError(f"success must be a bool, got {self.success!r}")


# ---------------------------------------------------------------------------
# Shared row <-> record conversion (used by both backends)
# ---------------------------------------------------------------------------

_INSERT_COLUMNS = (
    "provider, call_type, model, item_count, token_count, batch_size, purpose, "
    "golden_repo_alias, job_id, node_id, success, latency_ms, occurred_at"
)


def _record_to_row(r: EmbeddingCallRecord) -> tuple:
    return (
        r.provider,
        r.call_type,
        r.model,
        r.item_count,
        r.token_count,
        r.batch_size,
        r.purpose,
        r.golden_repo_alias,
        r.job_id,
        r.node_id,
        r.success,
        r.latency_ms,
        r.occurred_at,
    )


def _row_to_record(row: tuple) -> EmbeddingCallRecord:
    (
        provider,
        call_type,
        model,
        item_count,
        token_count,
        batch_size,
        purpose,
        golden_repo_alias,
        job_id,
        node_id,
        success,
        latency_ms,
        occurred_at,
    ) = row
    return EmbeddingCallRecord(
        provider=provider,
        call_type=call_type,
        model=model,
        item_count=item_count,
        token_count=token_count,
        batch_size=batch_size,
        purpose=purpose,
        success=bool(success),
        latency_ms=latency_ms,
        occurred_at=occurred_at,
        golden_repo_alias=golden_repo_alias,
        job_id=job_id,
        node_id=node_id,
    )


def _build_query_where_clause(
    placeholder: str,
    *,
    provider: Optional[str],
    purpose: Optional[str],
    golden_repo_alias: Optional[str],
    job_id: Optional[str],
    start_time: Optional[float],
    end_time: Optional[float],
):
    """Build a shared WHERE clause for both backends' query().

    ``placeholder`` is "?" for SQLite, "%s" for PostgreSQL.
    """
    conditions = []
    params: list = []
    if provider is not None:
        conditions.append(f"provider = {placeholder}")
        params.append(provider)
    if purpose is not None:
        conditions.append(f"purpose = {placeholder}")
        params.append(purpose)
    if golden_repo_alias is not None:
        conditions.append(f"golden_repo_alias = {placeholder}")
        params.append(golden_repo_alias)
    if job_id is not None:
        conditions.append(f"job_id = {placeholder}")
        params.append(job_id)
    if start_time is not None:
        conditions.append(f"occurred_at >= {placeholder}")
        params.append(start_time)
    if end_time is not None:
        conditions.append(f"occurred_at < {placeholder}")
        params.append(end_time)
    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
    return where_clause, params


# ---------------------------------------------------------------------------
# SQLite backend (solo / development mode)
# ---------------------------------------------------------------------------


class EmbeddingCallStatsSqliteBackend:
    """SQLite backend for the embedding_call_stats table (solo mode)."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS embedding_call_stats (
                        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                        provider           TEXT NOT NULL,
                        call_type          TEXT NOT NULL,
                        model              TEXT NOT NULL,
                        item_count         INTEGER NOT NULL,
                        token_count        INTEGER NOT NULL,
                        batch_size         INTEGER NOT NULL,
                        purpose            TEXT NOT NULL,
                        golden_repo_alias  TEXT,
                        job_id             TEXT,
                        node_id            TEXT,
                        success            INTEGER NOT NULL,
                        latency_ms         INTEGER NOT NULL,
                        occurred_at        REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ecs_occurred_at "
                    "ON embedding_call_stats (occurred_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ecs_provider "
                    "ON embedding_call_stats (provider)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ecs_golden_repo_alias "
                    "ON embedding_call_stats (golden_repo_alias)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ecs_job_id "
                    "ON embedding_call_stats (job_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ecs_purpose "
                    "ON embedding_call_stats (purpose)"
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(
                "EmbeddingCallStatsSqliteBackend: schema setup failed: %s", exc
            )

    def insert_batch(self, records: List[EmbeddingCallRecord]) -> None:
        """Insert a batch of records in ONE transaction. No-op for empty batch."""
        if not records:
            return
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                conn.executemany(
                    f"INSERT INTO embedding_call_stats ({_INSERT_COLUMNS}) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [_record_to_row(r) for r in records],
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(
                "EmbeddingCallStatsSqliteBackend: insert_batch failed: %s", exc
            )
            raise

    def query(
        self,
        *,
        provider: Optional[str] = None,
        purpose: Optional[str] = None,
        golden_repo_alias: Optional[str] = None,
        job_id: Optional[str] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> List[EmbeddingCallRecord]:
        """Query records, newest first, filtered by any combination of args."""
        where_clause, params = _build_query_where_clause(
            "?",
            provider=provider,
            purpose=purpose,
            golden_repo_alias=golden_repo_alias,
            job_id=job_id,
            start_time=start_time,
            end_time=end_time,
        )
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                sql = (
                    f"SELECT {_INSERT_COLUMNS} FROM embedding_call_stats "
                    f"{where_clause} ORDER BY occurred_at DESC LIMIT ? OFFSET ?"
                )
                rows = conn.execute(sql, params + [limit, offset]).fetchall()
                return [_row_to_record(row) for row in rows]
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("EmbeddingCallStatsSqliteBackend: query failed: %s", exc)
            return []

    def delete_where(self, occurred_at_before: float) -> int:
        """Delete rows with occurred_at strictly before the cutoff. Returns count."""
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                cur = conn.execute(
                    "DELETE FROM embedding_call_stats WHERE occurred_at < ?",
                    (occurred_at_before,),
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(
                "EmbeddingCallStatsSqliteBackend: delete_where failed: %s", exc
            )
            return 0


# ---------------------------------------------------------------------------
# PostgreSQL backend (cluster mode)
# ---------------------------------------------------------------------------


class EmbeddingCallStatsPostgresBackend:
    """PostgreSQL backend for the embedding_call_stats table (cluster mode)."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS embedding_call_stats (
                        id                 BIGSERIAL PRIMARY KEY,
                        provider           TEXT NOT NULL,
                        call_type          TEXT NOT NULL,
                        model              TEXT NOT NULL,
                        item_count         INTEGER NOT NULL,
                        token_count        INTEGER NOT NULL,
                        batch_size         INTEGER NOT NULL,
                        purpose            TEXT NOT NULL,
                        golden_repo_alias  TEXT,
                        job_id             TEXT,
                        node_id            TEXT,
                        success            BOOLEAN NOT NULL,
                        latency_ms         INTEGER NOT NULL,
                        occurred_at        DOUBLE PRECISION NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ecs_occurred_at "
                    "ON embedding_call_stats (occurred_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ecs_provider "
                    "ON embedding_call_stats (provider)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ecs_golden_repo_alias "
                    "ON embedding_call_stats (golden_repo_alias)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ecs_job_id "
                    "ON embedding_call_stats (job_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ecs_purpose "
                    "ON embedding_call_stats (purpose)"
                )
                conn.commit()
        except Exception as exc:
            logger.warning(
                "EmbeddingCallStatsPostgresBackend: schema setup failed: %s", exc
            )

    def insert_batch(self, records: List[EmbeddingCallRecord]) -> None:
        """Insert a batch of records in ONE transaction/commit (Bug #1181 pattern).

        SET LOCAL synchronous_commit = off relaxes WAL fsync for these
        ephemeral, observability-only rows -- the commit is still visible
        immediately; only crash durability is relaxed.

        psycopg v3: executemany lives on the CURSOR, not the connection --
        using the cursor keeps this in the SAME transaction as the SET LOCAL
        above and the single commit() below.
        """
        if not records:
            return
        rows = [_record_to_row(r) for r in records]
        placeholders = ", ".join(["%s"] * 13)
        try:
            with self._pool.connection() as conn:
                conn.execute("SET LOCAL synchronous_commit = off")
                with conn.cursor() as cur:
                    cur.executemany(
                        f"INSERT INTO embedding_call_stats ({_INSERT_COLUMNS}) "
                        f"VALUES ({placeholders})",
                        rows,
                    )
                conn.commit()
        except Exception as exc:
            logger.warning(
                "EmbeddingCallStatsPostgresBackend: insert_batch failed: %s", exc
            )
            raise

    def query(
        self,
        *,
        provider: Optional[str] = None,
        purpose: Optional[str] = None,
        golden_repo_alias: Optional[str] = None,
        job_id: Optional[str] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> List[EmbeddingCallRecord]:
        """Query records, newest first, filtered by any combination of args."""
        where_clause, params = _build_query_where_clause(
            "%s",
            provider=provider,
            purpose=purpose,
            golden_repo_alias=golden_repo_alias,
            job_id=job_id,
            start_time=start_time,
            end_time=end_time,
        )
        try:
            with self._pool.connection() as conn:
                sql = (
                    f"SELECT {_INSERT_COLUMNS} FROM embedding_call_stats "
                    f"{where_clause} ORDER BY occurred_at DESC LIMIT %s OFFSET %s"
                )
                rows = conn.execute(sql, params + [limit, offset]).fetchall()
                return [_row_to_record(tuple(row)) for row in rows]
        except Exception as exc:
            logger.warning("EmbeddingCallStatsPostgresBackend: query failed: %s", exc)
            return []

    def delete_where(self, occurred_at_before: float) -> int:
        """Delete rows with occurred_at strictly before the cutoff. Returns count."""
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM embedding_call_stats WHERE occurred_at < %s",
                        (occurred_at_before,),
                    )
                    affected = cur.rowcount
                conn.commit()
                return int(affected)
        except Exception as exc:
            logger.warning(
                "EmbeddingCallStatsPostgresBackend: delete_where failed: %s", exc
            )
            return 0
