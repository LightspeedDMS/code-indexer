"""GitCredentialsBackend Protocol (Story #410).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class GitCredentialsBackend(Protocol):
    """Protocol for user git credentials storage."""

    def upsert_credential(
        self,
        credential_id: str,
        username: str,
        forge_type: str,
        forge_host: str,
        encrypted_token: str,
        git_user_name: Optional[str] = None,
        git_user_email: Optional[str] = None,
        forge_username: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None: ...

    def list_credentials(self, username: str) -> List[Dict[str, Any]]: ...

    def delete_credential(self, username: str, credential_id: str) -> bool: ...

    def get_credential_for_host(
        self, username: str, forge_host: str
    ) -> Optional[Dict[str, Any]]: ...

    def update_encrypted_token(
        self, credential_id: str, new_encrypted_token: str
    ) -> None: ...

    def close(self) -> None: ...
