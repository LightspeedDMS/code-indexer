"""CITokensBackend Protocol (Story #410).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class CITokensBackend(Protocol):
    """Protocol for CI token storage."""

    def save_token(
        self, platform: str, encrypted_token: str, base_url: Optional[str] = None
    ) -> None: ...

    def get_token(self, platform: str) -> Optional[Dict[str, Any]]: ...

    def delete_token(self, platform: str) -> bool: ...

    def list_tokens(self) -> Dict[str, Dict[str, Any]]: ...

    def update_encrypted_token(
        self, platform: str, new_encrypted_token: str
    ) -> None: ...

    def close(self) -> None: ...
