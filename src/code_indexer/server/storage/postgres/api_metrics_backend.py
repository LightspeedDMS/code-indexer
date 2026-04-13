"""
PostgreSQL backend for API metrics storage (Story #502).

Drop-in replacement for ApiMetricsSqliteBackend using psycopg v3 sync connections
via ConnectionPool.  Satisfies the ApiMetricsBackend Protocol (protocols.py).

Table created on first use (CREATE TABLE IF NOT EXISTS) so no separate
migration step is required.

Unlike error-path backends, metric insert failures are caught and logged as
warnings rather than propagated -- a failed metric write must never crash
the application.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Dict, List, Optional, Tuple

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)

# Valid values for bucket fields — used in upsert_bucket validation
_VALID_GRANULARITIES = frozenset({"min1", "min5", "hour1", "day1"})
_VALID_METRIC_TYPES = frozenset({"semantic", "other_index", "regex", "other_api"})

# Named period constants (seconds) — Story #673.
# Defined locally; datetime/timezone/timedelta are already imported above.
_PERIOD_15MIN: int = 900
_PERIOD_1H: int = 3_600
_PERIOD_24H: int = 86_400
_PERIOD_7D: int = 604_800
_PERIOD_15D: int = 1_296_000

_PERIOD_TO_TIER: Dict[int, str] = {
    _PERIOD_15MIN: "min1",
    _PERIOD_1H: "min5",
    _PERIOD_24H: "hour1",
    _PERIOD_7D: "day1",
    _PERIOD_15D: "day1",
}

_TIMESERIES_GROUP_HOURS: int = 2


def _resolve_tier_and_cutoff(period_seconds: int) -> Tuple[str, str]:
    """Resolve granularity tier and ISO cutoff for the given period.

    datetime, timezone, timedelta are imported at module level in this file.

    Args:
        period_seconds: Must be a key in _PERIOD_TO_TIER.

    Raises:
        ValueError: If period_seconds is not in _PERIOD_TO_TIER.
    """
    if period_seconds not in _PERIOD_TO_TIER:
        raise ValueError(
            f"period_seconds {period_seconds!r} not in _PERIOD_TO_TIER. "
            f"Valid values: {sorted(_PERIOD_TO_TIER)}"
        )
    tier = _PERIOD_TO_TIER[period_seconds]
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=period_seconds)
    ).isoformat()
    return tier, cutoff


# get_metrics_bucketed, get_metrics_by_user, get_metrics_timeseries are added
# as instance methods of ApiMetricsPostgresBackend — see class body below.
# They delegate to _resolve_tier_and_cutoff and use PostgreSQL %s placeholders.

# Retention window per granularity tier (Story #672) — immutable
_RETENTION_WINDOWS = MappingProxyType(
    {
        "min1": timedelta(minutes=15),
        "min5": timedelta(hours=1),
        "hour1": timedelta(hours=24),
        "day1": timedelta(days=15),
    }
)


class ApiMetricsPostgresBackend:
    """
    PostgreSQL backend for API metrics storage.

    Satisfies the ApiMetricsBackend Protocol (protocols.py).
    All mutations commit immediately after executing the DML statement.
    Read operations do not commit (auto-commit is fine for SELECT).

    Insert failures are swallowed with a warning so that metric writes never
    bring down the application.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        """
        Initialize with a shared connection pool and ensure the table exists.

        Args:
            pool: ConnectionPool instance providing psycopg v3 connections.
        """
        self._pool = pool
        self._ensure_schema()
        self._ensure_buckets_schema()

    def _ensure_buckets_schema(self) -> None:
        """Create the api_metrics_buckets table if it does not already exist.

        Includes node_id in the PRIMARY KEY so each cluster node maintains
        independent bucket rows. Migrates existing tables (without node_id)
        by adding the column and recreating the primary key constraint.
        """
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS api_metrics_buckets (
                        username      TEXT NOT NULL,
                        granularity   TEXT NOT NULL,
                        bucket_start  TEXT NOT NULL,
                        metric_type   TEXT NOT NULL,
                        node_id       TEXT NOT NULL DEFAULT '',
                        count         INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (username, granularity, bucket_start, metric_type, node_id)
                    )
                    """
                )
                # Migration: add node_id column if missing and recreate PK
                conn.execute(
                    """
                    ALTER TABLE api_metrics_buckets
                    ADD COLUMN IF NOT EXISTS node_id TEXT NOT NULL DEFAULT ''
                    """
                )
                conn.execute(
                    """
                    ALTER TABLE api_metrics_buckets
                    DROP CONSTRAINT IF EXISTS api_metrics_buckets_pkey
                    """
                )
                conn.execute(
                    """
                    ALTER TABLE api_metrics_buckets
                    ADD PRIMARY KEY (username, granularity, bucket_start, metric_type, node_id)
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.warning(
                "ApiMetricsPostgresBackend: buckets schema setup failed: %s", exc
            )

    def _ensure_schema(self) -> None:
        """Create the api_metrics table and indexes if they do not already exist."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS api_metrics (
                        id SERIAL PRIMARY KEY,
                        metric_type TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        node_id TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_api_metrics_pg_type_timestamp
                    ON api_metrics(metric_type, timestamp)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_api_metrics_pg_node_id
                    ON api_metrics(node_id)
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.warning("ApiMetricsPostgresBackend: schema setup failed: %s", exc)

    def insert_metric(
        self,
        metric_type: str,
        timestamp: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> None:
        """Insert a single metric record.

        Failures are caught and logged as warnings to prevent metric writes from
        crashing the application.

        Args:
            metric_type: Category ('semantic', 'other_index', 'regex', 'other_api').
            timestamp: ISO 8601 timestamp. Uses current UTC time when None.
            node_id: Optional cluster node identifier (NULL in standalone).
        """
        now = (
            timestamp
            if timestamp is not None
            else datetime.now(timezone.utc).isoformat()
        )
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO api_metrics (metric_type, timestamp, node_id)
                    VALUES (%s, %s, %s)
                    """,
                    (metric_type, now, node_id),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("ApiMetricsPostgresBackend: insert_metric failed: %s", exc)

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
        if granularity not in _VALID_GRANULARITIES:
            raise ValueError(
                f"Invalid granularity {granularity!r}. "
                f"Must be one of: {sorted(_VALID_GRANULARITIES)}"
            )
        if metric_type not in _VALID_METRIC_TYPES:
            raise ValueError(
                f"Invalid metric_type {metric_type!r}. "
                f"Must be one of: {sorted(_VALID_METRIC_TYPES)}"
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

        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO api_metrics_buckets
                        (username, granularity, bucket_start, metric_type, node_id, count)
                    VALUES (%s, %s, %s, %s, %s, 1)
                    ON CONFLICT (username, granularity, bucket_start, metric_type, node_id)
                    DO UPDATE SET count = api_metrics_buckets.count + 1
                    """,
                    (username, granularity, bucket_start, metric_type, node_id),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("ApiMetricsPostgresBackend: upsert_bucket failed: %s", exc)

    def cleanup_expired_buckets(self) -> None:
        """Delete expired bucket rows per granularity retention policy.

        Retention windows are defined by _RETENTION_WINDOWS:
            min1  — 15 minutes
            min5  — 1 hour
            hour1 — 24 hours
            day1  — 15 days
        """
        now = datetime.now(timezone.utc)
        try:
            with self._pool.connection() as conn:
                for granularity, window in _RETENTION_WINDOWS.items():
                    cutoff = (now - window).isoformat()
                    conn.execute(
                        "DELETE FROM api_metrics_buckets WHERE granularity = %s AND bucket_start < %s",
                        (granularity, cutoff),
                    )
                conn.commit()
        except Exception as exc:
            logger.warning(
                "ApiMetricsPostgresBackend: cleanup_expired_buckets failed: %s", exc
            )

    def get_metrics(
        self,
        window_seconds: int = 3600,
        node_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """Return metric counts within the rolling window.

        Args:
            window_seconds: Time window in seconds (default 3600 = 1 hour).
            node_id: When provided, filter to metrics from this node only.
                     When None, aggregate across all nodes.

        Returns:
            Dict with keys: semantic_searches, other_index_searches,
            regex_searches, other_api_calls.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        ).isoformat()

        try:
            with self._pool.connection() as conn:
                if node_id is not None:
                    rows = conn.execute(
                        """
                        SELECT metric_type, COUNT(*) as count
                        FROM api_metrics
                        WHERE timestamp >= %s AND node_id = %s
                        GROUP BY metric_type
                        """,
                        (cutoff, node_id),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT metric_type, COUNT(*) as count
                        FROM api_metrics
                        WHERE timestamp >= %s
                        GROUP BY metric_type
                        """,
                        (cutoff,),
                    ).fetchall()
        except Exception as exc:
            logger.warning("ApiMetricsPostgresBackend: get_metrics failed: %s", exc)
            rows = []

        counts = {row[0]: row[1] for row in rows}
        return {
            "semantic_searches": counts.get("semantic", 0),
            "other_index_searches": counts.get("other_index", 0),
            "regex_searches": counts.get("regex", 0),
            "other_api_calls": counts.get("other_api", 0),
        }

    def get_metrics_bucketed(
        self,
        period_seconds: int,
        username: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """Return metric totals from api_metrics_buckets for the given period.

        Uses module-level _resolve_tier_and_cutoff for tier and cutoff derivation.
        PostgreSQL %s placeholders are used for all parameters.

        Args:
            period_seconds: Must be a key in _PERIOD_TO_TIER.
            username: When provided, filter to this user. When None, aggregate all.
            node_id: When provided, filter to this cluster node. When None, aggregate all.

        Returns:
            Dict with keys: semantic_searches, other_index_searches,
            regex_searches, other_api_calls.
        """
        tier, cutoff = _resolve_tier_and_cutoff(period_seconds)
        try:
            with self._pool.connection() as conn:
                # Build query dynamically based on optional filters
                where_parts = ["granularity = %s", "bucket_start >= %s"]
                params: list = [tier, cutoff]
                if username is not None:
                    where_parts.append("username = %s")
                    params.append(username)
                if node_id is not None:
                    where_parts.append("node_id = %s")
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
        except Exception as exc:
            logger.warning(
                "ApiMetricsPostgresBackend: get_metrics_bucketed failed: %s", exc
            )
            rows = []

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

        Args:
            period_seconds: Must be a key in _PERIOD_TO_TIER.

        Returns:
            Dict mapping username to {metric_type: count}.
        """
        tier, cutoff = _resolve_tier_and_cutoff(period_seconds)
        try:
            with self._pool.connection() as conn:
                rows = conn.execute(
                    """
                    SELECT username, metric_type, SUM(count) AS total
                    FROM api_metrics_buckets
                    WHERE granularity = %s AND bucket_start >= %s
                    GROUP BY username, metric_type
                    ORDER BY username ASC, metric_type ASC
                    """,
                    (tier, cutoff),
                ).fetchall()
        except Exception as exc:
            logger.warning(
                "ApiMetricsPostgresBackend: get_metrics_by_user failed: %s", exc
            )
            rows = []

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

        For the 24h period (hour1 tier), buckets are grouped into _TIMESERIES_GROUP_HOURS-hour
        windows (max 12 buckets). All other periods use raw bucket granularity.
        PostgreSQL integer division is used for the 2-hour grouping.

        Args:
            period_seconds: Must be a key in _PERIOD_TO_TIER.

        Returns:
            List of (bucket_start, metric_type, count) ordered by bucket_start ASC.
        """
        tier, cutoff = _resolve_tier_and_cutoff(period_seconds)
        try:
            with self._pool.connection() as conn:
                if period_seconds == _PERIOD_24H:
                    # Group hour1 buckets into _TIMESERIES_GROUP_HOURS-hour windows.
                    # PostgreSQL integer division: (EXTRACT(HOUR FROM ...) / 2) * 2
                    rows = conn.execute(
                        """
                        SELECT
                            to_char(
                                date_trunc('day', bucket_start::timestamptz) +
                                (FLOOR(EXTRACT(HOUR FROM bucket_start::timestamptz) / %s) * %s
                                 || ' hours')::interval,
                                'YYYY-MM-DD"T"HH24:MI:SS+00:00'
                            ) AS grouped_bucket,
                            metric_type,
                            SUM(count) AS total
                        FROM api_metrics_buckets
                        WHERE granularity = %s AND bucket_start >= %s
                        GROUP BY grouped_bucket, metric_type
                        ORDER BY grouped_bucket ASC, metric_type ASC
                        """,
                        (
                            _TIMESERIES_GROUP_HOURS,
                            _TIMESERIES_GROUP_HOURS,
                            tier,
                            cutoff,
                        ),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT bucket_start, metric_type, SUM(count) AS total
                        FROM api_metrics_buckets
                        WHERE granularity = %s AND bucket_start >= %s
                        GROUP BY bucket_start, metric_type
                        ORDER BY bucket_start ASC, metric_type ASC
                        """,
                        (tier, cutoff),
                    ).fetchall()
        except Exception as exc:
            logger.warning(
                "ApiMetricsPostgresBackend: get_metrics_timeseries failed: %s", exc
            )
            rows = []

        return [(row[0], row[1], int(row[2])) for row in rows]

    def cleanup_old(self, max_age_seconds: int = 86400) -> int:
        """Delete metric records older than max_age_seconds.

        Args:
            max_age_seconds: Records older than this many seconds are deleted
                             (default 86400 = 24 hours).

        Returns:
            Number of rows deleted.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        ).isoformat()

        with self._pool.connection() as conn:
            result = conn.execute(
                "DELETE FROM api_metrics WHERE timestamp < %s",
                (cutoff,),
            )
            deleted = int(result.rowcount) if result.rowcount else 0
            conn.commit()

        if deleted:
            logger.debug(
                "ApiMetricsPostgresBackend: cleaned up %d old metric records", deleted
            )
        return deleted

    def reset(self) -> None:
        """Delete all metric records (used for testing / manual resets)."""
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM api_metrics")
            conn.commit()

    def close(self) -> None:
        """No-op: pool lifecycle is managed externally."""
