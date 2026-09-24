"""SessionsBackend Protocol (Story #410).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Optional, Protocol, runtime_checkable


@runtime_checkable
class SessionsBackend(Protocol):
    """Protocol for session management storage."""

    def invalidate_session(self, username: str, token_id: str) -> None: ...

    def is_session_invalidated(self, username: str, token_id: str) -> bool: ...

    def clear_invalidated_sessions(self, username: str) -> None: ...

    def set_password_change_timestamp(self, username: str, changed_at: str) -> None: ...

    def get_password_change_timestamp(self, username: str) -> Optional[str]: ...

    def cleanup_old_data(self, days_to_keep: int = 30) -> int: ...

    def close(self) -> None: ...
