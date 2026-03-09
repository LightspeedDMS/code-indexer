"""
Git Credential Manager Service.

Story #386: Git Credential Management with Identity Discovery

Manages per-user git forge credentials with AES-256-CBC encryption.
Validates tokens against the forge API before storing.
"""

import base64
import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from code_indexer.server.clients.forge_client import ForgeAuthenticationError, get_forge_client
from code_indexer.server.storage.sqlite_backends import GitCredentialsSqliteBackend

logger = logging.getLogger(__name__)

# Encryption constants
PBKDF2_ITERATIONS = 100000
AES_KEY_SIZE = 32  # 256 bits
AES_BLOCK_SIZE = 16  # 128 bits


class GitCredentialManager:
    """
    Service for managing git forge credentials per user.

    Validates tokens against the forge API, encrypts them with AES-256-CBC,
    and persists via GitCredentialsSqliteBackend.
    """

    def __init__(self, db_path: str) -> None:
        self._backend = GitCredentialsSqliteBackend(db_path)
        self._encryption_key = self._derive_encryption_key()

    def _derive_encryption_key(self) -> bytes:
        machine_id = os.uname().nodename.encode("utf-8")
        salt = hashlib.sha256(machine_id).digest()
        return hashlib.pbkdf2_hmac(
            "sha256",
            b"cidx-token-encryption-key",
            salt,
            PBKDF2_ITERATIONS,
            dklen=AES_KEY_SIZE,
        )

    def _encrypt_token(self, plaintext: str) -> str:
        iv = os.urandom(AES_BLOCK_SIZE)
        padder = padding.PKCS7(128).padder()
        padded_data = padder.update(plaintext.encode("utf-8")) + padder.finalize()
        cipher = Cipher(algorithms.AES(self._encryption_key), modes.CBC(iv), backend=default_backend())
        encryptor = cipher.encryptor()
        encrypted_data = encryptor.update(padded_data) + encryptor.finalize()
        return base64.b64encode(iv + encrypted_data).decode("utf-8")

    def _decrypt_token(self, encrypted: str) -> str:
        combined = base64.b64decode(encrypted.encode("utf-8"))
        iv = combined[:AES_BLOCK_SIZE]
        encrypted_data = combined[AES_BLOCK_SIZE:]
        cipher = Cipher(algorithms.AES(self._encryption_key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        padded_data = decryptor.update(encrypted_data) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        data = unpadder.update(padded_data) + unpadder.finalize()
        return data.decode("utf-8")

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
                plaintext = self._decrypt_token(cred["encrypted_token"])
                entry["token_suffix"] = plaintext[-4:] if len(plaintext) >= 4 else plaintext
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
            raise PermissionError("Credential not found or does not belong to your account.")
        return True

    def get_credential_for_host(self, username: str, forge_host: str) -> Optional[Dict[str, Any]]:
        """Return credential with decrypted token, or None if not found."""
        cred = self._backend.get_credential_for_host(username, forge_host)
        if cred is None:
            return None
        result = {k: v for k, v in cred.items() if k != "encrypted_token"}
        result["token"] = self._decrypt_token(cred["encrypted_token"])
        return result
