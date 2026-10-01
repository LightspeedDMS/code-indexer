"""SyncJobsBackend Protocol (Story #410).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class SyncJobsBackend(Protocol):
    """Protocol for sync job management storage."""

    def create_job(
        self,
        job_id: str,
        username: str,
        user_alias: str,
        job_type: str,
        status: str,
        repository_url: Optional[str] = None,
    ) -> None: ...

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]: ...

    def update_job(self, job_id: str, **kwargs: Any) -> None: ...

    def list_jobs(self) -> list: ...

    def delete_job(self, job_id: str) -> bool: ...

    def cleanup_orphaned_jobs_on_startup(self) -> int: ...

    def cleanup_old_completed(self, cutoff_iso: str) -> int:
        """Delete completed or failed sync jobs older than cutoff_iso.

        Args:
            cutoff_iso: ISO 8601 timestamp; jobs with completed_at before
                        this value and status IN ('completed', 'failed') are deleted.

        Returns:
            Number of rows deleted.
        """
        ...

    def close(self) -> None: ...
