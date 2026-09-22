"""AuditLogBackend Protocol (AuditLogService storage interface, Story #410).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import List, Optional, Protocol, Tuple, runtime_checkable


@runtime_checkable
class AuditLogBackend(Protocol):
    """Protocol for audit log service storage (AuditLogService interface)."""

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

    def query(
        self,
        action_type: Optional[str] = None,
        target_type: Optional[str] = None,
        admin_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        exclude_target_type: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> Tuple[List[dict], int]: ...

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
