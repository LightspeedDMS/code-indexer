"""SearchEventLogWriter — per-query search event logging (Issue #1159).

Writes SearchEventRecord entries to the search_event_log table via a
background writer thread that batches inserts for performance.

Modeled after ApiMetricsService (Story #1083) writer/drain pattern.
"""

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from queue import Empty, Full, Queue
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from code_indexer.server.services.config_service import (
    get_config_service as get_config_service_for_eviction,
)
from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

logger = logging.getLogger(__name__)

# Maximum batch size per drain cycle (anti-unbounded-loop, Messi #14).
_MAX_DRAIN_BATCH = 500

# Eviction interval: run prune at most once per 24 hours.
_EVICTION_INTERVAL_SECONDS = 86400.0

# Default retention window when config is unavailable.
DEFAULT_RETENTION_DAYS = 90

# Seconds per day (used for cutoff calculation).
SECONDS_PER_DAY = 86400

# Bug #2106: user-filtered export batches seek (username, timestamp). Needed
# without ANALYZE statistics (solo SQLite never collects them); valid on both
# backends. Also in DatabaseSchema (SQLite) and PG migration 071.
_CREATE_IDX_SEL_USER_TS = (
    "CREATE INDEX IF NOT EXISTS idx_sel_user_ts "
    "ON search_event_log (username, timestamp)"
)


# ---------------------------------------------------------------------------
# Domain object
# ---------------------------------------------------------------------------


@dataclass
class SearchEventRecord:
    """One row in the search_event_log table."""

    timestamp: float
    username: str
    repo_alias: Optional[str]
    search_type: str
    query_text: str
    voyage_cache_hit: Optional[bool]
    voyage_cache_mode: Optional[str]
    voyage_latency_ms: Optional[int]
    cohere_cache_hit: Optional[bool]
    cohere_cache_mode: Optional[str]
    cohere_latency_ms: Optional[int]
    total_latency_ms: int
    result_count: int
    node_id: str
    correlation_id: Optional[str]
    # Bug #2114: whether query_text was cut to the stored cap. None only for
    # rows written before the column existed.
    query_text_truncated: Optional[bool] = None
    # Story #2117: the MCP tool or REST/Web endpoint that ran the search, the
    # entry point it came through (SEARCH_EVENT_DOORS), whether it succeeded
    # (SEARCH_EVENT_OUTCOMES) and, for a failure, a short neutral error class
    # (never a message, path, query or user data). None only for rows written
    # before the columns existed.
    tool: Optional[str] = None
    door: Optional[str] = None
    outcome: Optional[str] = None
    error_class: Optional[str] = None


# Story #2117: the values the door and outcome columns can carry.
SEARCH_EVENT_DOORS = ("mcp", "rest", "web")
SEARCH_EVENT_OUTCOMES = ("success", "failure")

# Bug #2114: added after the original DDL (an existing table gains it by ADD
# COLUMN).
_ADDED_COLUMN_TRUNCATED = "query_text_truncated"

# Columns added after the original DDL, in the order an existing table gains
# them: (name, SQLite type, PostgreSQL type). Bug #2114 then Story #2117
# (PG migrations 073 and 074).
_ADDED_COLUMNS = (
    (_ADDED_COLUMN_TRUNCATED, "INTEGER", "BOOLEAN"),
    ("tool", "TEXT", "TEXT"),
    ("door", "TEXT", "TEXT"),
    ("outcome", "TEXT", "TEXT"),
    ("error_class", "TEXT", "TEXT"),
)

# Every column a read returns, by name (both backends select this list, so
# a row maps by name whatever the physical column order).
_EVENT_COLUMNS = (
    "id",
    "timestamp",
    "username",
    "repo_alias",
    "search_type",
    "query_text",
    "voyage_cache_hit",
    "voyage_cache_mode",
    "voyage_latency_ms",
    "cohere_cache_hit",
    "cohere_cache_mode",
    "cohere_latency_ms",
    "total_latency_ms",
    "result_count",
    "node_id",
    "correlation_id",
) + tuple(name for name, _sqlite_type, _pg_type in _ADDED_COLUMNS)

_SELECT_COLUMNS = ", ".join(_EVENT_COLUMNS)

