"""
Shared AES-256-CBC token encryption utilities (Story #999 / Finding 5).

Provides the common encryption/decryption primitives used by both
CITokenManager and GitCredentialManager, eliminating duplication.

Key derivation priority (derive_encryption_key):
1. Salt file (.encryption_key_salt) in server_dir — takes absolute priority.
2. cluster_secret — used when no salt file is present.
3. Hostname — standalone fallback when neither is available.
"""

import base64
import hashlib
import logging
import os
from pathlib import Path
from typing import Optional, Tuple

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

# Encryption constants — must match the values used in both managers (Bug #533)
PBKDF2_ITERATIONS = 100000
AES_KEY_SIZE = 32  # 256 bits
AES_BLOCK_SIZE = 16  # 128 bits

_SALT_FILENAME = ".encryption_key_salt"


def derive_key_from_salt(salt_input: str) -> bytes:
    """Derive a 32-byte AES-256 key from a salt string using PBKDF2-HMAC-SHA256.

    Args:
        salt_input: String used as the salt source (hostname, cluster secret, or file content).

    Returns:
        32-byte key suitable for AES-256 operations.
    """
    salt = hashlib.sha256(salt_input.encode("utf-8")).digest()
    return hashlib.pbkdf2_hmac(
        "sha256",
        b"cidx-token-encryption-key",
        salt,
        PBKDF2_ITERATIONS,
        dklen=AES_KEY_SIZE,
    )


def derive_encryption_key(
    server_dir_for_salt: Optional[Path],
    cluster_secret: Optional[str],
) -> bytes:
    """Derive the AES-256 encryption key using the three-priority chain.

    Priority order:
    1. If server_dir_for_salt is set and .encryption_key_salt exists: use file content.
    2. If cluster_secret is set: use cluster_secret.
    3. Fallback: use system hostname (standalone backward-compat).

    Args:
        server_dir_for_salt: Path to server directory to look for .encryption_key_salt.
                             May be None to skip file-based derivation.
        cluster_secret: Shared cluster secret string. May be None.

    Returns:
        32-byte AES-256 key.
    """
    if server_dir_for_salt is not None:
        salt_file = server_dir_for_salt / _SALT_FILENAME
        if salt_file.exists():
            return derive_key_from_salt(salt_file.read_text())

    if cluster_secret is not None:
        return derive_key_from_salt(cluster_secret)

    return derive_key_from_salt(os.uname().nodename)


def encrypt_token(plaintext: str, key: bytes) -> str:
    """Encrypt a plaintext string using AES-256-CBC with a random IV.

    Args:
        plaintext: The string to encrypt.
        key: 32-byte AES-256 key.

    Returns:
        Base64-encoded string containing IV + ciphertext.
    """
    iv = os.urandom(AES_BLOCK_SIZE)
    padder = padding.PKCS7(128).padder()
    padded_data = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    cipher = Cipher(
        algorithms.AES(key),
        modes.CBC(iv),
        backend=default_backend(),
    )
    encryptor = cipher.encryptor()
    encrypted_data = encryptor.update(padded_data) + encryptor.finalize()
    return base64.b64encode(iv + encrypted_data).decode("utf-8")


def decrypt_single(encrypted: str, key: bytes) -> str:
    """Decrypt a single AES-256-CBC encrypted string.

    Args:
        encrypted: Base64-encoded IV + ciphertext (produced by encrypt_token).
        key: 32-byte AES-256 key.

    Returns:
        Decrypted plaintext string.

    Raises:
        ValueError: On PKCS7 unpadding failure (wrong key or corrupted data).
        binascii.Error: On invalid base64 input.
    """
    combined = base64.b64decode(encrypted.encode("utf-8"))
    iv = combined[:AES_BLOCK_SIZE]
    encrypted_data = combined[AES_BLOCK_SIZE:]
    cipher = Cipher(
        algorithms.AES(key),
        modes.CBC(iv),
        backend=default_backend(),
    )
    decryptor = cipher.decryptor()
    padded_data = decryptor.update(encrypted_data) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    data = unpadder.update(padded_data) + unpadder.finalize()
    return data.decode("utf-8")


def decrypt_with_fallback(
    encrypted: str,
    canonical_key: bytes,
    context_label: str,
) -> Tuple[str, bool]:
    """Decrypt using canonical key; fall back to hostname-derived key on failure.

    Attempts decryption with canonical_key first (exactly 2 attempts total).
    On failure, derives the hostname key and retries. Logs a WARNING on fallback
    success so operators know re-encryption is needed.

    Args:
        encrypted: Base64-encoded ciphertext from encrypt_token().
        canonical_key: Primary key derived from salt file or cluster secret.
        context_label: Short string for log messages (e.g. "CITokenManager").

    Returns:
        Tuple of (plaintext: str, used_fallback: bool). Callers must re-encrypt
        when used_fallback is True (Story #999 lazy re-encryption).

    Raises:
        ValueError: If both canonical and hostname-fallback keys fail due to
                    PKCS7 unpadding or data errors.
        binascii.Error: If the input is not valid base64.
    """
    import binascii

    try:
        return decrypt_single(encrypted, canonical_key), False
    except (ValueError, binascii.Error):
        hostname_key = derive_key_from_salt(os.uname().nodename)
        plaintext = decrypt_single(encrypted, hostname_key)
        logger.warning(
            "%s: token decrypted via hostname fallback key — "
            "re-encryption recommended (Story #999 migration)",
            context_label,
        )
        return plaintext, True
