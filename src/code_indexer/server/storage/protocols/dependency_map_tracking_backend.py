"""DependencyMapTrackingBackend Protocol (Story #410).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, List, Protocol, runtime_checkable


@runtime_checkable
class DependencyMapTrackingBackend(Protocol):
    """Protocol for dependency map tracking storage."""

    def get_tracking(self) -> Dict[str, Any]: ...

    def update_tracking(
        self,
        last_run: Any = ...,
        next_run: Any = ...,
        status: Any = ...,
        commit_hashes: Any = ...,
        error_message: Any = ...,
        refinement_cursor: Any = ...,
        refinement_next_run: Any = ...,
    ) -> None: ...

    def cleanup_stale_status_on_startup(self) -> bool: ...

    def record_run_metrics(self, metrics: Dict[str, Any]) -> None: ...

    def get_run_history(self, limit: int = 5) -> List[Dict[str, Any]]: ...