# Columns an insert writes (every column but the generated id).
_INSERT_COLUMNS = _EVENT_COLUMNS[1:]


def _sqlite_event_columns(conn: sqlite3.Connection) -> Set[str]:
    """Column names of the search_event_log table as SQLite has them now."""
    return {row[1] for row in conn.execute("PRAGMA table_info(search_event_log)")}


def _sqlite_add_missing_columns(conn: sqlite3.Connection) -> None:
    """Add every _ADDED_COLUMNS column an existing table lacks; old rows NULL.

    The read-then-add is not atomic: another worker process may add a column
    between our read and our ALTER. That ALTER failure is tolerated only when
    the column now exists -- decided by the schema itself, never the message.
    """
    for name, sqlite_type, _pg_type in _ADDED_COLUMNS:
        if name in _sqlite_event_columns(conn):
            continue
        try:
            conn.execute(
                f"ALTER TABLE search_event_log ADD COLUMN {name} {sqlite_type}"
            )
        except sqlite3.OperationalError:
            if name not in _sqlite_event_columns(conn):
                raise


def _pg_add_missing_columns(conn: Any) -> None:
    """Add every _ADDED_COLUMNS column an existing table lacks (also PG
    migrations 073/074). Checked first, so a node whose table already has a
    column never takes ALTER TABLE's exclusive lock for it."""
    for name, _sqlite_type, pg_type in _ADDED_COLUMNS:
        has_column = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'search_event_log' AND column_name = %s",
            (name,),
        ).fetchone()
        if has_column is None:
            conn.execute(
                f"ALTER TABLE search_event_log ADD COLUMN IF NOT EXISTS {name} {pg_type}"
            )


def _pg_row_to_event(row: Any) -> Dict[str, Any]:
    """Map a PostgreSQL row read with _SELECT_COLUMNS to an event dict."""
    return dict(zip(_EVENT_COLUMNS, row))


def _flag(value: Optional[bool]) -> Optional[int]:
    """A nullable boolean as SQLite stores it."""
    return None if value is None else int(value)


def _insert_values(r: SearchEventRecord, sqlite: bool) -> tuple:
    """One record's values in _INSERT_COLUMNS order."""
    to_db = _flag if sqlite else (lambda v: v)
    return (
        r.timestamp,
        r.username,
        r.repo_alias,
        r.search_type,
        r.query_text,
        to_db(r.voyage_cache_hit),
        r.voyage_cache_mode,
        r.voyage_latency_ms,
        to_db(r.cohere_cache_hit),
        r.cohere_cache_mode,
        r.cohere_latency_ms,
        r.total_latency_ms,
        r.result_count,
        r.node_id,
        r.correlation_id,
        to_db(r.query_text_truncated),
        r.tool,
        r.door,
        r.outcome,
        r.error_class,
    )


def _insert_sql(ph: str) -> str:
    return (
        f"INSERT INTO search_event_log ({', '.join(_INSERT_COLUMNS)}) "
        f"VALUES ({', '.join([ph] * len(_INSERT_COLUMNS))})"
    )


def _validated_choice(value: Any, allowed: Tuple[str, ...], name: str) -> None:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}, got {value!r}")


# Longest tool identifier a row carries, and longest correlation id a filter
# accepts (Story #2117).
TOOL_MAX_CHARS = 100
CORRELATION_ID_MAX_CHARS = 128


def _dimension_conditions(
    filters: dict, ph: str, conditions: List[str], params: List[Any]
) -> None:
    """Story #2117 exact-match filters: door, outcome, tool, correlation id.
    A row recorded before the columns existed (NULL) matches none of them.
    Raises ValueError for an unknown door/outcome or an over-long tool or
    correlation id."""
    door = filters.get("door") or None
    if door:
        _validated_choice(door, SEARCH_EVENT_DOORS, "door")
    outcome = filters.get("outcome") or None
    if outcome:
        _validated_choice(outcome, SEARCH_EVENT_OUTCOMES, "outcome")
    tool = filters.get("tool") or None
    if tool and len(tool) > TOOL_MAX_CHARS:
        raise ValueError(f"tool filter must be at most {TOOL_MAX_CHARS} characters")
    correlation_id = filters.get("correlation_id") or None
    if correlation_id and len(correlation_id) > CORRELATION_ID_MAX_CHARS:
        raise ValueError(
            f"correlation_id filter must be at most {CORRELATION_ID_MAX_CHARS} characters"
        )
    for column, value in (
        ("door", door),
        ("outcome", outcome),
        ("tool", tool),
        ("correlation_id", correlation_id),
    ):
        if value:
            conditions.append(f"{column} = {ph}")
            params.append(value)


