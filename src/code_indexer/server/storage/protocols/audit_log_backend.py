"""AuditLogBackend Protocol (AuditLogService storage interface, Story #410).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from ._shared import List, Optional, Protocol, Tuple, runtime_checkable

if TYPE_CHECKING:
    from code_indexer.server.services.audit_events import AuditEvent
    from code_indexer.server.services.audit_log_query import AuditFilters


@runtime_checkable
class AuditLogBackend(Protocol):
    """Protocol for audit log service storage (AuditLogService interface)."""

    def insert_events(self, events: "Sequence[AuditEvent]") -> None:
        """Insert *events* in ONE transaction; raise on failure.

        The single write function of the unified audit capture path.
        """
        ...

    def log(
        self,
        admin_id: str,
        action_type: str,
        target_type: str,
        target_id: str,
        details: Optional[str] = None,
    ) -> None: ...

    def log_raw(
        self,
        timestamp: str,
        admin_id: str,
        action_type: str,
        target_type: str,
        target_id: str,
        details: Optional[str] = None,
    ) -> None: ...

    # Shared read path (services/audit_log_query.py renders the SQL); the
    # ONE way rows are read for the Web page, MCP and REST.

    def query_page(
        self,
        filters: "AuditFilters",
        tier: str,
        *,
        seek: Optional[Tuple[str, int]],
        direction: str,
        limit: int,
        offset: int = 0,
    ) -> List[dict]:
        """One keyset page ordered by ``(timestamp, id)`` in *direction*."""
        ...

    def count_capped(self, filters: "AuditFilters", tier: str, *, cap: int) -> int:
        """Matching row count, reading at most ``cap + 1`` rows."""
        ...

    def aggregate(
        self, filters: "AuditFilters", tier: str, *, max_groups: int
    ) -> List[dict]:
        """SQL ``GROUP BY (action_type, outcome)`` of the matching rows."""
        ...

    def find_terminal_rows(self, correlation_ids: "Sequence[str]") -> List[dict]:
        """Non-attempted rows sharing one of *correlation_ids*."""
        ...

    def get_pr_logs(
        self,
        repo_alias: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]: ...

    def get_cleanup_logs(
        self,
        repo_path: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]: ...

    def cleanup_old_logs(self, cutoff_iso: str) -> int:
        """Delete audit log records older than cutoff_iso.

        Args:
            cutoff_iso: ISO 8601 timestamp; records with timestamp before
                        this value are deleted.

        Returns:
            Number of rows deleted.
        """
        ...
