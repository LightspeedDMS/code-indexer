"""RefreshTokenBackend Protocol (Story #515).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class RefreshTokenBackend(Protocol):
    """Protocol for refresh token storage (Story #515).

    Provides data-level access to token_families and refresh_tokens tables.
    Satisfies PEP 544 structural subtyping: any class implementing all of
    these methods is accepted as a RefreshTokenBackend without inheritance.
    """

    # token_families table

    def create_token_family(
        self, family_id: str, username: str, created_at: str, last_used_at: str
    ) -> None: ...

    def get_token_family(self, family_id: str) -> Optional[Dict[str, Any]]: ...

    def revoke_token_family(self, family_id: str, reason: str) -> None: ...

    def revoke_user_families(self, username: str, reason: str) -> int: ...

    def update_family_last_used(self, family_id: str, last_used_at: str) -> None: ...

    # refresh_tokens table

    def store_refresh_token(
        self,
        token_id: str,
        family_id: str,
        username: str,
        token_hash: str,
        created_at: str,
        expires_at: str,
        parent_token_id: Optional[str] = None,
    ) -> None: ...

    def get_refresh_token_by_hash(
        self, token_hash: str
    ) -> Optional[Dict[str, Any]]: ...

    def mark_token_used(self, token_id: str, used_at: str) -> None: ...

    def count_active_tokens_in_family(self, family_id: str) -> int: ...

    def delete_expired_tokens(self, now_iso: str) -> int: ...

    def delete_orphaned_families(self) -> int: ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...
