"""BackgroundJobsBackend Protocol (Story #410).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class BackgroundJobsBackend(Protocol):
    """Protocol for background job management storage."""

    def save_job(
        self,
        job_id: str,
        operation_type: str,
        status: str,
        created_at: str,
        username: str,
        progress: int,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        is_admin: bool = False,
        cancelled: bool = False,
        repo_alias: Optional[str] = None,
        resolution_attempts: int = 0,
        claude_actions: Optional[List[str]] = None,
        failure_reason: Optional[str] = None,
        extended_error: Optional[Dict[str, Any]] = None,
        language_resolution_status: Optional[Dict[str, Dict[str, Any]]] = None,
        progress_info: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        executing_node: Optional[str] = None,
        claimed_at: Optional[str] = None,
    ) -> None: ...

    def atomic_claim_insert(
        self,
        job_id: str,
        operation_type: str,
        status: str,
        created_at: str,
        username: str,
        progress: int,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        is_admin: bool = False,
        cancelled: bool = False,
        repo_alias: Optional[str] = None,
        resolution_attempts: int = 0,
        claude_actions: Optional[List[str]] = None,
        failure_reason: Optional[str] = None,
        extended_error: Optional[Dict[str, Any]] = None,
        language_resolution_status: Optional[Dict[str, Dict[str, Any]]] = None,
        current_phase: Optional[str] = None,
        phase_detail: Optional[str] = None,
        progress_info: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        executing_node: Optional[str] = None,
        claimed_at: Optional[str] = None,
        actor_username: Optional[str] = None,
    ) -> None: ...

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]: ...

    def update_job(self, job_id: str, **kwargs: Any) -> None: ...

    def list_jobs(
        self,
        username: Optional[str] = None,
        status: Optional[str] = None,
        operation_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]: ...

    def list_jobs_filtered(
        self,
        status: Optional[str] = None,
        operation_type: Optional[str] = None,
        search_text: Optional[str] = None,
        exclude_ids: Optional[Any] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        username: Optional[str] = None,
    ) -> tuple: ...

    def list_job_ids_filtered(
        self,
        status: Optional[str] = None,
        operation_type: Optional[str] = None,
        search_text: Optional[str] = None,
        username: Optional[str] = None,
        cap: Optional[int] = None,
    ) -> set: ...

    def delete_job(self, job_id: str) -> bool: ...

    def cleanup_old_jobs(self, max_age_hours: int = 24) -> int: ...

    def count_jobs_by_status(self) -> Dict[str, int]: ...

    def get_job_stats(self, time_filter: str = "24h") -> Dict[str, int]: ...

    def cleanup_orphaned_jobs_on_startup(self) -> int: ...

    def find_active_job_by_type_and_alias(
        self,
        operation_type: str,
        repo_alias: str,
    ) -> Optional[str]:
        """Return job_id of the active (pending/running) row for (operation_type, repo_alias).

        Direct non-paginated lookup — no Python-side filtering, no LIMIT/OFFSET.
        Called by _find_blocking_active_job_id after a unique-index violation to
        locate the blocking row without risking a pagination miss (Bug #1220).

        Returns:
            job_id string if a pending or running row exists, else None.
        """
        ...

    def close(self) -> None: ...
