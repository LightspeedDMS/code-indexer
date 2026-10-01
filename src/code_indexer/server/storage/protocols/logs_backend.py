"""LogsBackend Protocol (Story #500: LogsBackend Protocol and SQLite Wrapper).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Dict, List, Optional, Protocol, Tuple, runtime_checkable


@runtime_checkable
class LogsBackend(Protocol):
    """Protocol for operational log storage (Story #500).

    Supports both SQLite (standalone) and PostgreSQL (cluster) backends.
    Each node writes log records; the admin UI and REST API read them back
    with filtering and pagination.
    """

    is_cross_node_backend: bool

    def insert_log(
        self,
        timestamp: str,
        level: str,
        source: str,
        message: str,
        correlation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        request_path: Optional[str] = None,
        extra_data: Optional[str] = None,
        node_id: Optional[str] = None,
        alias: Optional[str] = None,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
    ) -> None:
        """Insert a single log record.

        Args:
            timestamp: ISO 8601 timestamp string.
            level: Log level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
            source: Logger name / source identifier.
            message: Formatted log message text.
            correlation_id: Optional request correlation ID.
            user_id: Optional user identifier.
            request_path: Optional HTTP request path.
            extra_data: Optional JSON-serialised extra fields.
            node_id: Optional cluster node identifier (NULL in standalone).
            alias: Optional repo alias (Story #876 Phase C). Tags lifecycle-runner
                ERROR rows so the admin UI can filter logs by repo.
            trace_id: Optional OTEL trace ID (Story #1676 AC2). 32-char hex,
                or the documented zero-value when no span was active.
            span_id: Optional OTEL span ID (Story #1676 AC2). 16-char hex,
                or the documented zero-value when no span was active.
        """
        ...

    def query_logs(
        self,
        level: Optional[str] = None,
        source: Optional[str] = None,
        correlation_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        node_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        levels: Optional[List[str]] = None,
        search: Optional[str] = None,
        sort_order: str = "desc",
    ) -> "Tuple[List[Dict], int]":
        """Query log records with optional filtering and pagination.

        Bug #1553: levels/search/sort_order are additive -- their defaults
        preserve the exact pre-existing behaviour for every caller that
        predates them.

        Args:
            level: Filter by log level (optional).
            source: Filter by logger name (optional).
            correlation_id: Filter by correlation ID (optional).
            date_from: ISO 8601 lower bound for timestamp (inclusive, optional).
            date_to: ISO 8601 upper bound for timestamp (inclusive, optional).
            node_id: Filter by cluster node ID (optional).
            limit: Maximum number of records to return (default 100).
            offset: Number of records to skip for pagination (default 0).
            levels: Filter by multiple log levels (optional, takes precedence
                over level).
            search: Case-insensitive substring match across message and
                correlation_id (optional).
            sort_order: "asc" or "desc" (default "desc" = newest first).

        Returns:
            Tuple of (list_of_log_dicts, total_count) where total_count reflects
            the full match count before pagination is applied.
        """
        ...

    def cleanup_old_logs(self, days_to_keep: int) -> int:
        """Delete log records older than days_to_keep days.

        Args:
            days_to_keep: Records with timestamp older than this many days are deleted.

        Returns:
            Number of rows deleted.
        """
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...
