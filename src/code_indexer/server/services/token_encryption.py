"""
Stored-secret encryption primitives (Story #999 / Finding 5; v2 format).

Two formats:

- ``v2:`` + Fernet token (AES-128-CBC + HMAC-SHA256, authenticated) under the
  random stored-secret key (``stored_secret_key``). A wrong key or a changed
  ciphertext raises ``StoredSecretKeyMismatch``.
- Legacy (no prefix): values written by earlier versions. They remain
  readable (``legacy_candidate_keys``) and are rewritten in the v2 format;
  the legacy format is still written only while a cluster has nodes that
  cannot read v2.
"""

import base64
import binascii
import functools
import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Set, Tuple

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

# Encryption constants — must match the values used in both managers (Bug #533)
PBKDF2_ITERATIONS = 100000
AES_KEY_SIZE = 32  # 256 bits
AES_BLOCK_SIZE = 16  # 128 bits

V2_PREFIX = "v2:"

_SALT_FILENAME = ".encryption_key_salt"
_JWT_SECRET_FILENAME = ".jwt_secret"


class StoredSecretKeyMismatch(ValueError):
    """A v2 value does not authenticate under this key (wrong key or a
    changed ciphertext)."""


class StoredSecretUnreadable(ValueError):
    """No legacy key derivation available on this node decrypts the value."""


class StoredSecretAmbiguous(StoredSecretUnreadable):
    """More than one legacy key derivation yields valid text: the value is
    kept unchanged and treated as unreadable."""


class StoredSecretUndetermined(StoredSecretUnreadable):
    """No available key decrypts the value, but a legacy key source could not
    be read: not a verdict, so the value is not marked as needing re-entry."""


class StoredSecretLegacyWriteRefused(RuntimeError):
    """A legacy-format write (cluster gate closed) has no readable
    ``.encryption_key_salt`` to derive its key from."""


@functools.lru_cache(maxsize=32)
def derive_key_from_salt(salt_input: str) -> bytes:
    """The legacy-format key for one legacy key source (compatibility only)."""
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
    """The legacy-format key earlier versions on this node write with
    (compatibility only: legacy writes while a cluster gate is closed)."""
    if server_dir_for_salt is not None:
        salt_file = server_dir_for_salt / _SALT_FILENAME
        if salt_file.exists():
            return derive_key_from_salt(salt_file.read_text())

    if cluster_secret is not None:
        return derive_key_from_salt(cluster_secret)

    return derive_key_from_salt(os.uname().nodename)


def legacy_write_key(server_dir: Path) -> bytes:
    """The key for a legacy-format write: only a readable existing
    ``.encryption_key_salt`` (the derivation older nodes read with)."""
    salt, _ = read_legacy_source(Path(server_dir) / _SALT_FILENAME, strip=False)
    if salt is None:
        raise StoredSecretLegacyWriteRefused(
            "Stored-secret upgrade in progress across the cluster and this node "
            "has no readable legacy key file; retry after all nodes run 12.90 "
            "or later"
        )
    return derive_key_from_salt(salt)


def legacy_candidate_keys(
    server_dir: Optional[Path], cluster_jwt_secret: Optional[str]
) -> Tuple[List[bytes], bool]:
    """(keys, complete): every legacy-format key earlier versions may have
    written a stored value with, duplicates tried once. Missing sources are
    skipped; an unreadable source is skipped and makes ``complete`` False."""
    salt: Tuple[Optional[str], bool] = (None, False)
    jwt: Tuple[Optional[str], bool] = (None, False)
    if server_dir is not None:
        salt = read_legacy_source(Path(server_dir) / _SALT_FILENAME, strip=False)
        jwt = read_legacy_source(Path(server_dir) / _JWT_SECRET_FILENAME, strip=True)
    complete = not salt[1] and not jwt[1]
    salts = [salt[0], cluster_jwt_secret, jwt[0], os.uname().nodename]
    unique = [s for i, s in enumerate(salts) if s and s not in salts[:i]]
    return [derive_key_from_salt(s) for s in unique], complete


_WARNED_SOURCES: Set[str] = set()
_WARNED_LOCK = threading.Lock()


