"""GlobalReposBackend Protocol (Story #410).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class GlobalReposBackend(Protocol):
    """Protocol for global repository registry storage."""

    def register_repo(
        self,
        alias_name: str,
        repo_name: str,
        repo_url: Optional[str],
        index_path: str,
        enable_temporal: bool = False,
        temporal_options: Optional[Dict[str, Any]] = None,
        enable_scip: bool = False,
    ) -> None: ...

    def get_repo(self, alias_name: str) -> Optional[Dict[str, Any]]: ...

    def list_repos(self) -> Dict[str, Dict[str, Any]]: ...

    def delete_repo(self, alias_name: str) -> bool: ...

    def update_last_refresh(self, alias_name: str) -> bool: ...

    def update_enable_temporal(
        self, alias_name: str, enable_temporal: bool
    ) -> bool: ...

    def update_enable_scip(self, alias_name: str, enable_scip: bool) -> bool: ...

    def update_next_refresh(
        self, alias_name: str, next_refresh: Optional[str]
    ) -> bool: ...

    def list_due_repos(self, limit: int, now: float) -> List[Dict[str, Any]]: ...

    def close(self) -> None: ...
