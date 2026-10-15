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

import anyio

from code_indexer.server.clients.forge_client import get_forge_client
from code_indexer.server.storage.protocols import GitCredentialsBackend
from code_indexer.server.storage.sqlite_backends import GitCredentialsSqliteBackend
from code_indexer.utils.credential_redaction import stored_secret_tail
from .audit_outcome import conforming_details, record_outcome, record_outcome_async
from .stored_secret_key import StoredSecretCipher

logger = logging.getLogger(__name__)


def _record_git(
    actor: str,
    action_type: str,
    credential_id: Optional[str],
    outcome: str,
    details: Dict[str, Any],
) -> None:
    record_outcome(
        actor=actor,
        action_type=action_type,
        target_type="git_credential",
        target_id=credential_id,
        outcome=outcome,
        details=details,
    )


def _verified_git_row(
    verified: Optional[Dict[str, Any]],
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Target id and details for a looked-up credential (None -> placeholder)."""
    if verified is None:
        return None, {}
    return verified.get("credential_id"), conforming_details(
        "git_credential_deleted",
        platform=verified.get("forge_type"),
        forge_host=verified.get("forge_host"),
    )


async def _record_git_async(
    actor: str,
    action_type: str,
    credential_id: Optional[str],
    outcome: str,
    details: Dict[str, Any],
) -> None:
    await record_outcome_async(
        actor=actor,
        action_type=action_type,
        target_type="git_credential",
        target_id=credential_id,
        outcome=outcome,
        details=details,
    )


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
        key_dir = Path(db_path).parent
        if server_dir is not None:
            candidate = Path(server_dir)
            if candidate.is_dir():
                key_dir = candidate
            else:
                logger.warning(
                    "GitCredentialManager: server_dir %s is not a directory — "
                    "using the database directory for the stored-secret key",
                    server_dir,
                )
        self._cipher = StoredSecretCipher(
            key_dir, "GitCredentialManager", cluster_secret=cluster_secret
        )

    def _encrypt_token(self, plaintext: str) -> str:
        """Encrypt a token for storage (v2 when the cluster gate allows)."""
        return self._cipher.encrypt(plaintext)

    def _read_token(self, cred: Dict[str, Any]) -> str:
        """Decrypt a stored token, rewriting a legacy value as v2 (CAS).

        An undecryptable value is marked as needing re-entry (never deleted)
        and its ValueError propagates."""
        encrypted = cred["encrypted_token"]
        credential_id = cred["credential_id"]
        try:
            plaintext = self._cipher.read(
                encrypted,
                lambda old, new: self._backend.replace_encrypted_token_if_unchanged(
                    credential_id, old, new
                ),
            )
            # Readable again (e.g. the right key restored): drop a stale mark.
            # Only a marked row is written; an unmarked read stays read-only.
            if cred.get("needs_reentry_at"):
                self._backend.clear_needs_reentry(credential_id, encrypted)
            return plaintext
        except ValueError:
            try:
                self._backend.mark_needs_reentry(
                    credential_id, encrypted, node_id=self._cipher.node_id
                )
            except Exception:
                logger.warning(
                    "Could not mark git credential %s as needing re-entry",
                    credential_id,
                    exc_info=True,
                )
            raise

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
        now = datetime.now(timezone.utc).isoformat()

        def _store_credential() -> None:
            # Encryption may query the cluster gate: keep it with the write.
            self._backend.upsert_credential(
                credential_id=credential_id,
                username=username,
                forge_type=forge_type,
                forge_host=forge_host,
                encrypted_token=self._encrypt_token(token),
                git_user_name=identity.get("git_user_name"),
                git_user_email=identity.get("git_user_email"),
                forge_username=identity.get("forge_username"),
                name=name,
            )

        await anyio.to_thread.run_sync(_store_credential)

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

    async def configure_credential_audited(
        self,
        username: str,
        forge_type: str,
        forge_host: str,
        token: str,
        name: Optional[str] = None,
        *,
        actor: str,
    ) -> Dict[str, Any]:
        """:meth:`configure_credential`, recording ``git_credential_configured``.

        Every front door calls this.  The row is written off the event loop;
        the token and the free-text name are never recorded.  A failed
        configuration persisted nothing, so its row names no credential and
        carries no details.
        """
        try:
            result = await self.configure_credential(
                username, forge_type, forge_host, token, name=name
            )
        except Exception:
            await _record_git_async(
                actor, "git_credential_configured", None, "failure", {}
            )
            raise
        details = conforming_details(
            "git_credential_configured", platform=forge_type, forge_host=forge_host
        )
        await _record_git_async(
            actor,
            "git_credential_configured",
            result.get("credential_id"),
            "success",
            details,
        )
        return result

    def delete_credential_audited(
        self, username: str, credential_id: str, *, actor: str
    ) -> bool:
        """:meth:`delete_credential`, recording ``git_credential_deleted``.

        The credential is named only when the lookup inside the guarded
        block found it among *username*'s persisted credentials.
        """
        verified: Optional[Dict[str, Any]] = None
        try:
            verified = next(
                (
                    cred
                    for cred in self._backend.list_credentials(username)
                    if cred.get("credential_id") == credential_id
                ),
                None,
            )
            deleted = self.delete_credential(username, credential_id)
        except Exception:
            verified_id, details = _verified_git_row(verified)
            _record_git(
                actor, "git_credential_deleted", verified_id, "failure", details
            )
            raise
        verified_id, details = _verified_git_row(verified)
        _record_git(actor, "git_credential_deleted", verified_id, "success", details)
        return deleted

    def list_credentials(self, username: str) -> List[Dict[str, Any]]:
        """List credentials for user; the token is reduced to ``token_suffix``
        (its last 4 characters, or "" for a token under 20 characters)."""
        raw = self._backend.list_credentials(username)
        result = []
        for cred in raw:
            entry = {
                k: v
                for k, v in cred.items()
                if k not in ("encrypted_token", "needs_reentry_at")
            }
            try:
                entry["token_suffix"] = stored_secret_tail(self._read_token(cred))
                entry["needs_reentry"] = False
            except Exception as exc:
                # A ValueError is a value no key on this node decrypts.
                entry["token_suffix"] = "????"
                entry["needs_reentry"] = isinstance(exc, ValueError)
                if entry["needs_reentry"]:
                    entry["needs_reentry_node"] = self._cipher.node_id
                logger.warning(
                    "Failed to read token for credential %s (user=%s, %s)",
                    cred.get("credential_id"),
                    username,
                    type(exc).__name__,
                )
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
        result = {
            k: v
            for k, v in cred.items()
            if k not in ("encrypted_token", "needs_reentry_at")
        }
        result["token"] = self._read_token(cred)
        return result


_VALID_STORAGE_MODES = {"sqlite", "postgres"}


def create_git_credential_manager(
    db_path: str,
    server_dir: str,
    storage_mode: str = "sqlite",
) -> GitCredentialManager:
    """Factory for GitCredentialManager — ensures consistent encryption key.

    The stored-secret key lives in server_dir (solo) or cluster_secrets
    (cluster). In postgres mode the legacy salt file is still seeded: legacy
    writes use it while a cluster node cannot yet read v2 values.

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

    backend: Optional[GitCredentialsBackend] = None
    if storage_mode == "postgres":
        from code_indexer.server.services.encryption_key_salt import (
            ensure_encryption_key_salt,
        )

        ensure_encryption_key_salt(Path(server_dir), storage_mode)
        from code_indexer.server.services.config_service import get_config_service
        from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
        from code_indexer.server.storage.postgres.git_credentials_backend import (
            GitCredentialsPostgresBackend,
        )

        config = get_config_service().config_manager.load_config()
        pool = ConnectionPool(config.postgres_dsn, name="git-credentials")
        backend = GitCredentialsPostgresBackend(pool)

    return GitCredentialManager(db_path, server_dir=server_dir, backend=backend)