def _export_where(
    filters: dict,
    after: Optional[Tuple[float, int]],
    ph: str,
    true_lit: str,
    false_lit: str,
) -> Tuple[str, List[Any]]:
    """Build the WHERE clause of one export read (Bug #2106).

    Every filter -- including cache_hit_filter -- is evaluated in the store,
    before the LIMIT. `after` is the (timestamp, id) keyset of the last row
    already read; only rows strictly after it in (timestamp, id) order match.
    `ph` is the parameter placeholder, `true_lit`/`false_lit` the boolean
    literals of the backend's cache-hit columns.
    """
    conditions: List[str] = []
    params: List[Any] = []

    username = filters.get("user") or None
    if username:
        conditions.append(f"username = {ph}")
        params.append(username)

    repo_alias = filters.get("repo_alias") or None
    if repo_alias:
        conditions.append(f"repo_alias = {ph}")
        params.append(repo_alias)

    search_type = filters.get("search_type")
    if search_type and search_type != "all":
        conditions.append(f"search_type = {ph}")
        params.append(search_type)

    from_ts = filters.get("from_timestamp")
    if from_ts is not None:
        conditions.append(f"timestamp >= {ph}")
        params.append(from_ts)

    to_ts = filters.get("to_timestamp")
    if to_ts is not None:
        conditions.append(f"timestamp < {ph}")
        params.append(to_ts)

    _dimension_conditions(filters, ph, conditions, params)

    # Bug #2112: a cache mode is recorded only when the embedding cache was
    # consulted; it is NULL when no cache applies (fts, regex, xray, ...),
    # when the cache is absent, disabled or off, and when it was bypassed.
    # Such rows are in neither hits_only nor misses_only.
    cache_hit_filter = filters.get("cache_hit_filter", "all")
    if cache_hit_filter == "hits_only":
        conditions.append(
            f"((voyage_cache_mode IS NOT NULL AND voyage_cache_hit = {true_lit})"
            f" OR (cohere_cache_mode IS NOT NULL AND cohere_cache_hit = {true_lit}))"
        )
    elif cache_hit_filter == "misses_only":
        conditions.append(
            "(voyage_cache_mode IS NOT NULL OR cohere_cache_mode IS NOT NULL)"
            f" AND (voyage_cache_hit IS NULL OR voyage_cache_hit = {false_lit})"
            f" AND (cohere_cache_hit IS NULL OR cohere_cache_hit = {false_lit})"
        )

    if cache_hit_filter not in ("all", "hits_only", "misses_only"):
        raise ValueError(
            "cache_hit_filter must be 'all', 'hits_only' or 'misses_only', "
            f"got {cache_hit_filter!r}"
        )

    if after is not None:
        # Equivalent to (timestamp, id) > after, written so the leading
        # `timestamp >=` is an index range seek on both backends (an OR at the
        # top level makes the timestamp index a full scan, and a full export
        # quadratic).
        conditions.append(f"timestamp >= {ph} AND (timestamp > {ph} OR id > {ph})")
        params.extend([after[0], after[0], after[1]])

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
    return where_clause, params


def _export_select(
    filters: dict,
    after: Optional[Tuple[float, int]],
    limit: int,
    ph: str,
    true_lit: str,
    false_lit: str,
) -> Tuple[str, List[Any]]:
    """Return the (sql, params) of one bounded export read, for either backend."""
    where_clause, params = _export_where(filters, after, ph, true_lit, false_lit)
    sql = (
        f"SELECT {_SELECT_COLUMNS} FROM search_event_log {where_clause} "
        f"ORDER BY timestamp ASC, id ASC LIMIT {ph}"
    )
    return sql, params + [limit]


