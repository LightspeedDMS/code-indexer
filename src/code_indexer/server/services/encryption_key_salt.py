"""
Encryption key salt management for unified token encryption (Story #999).

The legacy .encryption_key_salt file: a legacy key source used to read
stored secrets written by earlier versions, and to write the legacy format
while a cluster still has nodes that cannot read the v2 format. An existing
file is returned unchanged. Unsupported storage_mode raises ValueError; a
None server_dir raises TypeError.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_SALT_FILENAME = ".encryption_key_salt"
_JWT_SECRET_FILENAME = ".jwt_secret"
_VALID_STORAGE_MODES = frozenset({"sqlite", "postgres"})


def ensure_encryption_key_salt(server_dir: Path, storage_mode: str) -> str:
    """Return the encryption key salt for the server, creating it if absent.

    Args:
        server_dir: Path to the server data directory (e.g. ~/.cidx-server).
        storage_mode: "sqlite" for standalone mode, "postgres" for cluster mode.

    Returns:
        The salt string to use for encryption key derivation.

    Raises:
        TypeError: If server_dir is None.
        ValueError: If storage_mode is not "sqlite" or "postgres".
    """
    if server_dir is None:
        raise TypeError("server_dir must not be None")
    if storage_mode not in _VALID_STORAGE_MODES:
        raise ValueError(
            f"Invalid storage_mode {storage_mode!r}. "
            f"Must be one of: {sorted(_VALID_STORAGE_MODES)}"
        )

    salt_file = Path(server_dir) / _SALT_FILENAME
    if salt_file.exists():
        return salt_file.read_text()

    salt = _compute_initial_salt(Path(server_dir), storage_mode)
    salt_file.write_text(salt)
    os.chmod(salt_file, 0o600)
    return salt


def read_encryption_key_salt(server_dir: Path) -> str:
    """Read the existing encryption key salt file.

    Args:
        server_dir: Path to the server data directory.

    Returns:
        The salt string from the file.

    Raises:
        TypeError: If server_dir is None.
        FileNotFoundError: If .encryption_key_salt does not exist.
    """
    if server_dir is None:
        raise TypeError("server_dir must not be None")
    salt_file = Path(server_dir) / _SALT_FILENAME
    if not salt_file.exists():
        raise FileNotFoundError(
            f"Encryption key salt file not found: {salt_file}. "
            "Call ensure_encryption_key_salt() first."
        )
    return salt_file.read_text()


def _compute_initial_salt(server_dir: Path, storage_mode: str) -> str:
    """Compute the initial salt value based on storage mode.

    Caller guarantees storage_mode is valid and server_dir is not None.
    """
    if storage_mode == "sqlite":
        return os.uname().nodename
    elif storage_mode == "postgres":
        jwt_file = server_dir / _JWT_SECRET_FILENAME
        if jwt_file.exists():
            return jwt_file.read_text().strip()
        logger.warning(
            "ensure_encryption_key_salt: .jwt_secret not found at %s — "
            "falling back to hostname-based salt. Tokens may not be readable "
            "across cluster nodes until .jwt_secret is present.",
            jwt_file,
        )
        return os.uname().nodename
    else:
        raise ValueError(
            f"Unhandled storage_mode {storage_mode!r} in _compute_initial_salt"
        )
