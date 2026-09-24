"""ResearchSessionsBackend Protocol (Story #522).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class ResearchSessionsBackend(Protocol):
    """Protocol for research sessions storage (Story #522).

    Provides data-level access to research_sessions and research_messages tables.
    Satisfies PEP 544 structural subtyping: any class implementing all of
    these methods is accepted as a ResearchSessionsBackend without inheritance.
    """

    def create_session(
        self,
        session_id: str,
        name: str,
        folder_path: str,
        claude_session_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None: ...

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]: ...

    def list_sessions(self) -> List[Dict[str, Any]]: ...

    def delete_session(self, session_id: str) -> bool: ...

    def update_session_title(self, session_id: str, name: str) -> bool: ...

    def update_session_claude_id(
        self, session_id: str, claude_session_id: str
    ) -> None: ...

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]: ...

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]: ...

    def close(self) -> None: ...