def warn_unreadable_legacy_source(source: str, exc: BaseException) -> None:
    """One WARNING per legacy key source per process (never its content)."""
    with _WARNED_LOCK:
        if source in _WARNED_SOURCES:
            return
        _WARNED_SOURCES.add(source)
    logger.warning(
        "Legacy key source %s is unreadable (%s); it is skipped",
        source,
        type(exc).__name__,
    )


class NotARegularFile(OSError):
    """A legacy key path exists but is not a regular file."""


def read_legacy_source(path: Path, strip: bool) -> Tuple[Optional[str], bool]:
    """(text, unreadable): text is None when nothing exists at the path
    (unreadable False) or when it exists but is not a readable regular text
    file (unreadable True, one WARNING)."""
    try:
        if not os.path.lexists(path):
            return None, False
        if not path.is_file():
            raise NotARegularFile(f"{path} is not a regular file")
        text = path.read_text()
    except (OSError, ValueError) as exc:
        warn_unreadable_legacy_source(str(path), exc)
        return None, True
    return (text.strip() if strip else text), False


def encrypt_token(plaintext: str, key: bytes) -> str:
    """Encrypt a plaintext string in the legacy format (AES-256-CBC, random IV).

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
    """Decrypt a single legacy AES-256-CBC encrypted string.

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
    plaintext: str = data.decode("utf-8")
    return plaintext


def is_v2(value: str) -> bool:
    return value.startswith(V2_PREFIX)


def encrypt_v2(plaintext: str, key: bytes) -> str:
    """``v2:`` + Fernet token of *plaintext* under the stored-secret *key*."""
    token = Fernet(key).encrypt(plaintext.encode("utf-8"))
    return V2_PREFIX + str(token.decode("ascii"))


def decrypt_v2(value: str, key: bytes) -> str:
    """Decrypt a ``v2:`` value.

    Raises:
        StoredSecretKeyMismatch: wrong key or changed ciphertext.
        ValueError: *value* is not a v2 value.
    """
    if not is_v2(value):
        raise ValueError("not a v2 stored-secret value")
    try:
        data = Fernet(key).decrypt(value[len(V2_PREFIX) :].encode("ascii"))
    except (InvalidToken, UnicodeEncodeError):
        raise StoredSecretKeyMismatch(
            "stored secret does not authenticate under the stored-secret key"
        ) from None
    return str(data.decode("utf-8"))


def _try_legacy(encrypted: str, key: bytes) -> Optional[str]:
    """The plaintext, or None when *key* does not decrypt *encrypted* to
    printable text (every stored secret is printable text)."""
    try:
        plaintext = decrypt_single(encrypted, key)
    except (ValueError, binascii.Error):
        return None
    if not plaintext or not plaintext.isprintable():
        return None
    return plaintext


def decrypt_with_fallback(
    encrypted: str,
    v2_key: bytes,
    legacy_keys: Callable[[], Sequence[bytes]],
    context_label: str,
) -> Tuple[str, bool]:
    """Decrypt a stored value in either format.

    A ``v2:`` value is decrypted with *v2_key* only. A legacy value is tried
    with every key from *legacy_keys* (called only for legacy values).

    Returns:
        (plaintext, is_legacy). ``is_legacy`` True means the value should be
        rewritten as v2 (when the cluster gate allows it).

    Raises:
        StoredSecretKeyMismatch: a v2 value under another key.
        StoredSecretUnreadable: no legacy key decrypts the value.
    """
    if is_v2(encrypted):
        return decrypt_v2(encrypted, v2_key), False
    matches = [
        plaintext
        for plaintext in (_try_legacy(encrypted, key) for key in legacy_keys())
        if plaintext is not None
    ]
    if len(matches) == 1:
        logger.info("%s: read a legacy-format stored secret", context_label)
        return matches[0], True
    if matches:
        logger.warning(
            "%s: a legacy-format stored secret decrypts under %d keys; it is "
            "kept unchanged and needs re-entry",
            context_label,
            len(matches),
        )
        raise StoredSecretAmbiguous(
            f"{context_label}: the stored secret matches more than one legacy key"
        )
    raise StoredSecretUnreadable(
        f"{context_label}: no legacy key on this node decrypts the stored secret"
    )
