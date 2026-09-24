"""DescriptionRefreshTrackingBackend Protocol (Story #410).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class DescriptionRefreshTrackingBackend(Protocol):
    """Protocol for description refresh tracking storage."""

    def get_tracking_record(self, repo_alias: str) -> Optional[Dict[str, Any]]: ...

    def get_stale_repos(self, now_iso: str) -> List[Dict[str, Any]]: ...

    def upsert_tracking(self, repo_alias: str, **fields: Any) -> None: ...

    def delete_tracking(self, repo_alias: str) -> bool: ...

    def get_all_tracking(self) -> List[Dict[str, Any]]: ...

    def close(self) -> None: ...
