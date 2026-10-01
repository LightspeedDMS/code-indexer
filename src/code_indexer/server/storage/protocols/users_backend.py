"""UsersBackend Protocol (Story #410).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable


@runtime_checkable
class UsersBackend(Protocol):
    """Protocol for user management storage."""

    def create_user(
        self,
        username: str,
        password_hash: str,
        role: str,
        email: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None: ...

    def get_user(self, username: str) -> Optional[Dict[str, Any]]: ...

    def list_users(self) -> list: ...

    def update_user(
        self,
        username: str,
        new_username: Optional[str] = None,
        email: Optional[str] = None,
    ) -> bool: ...

    def delete_user(self, username: str) -> bool: ...

    def update_user_role(self, username: str, role: str) -> bool: ...

    def update_password_hash(self, username: str, password_hash: str) -> bool: ...

    def add_api_key(
        self,
        username: str,
        key_id: str,
        key_hash: str,
        key_prefix: str,
        name: Optional[str] = None,
    ) -> None: ...

    def delete_api_key(self, username: str, key_id: str) -> bool: ...

    def add_mcp_credential(
        self,
        username: str,
        credential_id: str,
        client_id: str,
        client_secret_hash: str,
        client_id_prefix: str,
        name: Optional[str] = None,
    ) -> None: ...

    def delete_mcp_credential(self, username: str, credential_id: str) -> bool: ...

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]: ...

    def set_oidc_identity(self, username: str, identity: Dict[str, Any]) -> bool: ...

    def remove_oidc_identity(self, username: str) -> bool: ...

    def update_mcp_credential_last_used(
        self, username: str, credential_id: str
    ) -> bool: ...

    def list_all_mcp_credentials(
        self, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]: ...

    def get_system_mcp_credentials(self) -> List[Dict[str, Any]]: ...

    def get_mcp_credential_by_client_id(
        self, client_id: str
    ) -> Optional[Tuple[str, dict]]: ...

    def close(self) -> None: ...
