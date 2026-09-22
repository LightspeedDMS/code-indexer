"""RepoCategoryBackend Protocol (Story #410).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class RepoCategoryBackend(Protocol):
    """Protocol for repository category management storage."""

    def create_category(self, name: str, pattern: str, priority: int) -> int: ...

    def list_categories(self) -> List[Dict[str, Any]]: ...

    def get_category(self, category_id: int) -> Optional[Dict[str, Any]]: ...

    def update_category(self, category_id: int, name: str, pattern: str) -> None: ...

    def delete_category(self, category_id: int) -> None: ...

    def reorder_categories(self, ordered_ids: List[int]) -> None: ...

    def shift_all_priorities(self) -> None: ...

    def get_next_priority(self) -> int: ...

    def get_repo_category_map(self) -> Dict[str, Dict[str, Any]]: ...

    def close(self) -> None: ...
