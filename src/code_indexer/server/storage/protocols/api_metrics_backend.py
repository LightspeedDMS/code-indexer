"""ApiMetricsBackend Protocol (Story #502: ApiMetricsBackend Protocol and SQLite Wrapper).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Dict, List, Optional, Protocol, Tuple, runtime_checkable


@runtime_checkable
class ApiMetricsBackend(Protocol):
    """Protocol for API metrics storage (Story #502).

    Supports both SQLite (standalone) and PostgreSQL (cluster) backends.
    Tracks rolling-window API call counts per category, optionally filtered
    by cluster node identifier.
    """

    def upsert_bucket(
        self,
        username: str,
        granularity: str,
        bucket_start: str,
        metric_type: str,
        node_id: str = "",
    ) -> None:
        """Upsert a single bucket row, incrementing count by 1.

        Args:
            username: Username for attribution (e.g. 'alice', '_anonymous').
            granularity: One of 'min1', 'min5', 'hour1', 'day1'.
            bucket_start: ISO 8601 timestamp of the bucket start boundary.
            metric_type: Category ('semantic', 'other_index', 'regex', 'other_api').
            node_id: Cluster node identifier. Empty string for standalone nodes.
                     Non-empty values must not be whitespace-only.
        """
        ...

    def cleanup_expired_buckets(self) -> None:
        """Delete expired bucket rows per granularity retention policy.

        Retention:
            min1  — 15 minutes
            min5  — 1 hour
            hour1 — 24 hours
            day1  — 15 days
        """
        ...

    def get_metrics_bucketed(
        self,
        period_seconds: int,
        username: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """Return metric totals from api_metrics_buckets for the given period.

        Args:
            period_seconds: Duration in seconds. Must be a key in PERIOD_TO_TIER.
            username: When provided, filter to this user's rows only.
                      When None, aggregate across all users.
            node_id: When provided, filter to this cluster node's rows only.
                     When None, aggregate across all nodes.

        Returns:
            Dict with keys: semantic, other_index, regex, other_api.
        """
        ...

    def get_metrics_by_user(
        self,
        period_seconds: int,
    ) -> Dict[str, Dict[str, int]]:
        """Return per-user metric totals from api_metrics_buckets for the given period.

        Args:
            period_seconds: Duration in seconds. Must be a key in PERIOD_TO_TIER.

        Returns:
            Dict mapping username to {metric_type: count}.
        """
        ...

    def get_metrics_timeseries(
        self,
        period_seconds: int,
    ) -> List[Tuple[str, str, int]]:
        """Return timeseries data from api_metrics_buckets for the given period.

        Args:
            period_seconds: Duration in seconds. Must be a key in PERIOD_TO_TIER.

        Returns:
            List of (bucket_start, metric_type, count) ordered by bucket_start ASC.
        """
        ...

    def reset(self) -> None:
        """Delete all metric records (used for testing / manual resets)."""
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...
