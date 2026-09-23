"""SSHKeysBackend Protocol (Story #410).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class SSHKeysBackend(Protocol):
    """Protocol for SSH key management storage."""

    def create_key(
        self,
        name: str,
        fingerprint: str,
        key_type: str,
        private_path: str,
        public_path: str,
        public_key: Optional[str] = None,
        email: Optional[str] = None,
        description: Optional[str] = None,
        is_imported: bool = False,
    ) -> None: ...

    def get_key(self, name: str) -> Optional[Dict[str, Any]]: ...

    def assign_host(self, key_name: str, hostname: str) -> None: ...

    def remove_host(self, key_name: str, hostname: str) -> None: ...

    def delete_key(self, name: str) -> bool: ...

    def list_keys(self) -> list: ...

    def close(self) -> None: ...
