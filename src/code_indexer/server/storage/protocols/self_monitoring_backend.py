"""SelfMonitoringBackend Protocol (Story #524).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable


@runtime_checkable
class SelfMonitoringBackend(Protocol):
    """Protocol for self-monitoring storage (Story #524).

    Provides data-level access to self_monitoring_scans and
    self_monitoring_issues tables.
    Satisfies PEP 544 structural subtyping: any class implementing all of
    these methods is accepted as a SelfMonitoringBackend without inheritance.
    """

    def create_scan_record(
        self,
        scan_id: str,
        started_at: str,
        log_id_start: int,
    ) -> None:
        """Insert initial scan record with RUNNING status."""
        ...

    def get_last_scan_log_id(self) -> int:
        """Return log_id_end from most recent SUCCESS scan, or 0."""
        ...

    def update_scan_record(
        self,
        scan_id: str,
        status: str,
        completed_at: str,
        log_id_end: "Optional[int]" = None,
        issues_created: "Optional[int]" = None,
        error_message: "Optional[str]" = None,
    ) -> None:
        """Update scan record with completion status and metrics."""
        ...

    def cleanup_orphaned_scans(self, cutoff_iso: str) -> int:
        """Mark scans started before cutoff_iso with no completed_at as FAILURE.

        Returns count of scans updated.
        """
        ...

    def get_last_started_at(self) -> "Optional[str]":
        """Return started_at from most recent scan (any status), or None."""
        ...

    def fetch_stored_fingerprints(
        self, retention_days: int
    ) -> "List[Tuple[str, str, str, str, str]]":
        """Return fingerprint rows (fingerprint, classification, error_codes, title, created_at)."""
        ...

    def store_issue_metadata(
        self,
        scan_id: str,
        github_issue_number: "Optional[int]",
        github_issue_url: "Optional[str]",
        classification: str,
        title: str,
        error_codes: str,
        fingerprint: str,
        source_log_ids: str,
        source_files: str,
        created_at: str,
    ) -> None:
        """Persist issue metadata in self_monitoring_issues."""
        ...

    def list_scans(self, limit: int = 50) -> "List[Dict[str, Any]]":
        """Return scan history records, most recent first.

        Args:
            limit: Maximum number of records to return.

        Returns:
            List of dicts with keys: scan_id, started_at, completed_at, status,
            log_id_start, log_id_end, issues_created, error_message.
        """
        ...

    def list_issues(self, limit: int = 100) -> "List[Dict[str, Any]]":
        """Return issue records, most recent first.

        Args:
            limit: Maximum number of records to return.

        Returns:
            List of dicts with keys: id, scan_id, github_issue_number,
            github_issue_url, classification, title, fingerprint,
            source_log_ids, source_files, created_at.
        """
        ...

    def get_running_scan_count(self) -> int:
        """Return count of scans where completed_at IS NULL (currently running).

        Returns:
            Integer count of running scans.
        """
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...
