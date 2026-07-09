"""
Git Credential Manager Service.

Story #386: Git Credential Management with Identity Discovery

Manages per-user git forge credentials with AES-256-CBC encryption.
Validates tokens against the forge API before storing.
"""

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from code_indexer.server.clients.forge_client import get_forge_client
from code_indexer.server.storage.protocols import GitCredentialsBackend
from code_indexer.server.storage.sqlite_backends import GitCredentialsSqliteBackend
from .token_encryption import (
    derive_key_from_salt as _derive_key_from_salt_fn,
    derive_encryption_key as _derive_encryption_key_fn,
    encrypt_token as _encrypt_token_fn,
    decrypt_single as _decrypt_single_fn,
    decrypt_with_fallback as _decrypt_with_fallback_fn,
)

logger = logging.getLogger(__name__)


class GitCredentialManager:
    """
    Service for managing git forge credentials per user.

    Validates tokens against the forge API, encrypts them with AES-256-CBC,
    and persists via a GitCredentialsBackend (SQLite by default, PG in cluster mode).
    """

    def __init__(
        self,
        db_path: str,
        cluster_secret: Optional[str] = None,
        server_dir: Optional[str] = None,
        backend: Optional[GitCredentialsBackend] = None,
    ) -> None:
        """Initialize the credential manager.

        Args:
            db_path: Path to SQLite database file.
            cluster_secret: Shared secret for encryption key derivation in
                            cluster mode (backward compat, Bug #533).
            server_dir: Path to server directory for reading .encryption_key_salt
                        (Story #999). If provided but not an existing directory,
                        a WARNING is logged and server_dir is ignored.
            backend: Optional pre-constructed storage backend satisfying
                     GitCredentialsBackend protocol (e.g. GitCredentialsPostgresBackend).
                     When provided, used instead of creating GitCredentialsSqliteBackend(db_path).
        """
        self._backend: GitCredentialsBackend = (
            backend if backend is not None else GitCredentialsSqliteBackend(db_path)
        )
        self._cluster_secret = cluster_secret
        self._server_dir_for_salt: Optional[Path] = None
        if server_dir is not None:
            candidate = Path(server_dir)
            if candidate.is_dir():
                self._server_dir_for_salt = candidate
            else:
                logger.warning(
                    "GitCredentialManager: server_dir %s is not a directory — "
                    "ignoring for salt-based key derivation",
                    server_dir,
                )
        self._encryption_key = self._derive_encryption_key()

    def _derive_encryption_key(self) -> bytes:
        """Derive AES-256 key using token_encryption priority chain (Story #999)."""
        return _derive_encryption_key_fn(
            server_dir_for_salt=self._server_dir_for_salt,
            cluster_secret=self._cluster_secret,
        )

    @staticmethod
    def _derive_key_from_salt(salt_input: str) -> bytes:
        """Derive AES-256 key from a salt string — delegates to token_encryption."""
        return _derive_key_from_salt_fn(salt_input)

    def _encrypt_token(self, plaintext: str) -> str:
        """Encrypt plaintext using AES-256-CBC — delegates to token_encryption."""
        return _encrypt_token_fn(plaintext, self._encryption_key)

    @staticmethod
    def _do_decrypt(encrypted: str, key: bytes) -> str:
        """Decrypt with explicit key — delegates to token_encryption."""
        return _decrypt_single_fn(encrypted, key)

    def _decrypt_token(self, encrypted: str) -> Tuple[str, bool]:
        """Decrypt token with canonical key, falling back to hostname key (Story #999).

        Returns:
            Tuple of (plaintext: str, used_fallback: bool). Callers must re-encrypt
            the token when used_fallback is True (Story #999 lazy re-encryption).

        Raises:
            ValueError: If both canonical and hostname-fallback keys fail.
            binascii.Error: If the input is not valid base64.
        """
        return _decrypt_with_fallback_fn(
            encrypted,
            canonical_key=self._encryption_key,
            context_label="GitCredentialManager",
        )

    async def configure_credential(
        self,
        username: str,
        forge_type: str,
        forge_host: str,
        token: str,
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate token with forge API, encrypt, and upsert credential."""
        client = get_forge_client(forge_type)
        # Raises ForgeAuthenticationError if invalid — propagate without storing
        identity = await client.validate_and_discover(token, forge_host)

        credential_id = str(uuid.uuid4())
        encrypted_token = self._encrypt_token(token)
        now = datetime.now(timezone.utc).isoformat()

        self._backend.upsert_credential(
            credential_id=credential_id,
            username=username,
            forge_type=forge_type,
            forge_host=forge_host,
            encrypted_token=encrypted_token,
            git_user_name=identity.get("git_user_name"),
            git_user_email=identity.get("git_user_email"),
            forge_username=identity.get("forge_username"),
            name=name,
        )

        return {
            "success": True,
            "credential_id": credential_id,
            "forge_type": forge_type,
            "forge_host": forge_host,
            "git_user_name": identity.get("git_user_name"),
            "git_user_email": identity.get("git_user_email"),
            "forge_username": identity.get("forge_username"),
            "name": name,
            "created_at": now,
        }

    def list_credentials(self, username: str) -> List[Dict[str, Any]]:
        """List credentials for user with token redacted to last 4 chars of plaintext."""
        raw = self._backend.list_credentials(username)
        result = []
        for cred in raw:
            entry = {k: v for k, v in cred.items() if k != "encrypted_token"}
            try:
                plaintext, used_fallback = self._decrypt_token(cred["encrypted_token"])
                entry["token_suffix"] = (
                    plaintext[-4:] if len(plaintext) >= 4 else plaintext
                )
                if used_fallback:
                    new_enc = self._encrypt_token(plaintext)
                    self._backend.update_encrypted_token(cred["credential_id"], new_enc)
            except Exception:
                logger.warning(
                    "Failed to decrypt token for credential %s (user=%s) — possible key derivation mismatch",
                    cred.get("credential_id"),
                    username,
                    exc_info=True,
                )
                entry["token_suffix"] = "????"
            result.append(entry)
        return result

    def delete_credential(self, username: str, credential_id: str) -> bool:
        """Delete credential by id. Raises PermissionError if not owned by username."""
        deleted = self._backend.delete_credential(username, credential_id)
        if not deleted:
            raise PermissionError(
                "Credential not found or does not belong to your account."
            )
        return True

    def get_credential_for_host(
        self, username: str, forge_host: str
    ) -> Optional[Dict[str, Any]]:
        """Return credential with decrypted token, or None if not found."""
        cred = self._backend.get_credential_for_host(username, forge_host)
        if cred is None:
            return None
        result = {k: v for k, v in cred.items() if k != "encrypted_token"}
        plaintext, used_fallback = self._decrypt_token(cred["encrypted_token"])
        result["token"] = plaintext
        if used_fallback:
            new_enc = self._encrypt_token(plaintext)
            self._backend.update_encrypted_token(cred["credential_id"], new_enc)
        return result


_VALID_STORAGE_MODES = {"sqlite", "postgres"}


def create_git_credential_manager(
    db_path: str,
    server_dir: str,
    storage_mode: str = "sqlite",
) -> GitCredentialManager:
    """Factory for GitCredentialManager — ensures consistent encryption key.

    Story #999: Calls ensure_encryption_key_salt() to seed .encryption_key_salt
    from .jwt_secret (in postgres mode) or hostname (in sqlite mode), then passes
    server_dir to GitCredentialManager so it reads the salt file for key derivation.

    Raises:
        ValueError: If storage_mode is not "sqlite" or "postgres", or if
                    db_path or server_dir are empty.
    """
    if not db_path:
        raise ValueError("db_path must be a non-empty string")
    if not server_dir:
        raise ValueError("server_dir must be a non-empty string")
    if storage_mode not in _VALID_STORAGE_MODES:
        raise ValueError(
            f"Invalid storage_mode {storage_mode!r}. Must be one of: {_VALID_STORAGE_MODES}"
        )

    from code_indexer.server.services.encryption_key_salt import (
        ensure_encryption_key_salt,
    )

    ensure_encryption_key_salt(Path(server_dir), storage_mode)

    backend: Optional[GitCredentialsBackend] = None
    if storage_mode == "postgres":
        from code_indexer.server.services.config_service import get_config_service
        from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
        from code_indexer.server.storage.postgres.git_credentials_backend import (
            GitCredentialsPostgresBackend,
        )

        config = get_config_service().config_manager.load_config()
        pool = ConnectionPool(config.postgres_dsn, name="git-credentials")
        backend = GitCredentialsPostgresBackend(pool)

    return GitCredentialManager(db_path, server_dir=server_dir, backend=backend)