def _list_where(
    ph: str,
    username: Optional[str],
    search_type: Optional[str],
    repo_alias: Optional[str],
    from_ts: Optional[float],
    to_ts: Optional[float],
    dimensions: Dict[str, Optional[str]],
) -> Tuple[str, List[Any]]:
    """WHERE clause of the admin event list (`query`), for either backend.

    Time range is half-open: [from_ts, to_ts). `dimensions` holds the
    Story #2117 exact-match filters (door, outcome, tool, correlation_id).
    """
    conditions: List[str] = []
    params: List[Any] = []
    for column, value in (
        ("username", username),
        ("search_type", search_type),
        ("repo_alias", repo_alias),
    ):
        if value is not None:
            conditions.append(f"{column} = {ph}")
            params.append(value)
    if from_ts is not None:
        conditions.append(f"timestamp >= {ph}")
        params.append(from_ts)
    if to_ts is not None:
        conditions.append(f"timestamp < {ph}")
        params.append(to_ts)
    _dimension_conditions(dimensions, ph, conditions, params)
    return ("WHERE " + " AND ".join(conditions) if conditions else ""), params


# Story #2117: the oldest row still stored (retention prunes older ones).
# MIN over the indexed timestamp column: an index read on both backends.
OLDEST_TIMESTAMP_SQL = "SELECT MIN(timestamp) FROM search_event_log"


def _oldest_from_row(row: Any) -> Optional[float]:
    """The OLDEST_TIMESTAMP_SQL result: None for an empty table."""
    if row is None:
        raise RuntimeError("MIN(timestamp) returned no row")
    return None if row[0] is None else float(row[0])


def _hit_rate_sql(ph: str, true_lit: str, windowed: bool) -> str:
    """SQL of get_hit_rate_counts for either backend (Issue #1257).

    Parameters: mode x4, then [from_ts, to_ts) when `windowed`. Story #2117:
    a failed search is not a search for the hit rate, so outcome 'failure'
    rows are excluded; rows from before the outcome column existed (NULL)
    are counted as they always were.

    The counts are of ROWS: each row is one searched repository's own cache
    lookup (Bug #2111 writes one row per searched repository, each carrying
    that repository's lookup), so the row count is the lookup count. Rows
    are never merged by correlation id: it comes from a client header and
    can be reused across separate searches.
    """
    window = f" AND timestamp >= {ph} AND timestamp < {ph}" if windowed else ""
    return (
        "SELECT "
        f"SUM(CASE WHEN voyage_cache_mode = {ph} OR cohere_cache_mode = {ph} "
        "THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN "
        f"(voyage_cache_mode = {ph} AND voyage_cache_hit = {true_lit}) "
        f"OR (cohere_cache_mode = {ph} AND cohere_cache_hit = {true_lit}) "
        "THEN 1 ELSE 0 END) "
        "FROM search_event_log "
        f"WHERE (outcome IS NULL OR outcome <> 'failure'){window}"
    )


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------


