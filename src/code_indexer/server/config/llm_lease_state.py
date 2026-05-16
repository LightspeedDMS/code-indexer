"""
LLM Lease State Persistence (Story #365).

Persists authoritative lease state (lease_id, credential_id) to disk,
encrypted with AES-256-CBC using the same pattern as delegation_config.py.
Default path: ~/.cidx-server/llm_lease_state.json
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

# Encryption constants — same as delegation_config.py for consistency
PBKDF2_ITERATIONS = 100000
AES_KEY_SIZE = 32  # 256 bits
AES_BLOCK_SIZE = 16  # 128 bits

_STATE_FILENAME = "llm_lease_state.json"
# Deterministic salt for cluster mode encryption key derivation
_CLUSTER_KEY_SALT = hashlib.sha256(b"cidx-llm-lease-cluster-salt").digest()


# ---------------------------------------------------------------------------
# State dataclass
# ---------------------------------------------------------------------------


@dataclass
class LlmLeaseState:
    """Authoritative lease state that must survive server restarts."""

    lease_id: str
    credential_id: str
    credential_type: str = "oauth"  # "oauth" | "api_key"


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class LlmLeaseStateManager:
    """
    Manages encrypted persistence of LLM lease state.

    The state is serialised to JSON, then the JSON string is encrypted with
    AES-256-CBC before being written to disk.  The file is stored with
    ``0o600`` permissions to prevent unauthorised reads.
    """

    def __init__(
        self,
        server_dir_path: Optional[str] = None,
        state_filename: str = _STATE_FILENAME,
    ) -> None:
        """
        Initialise the manager.

        Args:
            server_dir_path: Directory where the state file is stored.
                             Defaults to ``~/.cidx-server``.
            state_filename: Name of the state file within server_dir_path.
                            Must be a plain filename — non-empty, not ``"."``
                            or ``".."``, no path separators, and
                            ``Path(state_filename).parts`` must have exactly
                            one element.  Defaults to
                            ``llm_lease_state.json``.  Callers that need
                            vendor-scoped isolation (e.g. Codex) pass a
                            distinct filename here instead of mutating
                            ``_state_file`` after construction.

        Raises:
            ValueError: If ``state_filename`` fails any of the plain-filename
                        safety checks.
        """
        import os as _os

        if not state_filename:
            raise ValueError("state_filename must be a non-empty string")
        if state_filename in (".", ".."):
            raise ValueError(
                f"state_filename must not be a directory-traversal token; "
                f"got: {state_filename!r}"
            )
        _parts = Path(state_filename).parts
        if len(_parts) != 1:
            raise ValueError(
                f"state_filename must be a single-component filename "
                f"(Path.parts must have exactly one element); "
                f"got: {state_filename!r} with parts {_parts}"
            )
        if _os.path.basename(state_filename) != state_filename:
            raise ValueError(
                f"state_filename must be a plain filename with no path "
                f"separators; got: {state_filename!r}"
            )

        if server_dir_path:
            self._server_dir = Path(server_dir_path)
        else:
            self._server_dir = Path.home() / ".cidx-server"

        self._state_file = self._server_dir / state_filename
        self._encryption_key = self._derive_encryption_key()
        self._pool: Optional[Any] = None
        self._cluster_encryption_key: Optional[bytes] = None

    # ------------------------------------------------------------------
    # Cluster pool wiring
    # ------------------------------------------------------------------

    def set_connection_pool(self, pool: Any) -> None:
        """Set PostgreSQL connection pool for cluster mode.

        When set, save_state/load_state/clear_state use the cluster_secrets
        PG table instead of a local encrypted file, enabling cross-node
        lease state sharing. The encryption key is derived from the shared
        JWT secret in cluster_secrets so all nodes decrypt identically.

        Raises:
            RuntimeError: If the jwt_secret row is absent from cluster_secrets.
        """
        self._pool = pool
        self._cluster_encryption_key = self._derive_cluster_encryption_key(pool)
        logger.info(
            "LlmLeaseStateManager: using PostgreSQL connection pool (cluster mode)"
        )

    def _derive_cluster_encryption_key(self, pool: Any) -> bytes:
        """Derive a deterministic AES-256 key from the shared JWT secret in cluster_secrets.

        Reads the 'jwt_secret' row and applies PBKDF2 with _CLUSTER_KEY_SALT so
        all cluster nodes sharing the same pool derive the same key.

        Raises:
            RuntimeError: If 'jwt_secret' row does not exist in cluster_secrets.
        """
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT key_value FROM cluster_secrets WHERE key_name = %s",
                ("jwt_secret",),
            ).fetchone()
        if row is None:
            raise RuntimeError(
                "cluster_secrets table has no 'jwt_secret' row; "
                "cannot derive cluster encryption key for LlmLeaseStateManager"
            )
        jwt_secret_bytes = row[0].encode("utf-8")
        return hashlib.pbkdf2_hmac(
            "sha256",
            jwt_secret_bytes,
            _CLUSTER_KEY_SALT,
            PBKDF2_ITERATIONS,
            dklen=AES_KEY_SIZE,
        )

    # ------------------------------------------------------------------
    # Encryption helpers (same algorithm as delegation_config.py)
    # ------------------------------------------------------------------

    def _derive_encryption_key(self) -> bytes:
        """Derive encryption key using PBKDF2 with machine-specific salt."""
        machine_id = os.uname().nodename.encode("utf-8")
        salt = hashlib.sha256(machine_id).digest()
        return hashlib.pbkdf2_hmac(
            "sha256",
            b"cidx-llm-lease-encryption-key",
            salt,
            PBKDF2_ITERATIONS,
            dklen=AES_KEY_SIZE,
        )

    @property
    def _active_encryption_key(self) -> bytes:
        """Return the cluster key when PG pool is set, else the node-local key."""
        if self._cluster_encryption_key is not None:
            return self._cluster_encryption_key
        return self._encryption_key

    def _encrypt(self, plaintext: str) -> str:
        """Encrypt a string with AES-256-CBC. Returns base64-encoded ciphertext."""
        key = self._active_encryption_key
        iv = os.urandom(AES_BLOCK_SIZE)
        padder = padding.PKCS7(AES_BLOCK_SIZE * 8).padder()
        padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()

        cipher = Cipher(
            algorithms.AES(key),
            modes.CBC(iv),
            backend=default_backend(),
        )
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()

        return base64.b64encode(iv + ciphertext).decode("utf-8")

    def _decrypt(self, encrypted: str) -> str:
        """Decrypt a base64-encoded AES-256-CBC ciphertext. Returns plaintext."""
        key = self._active_encryption_key
        combined = base64.b64decode(encrypted.encode("utf-8"))
        if len(combined) < AES_BLOCK_SIZE + 1:
            raise ValueError("Encrypted data too short to be valid")

        iv = combined[:AES_BLOCK_SIZE]
        ciphertext = combined[AES_BLOCK_SIZE:]

        cipher = Cipher(
            algorithms.AES(key),
            modes.CBC(iv),
            backend=default_backend(),
        )
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()

        unpadder = padding.PKCS7(AES_BLOCK_SIZE * 8).unpadder()
        plaintext_bytes = unpadder.update(padded) + unpadder.finalize()
        return str(plaintext_bytes.decode("utf-8"))

    # ------------------------------------------------------------------
    # Shared serialization helpers
    # ------------------------------------------------------------------

    _PG_KEY_NAME = "llm_lease_state"

    def _serialize_state(self, state: LlmLeaseState) -> str:
        """Serialize and encrypt a LlmLeaseState to a base64 blob."""
        plaintext = json.dumps(
            {
                "lease_id": state.lease_id,
                "credential_id": state.credential_id,
                "credential_type": state.credential_type,
            }
        )
        return self._encrypt(plaintext)

    def _deserialize_blob(self, encrypted_blob: str) -> LlmLeaseState:
        """Decrypt and deserialize a base64 blob to a LlmLeaseState."""
        plaintext = self._decrypt(encrypted_blob)
        data = json.loads(plaintext)
        return LlmLeaseState(
            lease_id=data["lease_id"],
            credential_id=data["credential_id"],
            credential_type=data.get("credential_type", "oauth"),
        )

    # ------------------------------------------------------------------
    # PostgreSQL cluster methods
    # ------------------------------------------------------------------

    def _pg_save_state(self, state: LlmLeaseState) -> None:
        encrypted_blob = self._serialize_state(state)
        assert self._pool is not None, (
            "_pg_save_state called before set_connection_pool()"
        )
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO cluster_secrets (key_name, key_value) "
                "VALUES (%s, %s) "
                "ON CONFLICT (key_name) DO UPDATE SET key_value = EXCLUDED.key_value, "
                "updated_at = CURRENT_TIMESTAMP",
                (self._PG_KEY_NAME, encrypted_blob),
            )
            conn.commit()
        logger.debug("Saved LLM lease state to cluster_secrets (PG)")

    def _pg_load_state(self) -> Optional[LlmLeaseState]:
        assert self._pool is not None, (
            "_pg_load_state called before set_connection_pool()"
        )
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT key_value FROM cluster_secrets WHERE key_name = %s",
                (self._PG_KEY_NAME,),
            ).fetchone()
        if row is None:
            return None
        return self._deserialize_blob(row[0])

    def _pg_clear_state(self) -> None:
        assert self._pool is not None, (
            "_pg_clear_state called before set_connection_pool()"
        )
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM cluster_secrets WHERE key_name = %s",
                (self._PG_KEY_NAME,),
            )
            conn.commit()
        logger.debug("Cleared LLM lease state from cluster_secrets (PG)")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_state(self, state: LlmLeaseState) -> None:
        """Encrypt and persist the lease state.

        Routes to PG in cluster mode, file in standalone mode.
        """
        if self._pool is not None:
            self._pg_save_state(state)
            return

        self._server_dir.mkdir(parents=True, exist_ok=True)
        encrypted_blob = self._serialize_state(state)
        envelope = json.dumps({"encrypted_state": encrypted_blob})

        with open(self._state_file, "w") as fh:
            fh.write(envelope)

        os.chmod(self._state_file, 0o600)
        logger.debug("Saved LLM lease state to %s", self._state_file)

    def load_state(self) -> Optional[LlmLeaseState]:
        """Load and decrypt the lease state.

        Routes to PG in cluster mode, file in standalone mode.
        """
        if self._pool is not None:
            return self._pg_load_state()

        if not self._state_file.exists():
            return None

        with open(self._state_file, "r") as fh:
            envelope = json.load(fh)

        return self._deserialize_blob(envelope["encrypted_state"])

    def clear_state(self) -> None:
        """Delete the persisted lease state.

        Routes to PG in cluster mode, file in standalone mode.
        """
        if self._pool is not None:
            self._pg_clear_state()
            return

        if self._state_file.exists():
            self._state_file.unlink()
            logger.debug("Cleared LLM lease state from %s", self._state_file)
