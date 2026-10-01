"""
SQLite backend for API metrics storage (Story #502).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import os
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Tuple

from ..database_manager import DatabaseConnectionManager

_API_METRICS_BUCKETS_COLUMNS_DDL = """
    username      TEXT NOT NULL,
    granularity   TEXT NOT NULL,
    bucket_start  TEXT NOT NULL,
    metric_type   TEXT NOT NULL,
    node_id       TEXT NOT NULL DEFAULT '',
    count         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (username, granularity, bucket_start, metric_type, node_id)
"""

# Period-to-tier mapping for bucketed query methods (Story #673).
# Maps period_seconds -> granularity tier stored in api_metrics_buckets.
PERIOD_TO_TIER: Dict[int, str] = {
    900: "min1",  # 15 minutes  -> 1-minute buckets
    3600: "min5",  # 1 hour      -> 5-minute buckets
    86400: "hour1",  # 24 hours    -> 1-hour buckets
    604800: "day1",  # 7 days      -> 1-day buckets
    1296000: "day1",  # 15 days     -> 1-day buckets
}


class ApiMetricsSqliteBackend:
    """
    SQLite backend for API metrics storage (Story #502).

    Stores rolling-window API call timestamps so the dashboard can report
    semantic_searches, other_index_searches, regex_searches, and other_api_calls
    within any time window.

    Uses a dedicated api_metrics.db file (separate from the main cidx_server.db)
    to isolate high-volume metric writes from other server state.

    Includes a node_id column for cluster support — each node tags its own
    metrics so per-node filtering is possible.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend and create the api_metrics table if it does not exist.

        Args:
            db_path: Path to SQLite database file
                     (e.g. ~/.cidx-server/data/api_metrics.db).
        """
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db_path = db_path
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

        # Enable WAL mode outside any transaction (PRAGMA cannot run inside BEGIN).
        conn = self._conn_manager.get_connection()
        conn.execute("PRAGMA journal_mode=WAL")

        self._ensure_legacy_api_metrics_schema()
        self._ensure_buckets_schema()

    def _ensure_legacy_api_metrics_schema(self) -> None:
        """Create the legacy api_metrics table and indexes if they do not already exist.

        This table is no longer written to by the current code, but MUST remain
        present for rolling-restart backward compatibility: old nodes in a cluster
        still write to it.  Never drop this table.
        """

        def operation(conn: Any) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    node_id TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_api_metrics_type_timestamp
                ON api_metrics(metric_type, timestamp)
                """
            )
            # Migrate existing databases: add node_id column if missing
            # (must run BEFORE creating the index on node_id)
            cursor = conn.execute("PRAGMA table_info(api_metrics)")
            columns = {row[1] for row in cursor.fetchall()}
            if "node_id" not in columns:
                conn.execute("ALTER TABLE api_metrics ADD COLUMN node_id TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_metrics_node_id ON api_metrics(node_id)"
            )

        self._conn_manager.execute_atomic(operation)

    # Valid values for bucket fields — used in upsert_bucket validation
    _VALID_GRANULARITIES = frozenset({"min1", "min5", "hour1", "day1"})
    _VALID_METRIC_TYPES = frozenset({"semantic", "other_index", "regex", "other_api"})

    # Retention window per granularity tier (Story #672) — immutable
    _RETENTION_WINDOWS = MappingProxyType(
        {
            "min1": timedelta(minutes=15),
            "min5": timedelta(hours=1),
            "hour1": timedelta(hours=24),
            "day1": timedelta(days=15),
        }
    )

    def _ensure_buckets_schema(self) -> None:
        """Create the api_metrics_buckets table if it does not already exist.

        Includes node_id in the PRIMARY KEY so each cluster node maintains
        independent bucket rows. Migrates existing tables (without node_id)
        by renaming, recreating with the new schema, copying data, then
        dropping the old table.
        """

        def operation(conn: Any) -> None:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS api_metrics_buckets ("
                f"{_API_METRICS_BUCKETS_COLUMNS_DDL})"
            )
            # Migration: if table exists but node_id column is missing,
            # recreate with node_id in the PK (ALTER TABLE cannot change PK in SQLite)
            cursor = conn.execute("PRAGMA table_info(api_metrics_buckets)")
            columns = {row[1] for row in cursor.fetchall()}
            if "node_id" not in columns:
                conn.execute(
                    "ALTER TABLE api_metrics_buckets RENAME TO _api_metrics_buckets_old"
                )
                conn.execute(
                    f"CREATE TABLE api_metrics_buckets ("
                    f"{_API_METRICS_BUCKETS_COLUMNS_DDL})"
                )
                conn.execute(
                    """
                    INSERT INTO api_metrics_buckets
                        (username, granularity, bucket_start, metric_type, node_id, count)
                    SELECT username, granularity, bucket_start, metric_type, '', count
                    FROM _api_metrics_buckets_old
                    """
                )
                conn.execute("DROP TABLE _api_metrics_buckets_old")

        self._conn_manager.execute_atomic(operation)

    def upsert_bucket(
        self,
        username: str,
        granularity: str,
        bucket_start: str,
        metric_type: str,
        node_id: str = "",
    ) -> None:
        """Upsert a bucket row — increment count by 1, creating the row if needed.

        Args:
            username: Non-empty username for attribution.
            granularity: One of 'min1', 'min5', 'hour1', 'day1'.
            bucket_start: ISO 8601 timestamp of the bucket boundary.
            metric_type: Category ('semantic', 'other_index', 'regex', 'other_api').
            node_id: Cluster node identifier. Empty string for standalone nodes.
                     Non-empty values must not be whitespace-only.

        Raises:
            ValueError: If any argument fails validation.
        """
        if not isinstance(username, str) or not username.strip():
            raise ValueError(f"username must be a non-empty string, got {username!r}")
        if granularity not in self._VALID_GRANULARITIES:
            raise ValueError(
                f"Invalid granularity {granularity!r}. "
                f"Must be one of: {sorted(self._VALID_GRANULARITIES)}"
            )
        if metric_type not in self._VALID_METRIC_TYPES:
            raise ValueError(
                f"Invalid metric_type {metric_type!r}. "
                f"Must be one of: {sorted(self._VALID_METRIC_TYPES)}"
            )
        try:
            datetime.fromisoformat(bucket_start)
        except (ValueError, TypeError):
            raise ValueError(
                f"bucket_start must be a valid ISO 8601 datetime string, got {bucket_start!r}"
            )
        if not isinstance(node_id, str):
            raise ValueError(f"node_id must be a string, got {node_id!r}")
        if node_id != "" and not node_id.strip():
            raise ValueError(f"node_id must not be whitespace-only, got {node_id!r}")

        def operation(conn: Any) -> None:
            conn.execute(
                """
                INSERT INTO api_metrics_buckets
                    (username, granularity, bucket_start, metric_type, node_id, count)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(username, granularity, bucket_start, metric_type, node_id)
                DO UPDATE SET count = count + 1
                """,
                (username, granularity, bucket_start, metric_type, node_id),
            )

        self._conn_manager.execute_atomic(operation)

    def upsert_buckets_batch(
        self,
        events: Any,
        node_id: str = "",
    ) -> None:
        """Upsert MANY bucket increments in ONE transaction (Story #1083).

        The background metrics writer drains the whole queue and calls this once
        per drain instead of one BEGIN EXCLUSIVE transaction per metric event,
        collapsing ~4N DB-wide-exclusive transactions into 1.

        Args:
            events: Iterable of dicts, each ``{"username": str, "metric_type": str,
                "buckets": {granularity: bucket_start_iso, ...}}`` — one per drained
                metric event. The four-tier bucket map is precomputed by the caller.
            node_id: Cluster node identifier. Empty string for standalone nodes.

        Raises:
            ValueError: If any event fails the same field validation as
                ``upsert_bucket`` (username, granularity, metric_type, bucket_start,
                node_id).

        Counts are preserved exactly: repeated (username, granularity, bucket_start,
        metric_type, node_id) keys within the batch are coalesced into a single
        ``count + N`` increment so the total equals the number of events.
        """
        if not isinstance(node_id, str):
            raise ValueError(f"node_id must be a string, got {node_id!r}")
        if node_id != "" and not node_id.strip():
            raise ValueError(f"node_id must not be whitespace-only, got {node_id!r}")

        # Coalesce identical bucket keys into a single +N increment. Validation
        # mirrors upsert_bucket so the batch path rejects exactly what the single
        # path would (no silent acceptance of bad input — MESSI #13).
        coalesced: Dict[Tuple[str, str, str, str], int] = {}
        for event in events:
            username = event["username"]
            metric_type = event["metric_type"]
            buckets = event["buckets"]
            if not isinstance(username, str) or not username.strip():
                raise ValueError(
                    f"username must be a non-empty string, got {username!r}"
                )
            if metric_type not in self._VALID_METRIC_TYPES:
                raise ValueError(
                    f"Invalid metric_type {metric_type!r}. "
                    f"Must be one of: {sorted(self._VALID_METRIC_TYPES)}"
                )
            for granularity, bucket_start in buckets.items():
                if granularity not in self._VALID_GRANULARITIES:
                    raise ValueError(
                        f"Invalid granularity {granularity!r}. "
                        f"Must be one of: {sorted(self._VALID_GRANULARITIES)}"
                    )
                try:
                    datetime.fromisoformat(bucket_start)
                except (ValueError, TypeError):
                    raise ValueError(
                        f"bucket_start must be a valid ISO 8601 datetime string, "
                        f"got {bucket_start!r}"
                    )
                key = (username, granularity, bucket_start, metric_type)
                coalesced[key] = coalesced.get(key, 0) + 1

        if not coalesced:
            return

        def operation(conn: Any) -> None:
            for (
                username,
                granularity,
                bucket_start,
                metric_type,
            ), inc in coalesced.items():
                conn.execute(
                    """
                    INSERT INTO api_metrics_buckets
                        (username, granularity, bucket_start, metric_type, node_id, count)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(username, granularity, bucket_start, metric_type, node_id)
                    DO UPDATE SET count = count + ?
                    """,
                    (
                        username,
                        granularity,
                        bucket_start,
                        metric_type,
                        node_id,
                        inc,
                        inc,
                    ),
                )

        self._conn_manager.execute_atomic(operation)

    def cleanup_expired_buckets(self) -> None:
        """Delete expired bucket rows per granularity retention policy.

        Retention windows are defined by _RETENTION_WINDOWS:
            min1  — 15 minutes
            min5  — 1 hour
            hour1 — 24 hours
            day1  — 15 days
        """
        now = datetime.now(timezone.utc)

        def operation(conn: Any) -> None:
            for granularity, window in self._RETENTION_WINDOWS.items():
                cutoff = (now - window).isoformat()
                conn.execute(
                    "DELETE FROM api_metrics_buckets WHERE granularity = ? AND bucket_start < ?",
                    (granularity, cutoff),
                )

        self._conn_manager.execute_atomic(operation)

    # Seconds in 24 hours — identifies the period that uses 2-hour grouping
    _PERIOD_24H_SECONDS: int = 86400

    # Hours per timeseries group for the 24h period (12 buckets total)
    _TIMESERIES_GROUP_HOURS: int = 2

    def _resolve_tier_and_cutoff(self, period_seconds: int) -> Tuple[str, str]:
        """Resolve granularity tier and ISO cutoff for a given period.

        Args:
            period_seconds: Duration in seconds. Must be a key in PERIOD_TO_TIER.

        Returns:
            (tier, cutoff_iso) where tier is the granularity string and
            cutoff_iso is the ISO 8601 lower bound for bucket_start queries.

        Raises:
            ValueError: If period_seconds is not in PERIOD_TO_TIER.
        """
        if period_seconds not in PERIOD_TO_TIER:
            raise ValueError(
                f"period_seconds {period_seconds!r} not in PERIOD_TO_TIER. "
                f"Valid values: {sorted(PERIOD_TO_TIER)}"
            )
        tier = PERIOD_TO_TIER[period_seconds]
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=period_seconds)
        ).isoformat()
        return tier, cutoff

    def get_metrics_bucketed(
        self,
        period_seconds: int,
        username: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """Return metric totals from api_metrics_buckets for the given period.

        Maps period_seconds to a granularity tier via PERIOD_TO_TIER, then
        sums counts from all bucket rows within the rolling window.

        Args:
            period_seconds: Duration in seconds. Must be a key in PERIOD_TO_TIER.
            username: When provided, filter to this user's rows only.
                      When None, aggregate across all users.
            node_id: When provided, filter to this cluster node's rows only.
                     When None, aggregate across all nodes.

        Returns:
            Dict with keys: semantic_searches, other_index_searches,
            regex_searches, other_api_calls — each mapped to the integer sum
            of counts in the period.

        Raises:
            ValueError: If period_seconds is not in PERIOD_TO_TIER.
        """
        tier, cutoff = self._resolve_tier_and_cutoff(period_seconds)

        conn = self._conn_manager.get_connection()
        # Build query dynamically based on optional filters
        where_parts = ["granularity = ?", "bucket_start >= ?"]
        params: list = [tier, cutoff]
        if username is not None:
            where_parts.append("username = ?")
            params.append(username)
        if node_id is not None:
            where_parts.append("node_id = ?")
            params.append(node_id)
        where_clause = " AND ".join(where_parts)
        rows = conn.execute(
            f"""
            SELECT metric_type, SUM(count) AS total
            FROM api_metrics_buckets
            WHERE {where_clause}
            GROUP BY metric_type
            """,
            params,
        ).fetchall()

        counts = {row[0]: int(row[1]) for row in rows}
        return {
            "semantic_searches": counts.get("semantic", 0),
            "other_index_searches": counts.get("other_index", 0),
            "regex_searches": counts.get("regex", 0),
            "other_api_calls": counts.get("other_api", 0),
        }

    def get_metrics_by_user(
        self,
        period_seconds: int,
    ) -> Dict[str, Dict[str, int]]:
        """Return per-user metric totals from api_metrics_buckets for the given period.

        Maps period_seconds to a granularity tier via PERIOD_TO_TIER, then
        groups by username and metric_type within the rolling window.

        Args:
            period_seconds: Duration in seconds. Must be a key in PERIOD_TO_TIER.

        Returns:
            Dict mapping username to {metric_type: count}.
            Example: {"alice": {"semantic": 5, "regex": 2}, "bob": {"semantic": 3}}

        Raises:
            ValueError: If period_seconds is not in PERIOD_TO_TIER.
        """
        tier, cutoff = self._resolve_tier_and_cutoff(period_seconds)

        conn = self._conn_manager.get_connection()
        rows = conn.execute(
            """
            SELECT username, metric_type, SUM(count) AS total
            FROM api_metrics_buckets
            WHERE granularity = ? AND bucket_start >= ?
            GROUP BY username, metric_type
            ORDER BY username ASC, metric_type ASC
            """,
            (tier, cutoff),
        ).fetchall()

        result: Dict[str, Dict[str, int]] = {}
        for row_username, metric_type, total in rows:
            if row_username not in result:
                result[row_username] = {}
            result[row_username][metric_type] = int(total)
        return result

    def get_metrics_timeseries(
        self,
        period_seconds: int,
    ) -> List[Tuple[str, str, int]]:
        """Return timeseries data from api_metrics_buckets for the given period.

        Maps period_seconds to a granularity tier via PERIOD_TO_TIER.
        For the 24h period (hour1 tier), buckets are grouped into 2-hour windows
        producing at most 12 data points. All other periods use raw bucket granularity.

        Args:
            period_seconds: Duration in seconds. Must be a key in PERIOD_TO_TIER.

        Returns:
            List of (bucket_start, metric_type, count) tuples ordered by
            bucket_start ASC. bucket_start is an ISO 8601 string.

        Raises:
            ValueError: If period_seconds is not in PERIOD_TO_TIER.
        """
        tier, cutoff = self._resolve_tier_and_cutoff(period_seconds)

        conn = self._conn_manager.get_connection()

        if period_seconds == self._PERIOD_24H_SECONDS:
            # Group hour1 buckets into _TIMESERIES_GROUP_HOURS-hour windows → max 12 buckets.
            # CAST integer division ensures correct floor: e.g. hour 3 → (3/2)*2 = 2.
            rows = conn.execute(
                """
                SELECT
                    strftime('%Y-%m-%dT', bucket_start) ||
                    printf('%02d',
                        CAST(CAST(strftime('%H', bucket_start) AS INTEGER) / ? AS INTEGER) * ?
                    ) || ':00:00' AS grouped_bucket,
                    metric_type,
                    SUM(count) AS total
                FROM api_metrics_buckets
                WHERE granularity = ? AND bucket_start >= ?
                GROUP BY grouped_bucket, metric_type
                ORDER BY grouped_bucket ASC, metric_type ASC
                """,
                (
                    self._TIMESERIES_GROUP_HOURS,
                    self._TIMESERIES_GROUP_HOURS,
                    tier,
                    cutoff,
                ),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT bucket_start, metric_type, SUM(count) AS total
                FROM api_metrics_buckets
                WHERE granularity = ? AND bucket_start >= ?
                GROUP BY bucket_start, metric_type
                ORDER BY bucket_start ASC, metric_type ASC
                """,
                (tier, cutoff),
            ).fetchall()

        return [(row[0], row[1], int(row[2])) for row in rows]

    def reset(self) -> None:
        """Delete all bucket data (used for testing / manual resets)."""

        def operation(conn: Any) -> None:
            conn.execute("DELETE FROM api_metrics_buckets")

        self._conn_manager.execute_atomic(operation)

    def close(self) -> None:
        """No-op: connections are managed by DatabaseConnectionManager."""
        pass