class SearchEventLogSqliteBackend:
    """SQLite backend for search_event_log table (solo / development mode)."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the search_event_log table and indexes if they do not exist."""
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_event_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        username TEXT NOT NULL,
                        repo_alias TEXT,
                        search_type TEXT NOT NULL,
                        query_text TEXT NOT NULL,
                        voyage_cache_hit INTEGER,
                        voyage_cache_mode TEXT,
                        voyage_latency_ms INTEGER,
                        cohere_cache_hit INTEGER,
                        cohere_cache_mode TEXT,
                        cohere_latency_ms INTEGER,
                        total_latency_ms INTEGER NOT NULL,
                        result_count INTEGER NOT NULL,
                        node_id TEXT NOT NULL,
                        correlation_id TEXT,
                        query_text_truncated INTEGER,
                        tool TEXT,
                        door TEXT,
                        outcome TEXT,
                        error_class TEXT
                    )
                    """
                )
                # Bug #2114 / Story #2117: a table created before a column
                # existed (here or by DatabaseSchema) gains it in place; old
                # rows read NULL.
                _sqlite_add_missing_columns(conn)
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_sel_timestamp
                        ON search_event_log (timestamp)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_sel_user
                        ON search_event_log (username)
                    """
                )
                conn.execute(_CREATE_IDX_SEL_USER_TS)
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("SearchEventLogSqliteBackend: schema setup failed: %s", exc)

    def insert_batch(self, records: List[SearchEventRecord]) -> None:
        """Insert a batch of records. No-op for empty batch."""
        if not records:
            return
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                conn.executemany(
                    _insert_sql("?"),
                    [_insert_values(r, sqlite=True) for r in records],
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("SearchEventLogSqliteBackend: insert_batch failed: %s", exc)
            raise

    def prune_older_than(self, cutoff_timestamp: float) -> None:
        """Delete records older than cutoff_timestamp (Unix epoch seconds)."""
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                conn.execute(
                    "DELETE FROM search_event_log WHERE timestamp < ?",
                    (cutoff_timestamp,),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(
                "SearchEventLogSqliteBackend: prune_older_than failed: %s", exc
            )

    def query(
        self,
        username: Optional[str] = None,
        search_type: Optional[str] = None,
        repo_alias: Optional[str] = None,
        from_ts: Optional[float] = None,
        to_ts: Optional[float] = None,
        limit: int = 100,
        offset: int = 0,
        door: Optional[str] = None,
        outcome: Optional[str] = None,
        tool: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ):
        """Query events with optional filters.

        Time range is half-open: [from_ts, to_ts).
        Results ordered by timestamp DESC.
        Returns (events: list[dict], total_count: int).

        Bug #2105: any read failure propagates to the caller. An empty result
        means the store was read successfully and nothing matched; it never
        stands in for a failed read.
        """
        where_clause, params = _list_where(
            "?",
            username,
            search_type,
            repo_alias,
            from_ts,
            to_ts,
            {
                "door": door,
                "outcome": outcome,
                "tool": tool,
                "correlation_id": correlation_id,
            },
        )

        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            # Count total
            count_sql = f"SELECT COUNT(*) FROM search_event_log {where_clause}"
            total_count = conn.execute(count_sql, params).fetchone()[0]

            # Fetch page
            data_sql = (
                f"SELECT {_SELECT_COLUMNS} FROM search_event_log {where_clause} "
                f"ORDER BY timestamp DESC LIMIT ? OFFSET ?"
            )
            rows = conn.execute(data_sql, params + [limit, offset]).fetchall()
            events = [dict(row) for row in rows]
            return events, total_count
        finally:
            conn.close()

    def query_for_export(
        self,
        filters: dict,
        *,
        limit: int,
        after: Optional[Tuple[float, int]] = None,
    ) -> List[dict]:
        """Return one bounded batch of matching rows for an export (Bug #2106).

        Rows are ordered by (timestamp, id) ASC; at most `limit` rows strictly
        after the `after` keyset are returned. Every filter, including
        cache_hit_filter, is applied in SQL before the LIMIT. Any read failure
        propagates (Bug #2105). Used by QueryAnalyticsExportService.
        """
        sql, params = _export_select(filters, after, limit, "?", "1", "0")
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def oldest_timestamp(self) -> Optional[float]:
        """Timestamp of the oldest stored row, None when there is none
        (Story #2117). One index read; read failures propagate."""
        conn = sqlite3.connect(self._db_path, timeout=30)
        try:
            return _oldest_from_row(conn.execute(OLDEST_TIMESTAMP_SQL).fetchone())
        finally:
            conn.close()

    def get_hit_rate_counts(
        self,
        mode: str,
        from_ts: Optional[float] = None,
        to_ts: Optional[float] = None,
    ) -> Dict[str, int]:
        """Request-denominated hit/request counts for a cache mode (Issue #1257).

        Returns aggregated counts as {"hits": int, "requests": int}, optionally
        scoped to a half-open time window [from_ts, to_ts) when BOTH bounds
        are provided (Bug #1391) -- mirroring the exact filter pattern
        query_for_export already uses in this same class, and matching the
        windowing semantics of get_windowed_metrics elsewhere on the
        dashboard. When either bound is omitted, no time filter is applied
        and the counts cover the table's entire lifetime (the original,
        backward-compatible default).

        A row counts toward "requests" when EITHER provider recorded this mode
        for that request (voyage_cache_mode == mode OR cohere_cache_mode ==
        mode). A row counts toward "hits" when a provider recorded BOTH this
        mode AND a hit for that SAME provider on that SAME request. This is
        ROW-denominated (each row is one searched repository's own cache
        lookup, Bug #2111; rows are never merged by the client-supplied
        correlation id), matching the
        cache_hit_filter="hits_only" OR-combination semantics already used by
        query_for_export -- unlike the in-process QueryEmbeddingCacheMetrics
        tallies, which increment once per cache OPERATION and can therefore
        count a single request twice when it performs two on-mode embedding
        operations (e.g. voyage + cohere, or a shared-vector re-embed).

        Fail-open: returns {"hits": 0, "requests": 0} on any error.
        """
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                params: List[Any] = [mode, mode, mode, mode]
                windowed = from_ts is not None and to_ts is not None
                if windowed:
                    params.append(from_ts)
                    params.append(to_ts)
                row = conn.execute(_hit_rate_sql("?", "1", windowed), params).fetchone()
                requests_count = row[0] or 0
                hits_count = row[1] or 0
                return {"hits": int(hits_count), "requests": int(requests_count)}
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(
                "SearchEventLogSqliteBackend: get_hit_rate_counts failed: %s", exc
            )
            return {"hits": 0, "requests": 0}


# ---------------------------------------------------------------------------
# PostgreSQL backend
# ---------------------------------------------------------------------------


class SearchEventLogPostgresBackend:
    """PostgreSQL backend for search_event_log table (cluster mode)."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the search_event_log table and indexes if they do not exist."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_event_log (
                        id                  BIGSERIAL PRIMARY KEY,
                        timestamp           DOUBLE PRECISION NOT NULL,
                        username            TEXT NOT NULL,
                        repo_alias          TEXT,
                        search_type         TEXT NOT NULL,
                        query_text          TEXT NOT NULL,
                        voyage_cache_hit    BOOLEAN,
                        voyage_cache_mode   TEXT,
                        voyage_latency_ms   INTEGER,
                        cohere_cache_hit    BOOLEAN,
                        cohere_cache_mode   TEXT,
                        cohere_latency_ms   INTEGER,
                        total_latency_ms    INTEGER NOT NULL,
                        result_count        INTEGER NOT NULL,
                        node_id             TEXT NOT NULL,
                        correlation_id      TEXT,
                        query_text_truncated BOOLEAN,
                        tool                TEXT,
                        door                TEXT,
                        outcome             TEXT,
                        error_class         TEXT
                    )
                    """
                )
                # Bug #2114 / Story #2117 (also PG migrations 073/074): a
                # table created before a column existed gains it.
                _pg_add_missing_columns(conn)
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_sel_timestamp
                        ON search_event_log (timestamp)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_sel_user
                        ON search_event_log (username)
                    """
                )
                conn.execute(_CREATE_IDX_SEL_USER_TS)
                conn.commit()
        except Exception as exc:
            logger.warning(
                "SearchEventLogPostgresBackend: schema setup failed: %s", exc
            )

    def insert_batch(self, records: List[SearchEventRecord]) -> None:
        """Insert a batch of records. No-op for empty batch."""
        if not records:
            return
        try:
            with self._pool.connection() as conn:
                for r in records:
                    conn.execute(_insert_sql("%s"), _insert_values(r, sqlite=False))
                conn.commit()
        except Exception as exc:
            logger.warning(
                "SearchEventLogPostgresBackend: insert_batch failed: %s", exc
            )
            raise

    def prune_older_than(self, cutoff_timestamp: float) -> None:
        """Delete records older than cutoff_timestamp (Unix epoch seconds)."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    "DELETE FROM search_event_log WHERE timestamp < %s",
                    (cutoff_timestamp,),
                )
                conn.commit()
        except Exception as exc:
            logger.warning(
                "SearchEventLogPostgresBackend: prune_older_than failed: %s", exc
            )

    def query(
        self,
        username: Optional[str] = None,
        search_type: Optional[str] = None,
        repo_alias: Optional[str] = None,
        from_ts: Optional[float] = None,
        to_ts: Optional[float] = None,
        limit: int = 100,
        offset: int = 0,
        door: Optional[str] = None,
        outcome: Optional[str] = None,
        tool: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ):
        """Query events with optional filters.

        Time range is half-open: [from_ts, to_ts).
        Results ordered by timestamp DESC.
        Returns (events: list[dict], total_count: int).

        Bug #2105: any read failure propagates to the caller (see the SQLite
        backend's query()).
        """
        where_clause, params = _list_where(
            "%s",
            username,
            search_type,
            repo_alias,
            from_ts,
            to_ts,
            {
                "door": door,
                "outcome": outcome,
                "tool": tool,
                "correlation_id": correlation_id,
            },
        )

        with self._pool.connection() as conn:
            count_sql = f"SELECT COUNT(*) FROM search_event_log {where_clause}"
            total_count = conn.execute(count_sql, params).fetchone()[0]

            data_sql = (
                f"SELECT {_SELECT_COLUMNS} FROM search_event_log {where_clause} "
                f"ORDER BY timestamp DESC LIMIT %s OFFSET %s"
            )
            rows = conn.execute(data_sql, params + [limit, offset]).fetchall()
            events = [_pg_row_to_event(row) for row in rows]
            return events, total_count

    def query_for_export(
        self,
        filters: dict,
        *,
        limit: int,
        after: Optional[Tuple[float, int]] = None,
    ) -> List[dict]:
        """Return one bounded batch of matching rows for an export (Bug #2106).

        Same contract as SearchEventLogSqliteBackend.query_for_export: ordered
        by (timestamp, id) ASC, at most `limit` rows strictly after `after`,
        every filter applied in SQL before the LIMIT, read failures propagate.
        """
        sql, params = _export_select(filters, after, limit, "%s", "TRUE", "FALSE")
        with self._pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [_pg_row_to_event(row) for row in rows]

    def oldest_timestamp(self) -> Optional[float]:
        """Timestamp of the oldest stored row, None when there is none
        (Story #2117). One index read; read failures propagate."""
        with self._pool.connection() as conn:
            return _oldest_from_row(conn.execute(OLDEST_TIMESTAMP_SQL).fetchone())

    def get_hit_rate_counts(
        self,
        mode: str,
        from_ts: Optional[float] = None,
        to_ts: Optional[float] = None,
    ) -> Dict[str, int]:
        """Request-denominated hit/request counts for a cache mode (Issue #1257).

        See SearchEventLogSqliteBackend.get_hit_rate_counts for full rationale,
        including the Bug #1391 optional [from_ts, to_ts) half-open windowing
        (applied only when BOTH bounds are provided; omitting either preserves
        the original whole-table behavior). Cluster mode: this table is shared
        PostgreSQL, so the returned counts are automatically aggregated across
        every node -- no per-node merge is needed by the caller.

        Fail-open: returns {"hits": 0, "requests": 0} on any error.
        """
        try:
            with self._pool.connection() as conn:
                params: List[Any] = [mode, mode, mode, mode]
                windowed = from_ts is not None and to_ts is not None
                if windowed:
                    params.append(from_ts)
                    params.append(to_ts)
                row = conn.execute(
                    _hit_rate_sql("%s", "TRUE", windowed), params
                ).fetchone()
                requests_count = row[0] or 0
                hits_count = row[1] or 0
                return {"hits": int(hits_count), "requests": int(requests_count)}
        except Exception as exc:
            logger.warning(
                "SearchEventLogPostgresBackend: get_hit_rate_counts failed: %s", exc
            )
            return {"hits": 0, "requests": 0}


# ---------------------------------------------------------------------------
# Writer service
# ---------------------------------------------------------------------------


def _record_drop_metric() -> None:
    """Count one dropped search event in OTEL (Bug #2114). Never raises."""
    try:
        from code_indexer.server.telemetry.manager import peek_telemetry_manager
        from code_indexer.server.telemetry.metrics_instrumentation import (
            get_application_metrics,
        )

        telemetry_manager = peek_telemetry_manager()
        if telemetry_manager is None:
            return
        get_application_metrics(telemetry_manager).record_search_event_dropped()
    except Exception as exc:  # telemetry never breaks the hot path
        logger.debug("search event drop metric not recorded: %s", exc)


@dataclass(frozen=True)
class SearchEventDrops:
    """Events one writer (one server process) dropped on queue overflow."""

    dropped: int
    # None until the first drop.
    seconds_since_last_drop: Optional[float]


class SearchEventLogWriter:
    """Background writer for search event records.

    Maintains an in-memory queue and drains it in a background thread,
    writing batches to the storage backend. The hot path (enqueue) NEVER
    blocks and NEVER raises (Messi #2 anti-fallback, Messi #13 anti-silent-failure).

    Bug #2114: every event dropped on overflow is counted on this instance
    (per process, never shared state); `drop_snapshot()` reads the count for
    the health surface and the Query Analytics Export page.
    """

    def __init__(
        self,
        backend: Any,
        maxsize: int = 5000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._queue: Queue = Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._overflow_warned = False
        # 0.0 so the first _maybe_evict call always fires (Messi #14: provable bound).
        self._last_eviction: float = 0.0
        self._clock = clock
        self._drop_lock = threading.Lock()
        self._dropped = 0
        self._last_drop_at: Optional[float] = None

    def drop_snapshot(self) -> SearchEventDrops:
        """Events this writer dropped since it was created. O(1), no I/O."""
        with self._drop_lock:
            dropped, last_drop_at = self._dropped, self._last_drop_at
        return SearchEventDrops(
            dropped=dropped,
            seconds_since_last_drop=(
                None if last_drop_at is None else self._clock() - last_drop_at
            ),
        )

    @property
    def backend(self) -> Any:
        """Public read-only accessor for the underlying storage backend."""
        return self._backend

    def enqueue(self, record: SearchEventRecord) -> None:
        """Hot path — NEVER blocks, NEVER raises.

        Drops the newest event on queue overflow with a one-time WARNING
        (the flag resets after any successful enqueue so the next overflow
        will also warn once).
        """
        if self._stop.is_set():
            return
        try:
            self._queue.put_nowait(record)
            self._overflow_warned = False
        except Full:
            with self._drop_lock:
                self._dropped += 1
                self._last_drop_at = self._clock()
            _record_drop_metric()
            if not self._overflow_warned:
                logger.warning("search_event_log queue full — dropping newest events")
                self._overflow_warned = True

    def _drain(self) -> None:
        """Drain at most _MAX_DRAIN_BATCH records and write them to the backend."""
        batch: List[SearchEventRecord] = []
        for _ in range(_MAX_DRAIN_BATCH):
            try:
                record = self._queue.get_nowait()
                batch.append(record)
            except Empty:
                break
        if batch:
            self._backend.insert_batch(batch)

    def _maybe_evict(self) -> None:
        """Run prune_older_than at most once every 24 hours."""
        now = time.time()
        if now - self._last_eviction < _EVICTION_INTERVAL_SECONDS:
            return
        try:
            retention_days = (
                get_config_service_for_eviction()
                .get_config()
                .search_event_log_retention_days
            )
        except Exception as exc:
            logger.warning(
                "search_event_log: could not read retention config (%s); "
                "using default %d days",
                exc,
                DEFAULT_RETENTION_DAYS,
            )
            retention_days = DEFAULT_RETENTION_DAYS
        # Clamp to valid range regardless of source — defends against stale/corrupt config.
        retention_days = max(1, min(3650, retention_days))
        cutoff = now - (retention_days * SECONDS_PER_DAY)
        self._backend.prune_older_than(cutoff)
        self._last_eviction = now

    def _loop(self) -> None:
        """Background loop: drain + evict every 5 seconds, stop on signal."""
        while not self._stop.wait(timeout=5.0):
            try:
                self._drain()
                self._maybe_evict()
            except Exception as exc:
                logger.warning("search_event_log writer error: %s", exc)
        # Final drain on shutdown so no queued records are lost.
        while not self._queue.empty():
            try:
                self._drain()
            except Exception as exc:
                logger.warning("search_event_log final drain error: %s", exc)

    def start(self) -> None:
        """Start the background writer thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="search-event-log-writer",
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the background thread to stop and wait for it to drain."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "search_event_log writer thread did not stop within %.1fs timeout",
                    timeout,
                )
