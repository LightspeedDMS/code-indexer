"""
CI Token Manager Service.

Manages GitHub and GitLab API tokens with AES-256-CBC encryption.
Tokens are stored in ~/.cidx-server/ci_tokens.json with 0600 permissions.
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple, cast

from code_indexer.server.logging_utils import format_error_log
from .token_encryption import (
    derive_key_from_salt as _derive_key_from_salt_fn,
    derive_encryption_key as _derive_encryption_key_fn,
    encrypt_token as _encrypt_token_fn,
    decrypt_single as _decrypt_single_fn,
    decrypt_with_fallback as _decrypt_with_fallback_fn,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# De-spam memo for APP-GENERAL-061 (Bug #1222)
#
# CITokenManager is constructed fresh at every call site (NOT a singleton).
# Without a module-level memo, the same undecryptable ciphertext floods
# WARNING logs on every check-credentials request.
#
# Key: (platform, sha256_hexdigest(encrypted_token)) — NEVER the raw value.
# Thread-safe under FastAPI workers via _DECRYPT_WARN_SEEN_LOCK.
# ---------------------------------------------------------------------------
_DECRYPT_WARN_SEEN: Set[Tuple[str, str]] = set()
_DECRYPT_WARN_SEEN_LOCK: threading.Lock = threading.Lock()


# Token validation patterns
GITHUB_TOKEN_PATTERN = re.compile(
    r"^(ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,255})$"
)
# GitLab tokens can have periods in newer versioned formats (e.g., glpat-xxx.01.yyy)
GITLAB_TOKEN_PATTERN = re.compile(r"^glpat-[A-Za-z0-9_.-]{20,}$")


class TokenValidationError(Exception):
    """Raised when token format validation fails."""

    pass


@dataclass
class TokenData:
    """Data structure for stored token information."""

    platform: str
    token: str
    base_url: Optional[str] = None


@dataclass
class TokenStatus:
    """Status information for a platform's token configuration."""

    platform: str
    configured: bool
    base_url: Optional[str] = None


class CITokenManager:
    """
    Manages CI/CD platform API tokens with encryption.

    Features:
    - AES-256-CBC encryption with PBKDF2 key derivation
    - Secure file permissions (0600)
    - Token format validation
    - Support for GitHub and GitLab tokens

    Supports both SQLite backend (Story #702) and JSON file storage (backward compatible).
    """

    def __init__(
        self,
        server_dir_path: Optional[str] = None,
        use_sqlite: bool = False,
        db_path: Optional[str] = None,
        storage_backend: Optional[Any] = None,
        cluster_secret: Optional[str] = None,
        server_dir: Optional[str] = None,
    ):
        """
        Initialize the token manager.

        Args:
            server_dir_path: Optional path to server directory.
                           Defaults to ~/.cidx-server
            use_sqlite: If True, use SQLite backend instead of JSON file (Story #702)
            db_path: Path to SQLite database file (required when use_sqlite=True)
            storage_backend: Optional CITokensBackend for delegation
                             (used in PG/cluster mode, Bug #532). When set,
                             use_sqlite and db_path are ignored and the
                             provided backend is used directly.
            cluster_secret: Shared secret for encryption key derivation in
                            cluster mode (Bug #533). When provided, all nodes
                            derive the same key so tokens are readable
                            cluster-wide. When None, uses hostname (standalone).
            server_dir: Optional path to server directory for reading
                        .encryption_key_salt (Story #999). When provided,
                        overrides cluster_secret for key derivation.
        """
        self._sqlite_backend: Optional[Any] = None
        self._cluster_secret = cluster_secret
        self._server_dir_for_salt: Optional[Path] = (
            Path(server_dir) if server_dir else None
        )

        if server_dir_path:
            self.server_dir = Path(server_dir_path)
        else:
            self.server_dir = Path.home() / ".cidx-server"

        self._encryption_key = self._derive_encryption_key()

        if storage_backend is not None:
            # PG/cluster mode: use the provided backend directly via existing plumbing
            self._use_sqlite = True
            self._sqlite_backend = storage_backend
        elif use_sqlite:
            self._use_sqlite = True
            if db_path is None:
                raise ValueError("db_path is required when use_sqlite=True")
            from code_indexer.server.storage.sqlite_backends import (
                CITokensSqliteBackend,
            )

            self._sqlite_backend = CITokensSqliteBackend(db_path)
        else:
            self._use_sqlite = False
            # JSON file storage (backward compatible)
            self.token_file = self.server_dir / "ci_tokens.json"

    def _derive_encryption_key(self) -> bytes:
        """Derive encryption key using token_encryption priority chain (Story #999)."""
        return _derive_encryption_key_fn(
            server_dir_for_salt=self._server_dir_for_salt,
            cluster_secret=self._cluster_secret,
        )

    @staticmethod
    def _derive_key_from_salt(salt_input: str) -> bytes:
        """Derive AES-256 key from a salt string — delegates to token_encryption."""
        return _derive_key_from_salt_fn(salt_input)

    def _encrypt_token(self, token: str) -> str:
        """Encrypt token using AES-256-CBC — delegates to token_encryption."""
        return _encrypt_token_fn(token, self._encryption_key)

    @staticmethod
    def _do_decrypt(encrypted_token: str, key: bytes) -> str:
        """Decrypt with explicit key — delegates to token_encryption."""
        return _decrypt_single_fn(encrypted_token, key)

    def _decrypt_token(self, encrypted_token: str) -> Tuple[str, bool]:
        """Decrypt token with canonical key, falling back to hostname key (Story #999).

        Returns:
            Tuple of (plaintext: str, used_fallback: bool). Callers must re-encrypt
            the token when used_fallback is True (Story #999 lazy re-encryption).

        Raises:
            ValueError: If both canonical and hostname-fallback keys fail.
            binascii.Error: If the input is not valid base64.
        """
        return _decrypt_with_fallback_fn(
            encrypted_token,
            canonical_key=self._encryption_key,
            context_label="CITokenManager",
        )

    def _validate_token_format(self, platform: str, token: str) -> None:
        """
        Validate token format for the given platform.

        Args:
            platform: Platform name (github or gitlab)
            token: Token to validate

        Raises:
            TokenValidationError: If token format is invalid
        """
        if platform == "github":
            if not GITHUB_TOKEN_PATTERN.match(token):
                raise TokenValidationError(
                    "Invalid GitHub token format. Expected format: "
                    "ghp_<36 chars> or github_pat_<22-255 chars>"
                )
        elif platform == "gitlab":
            if not GITLAB_TOKEN_PATTERN.match(token):
                raise TokenValidationError(
                    "Invalid GitLab token format. Expected format: glpat-<20+ chars>"
                )
        else:
            raise TokenValidationError(f"Unknown platform: {platform}")

    def _load_tokens(self) -> Dict[str, Any]:
        """
        Load tokens from storage file.

        Returns:
            Dictionary of stored token data
        """
        if not self.token_file.exists():
            return {}

        with open(self.token_file, "r") as f:
            return cast(Dict[str, Any], json.load(f))

    def _save_tokens(self, tokens: Dict) -> None:
        """
        Save tokens to storage file with secure permissions.

        Args:
            tokens: Dictionary of token data to save
        """
        # Ensure server directory exists
        self.server_dir.mkdir(parents=True, exist_ok=True)

        # Write tokens to file
        with open(self.token_file, "w") as f:
            json.dump(tokens, f, indent=2)

        # Set secure permissions (0600)
        os.chmod(self.token_file, 0o600)

    def save_token(
        self, platform: str, token: str, base_url: Optional[str] = None
    ) -> None:
        """
        Save and encrypt a CI/CD platform token.

        Args:
            platform: Platform name (github or gitlab)
            token: API token to save
            base_url: Optional custom base URL (for self-hosted instances)

        Raises:
            TokenValidationError: If token format is invalid
        """
        # Validate token format
        self._validate_token_format(platform, token)

        # Encrypt token
        encrypted_token = self._encrypt_token(token)

        if self._use_sqlite and self._sqlite_backend is not None:
            # SQLite backend (Story #702)
            self._sqlite_backend.save_token(platform, encrypted_token, base_url)
        else:
            # JSON file storage (backward compatible)
            # Load existing tokens
            tokens = self._load_tokens()

            # Update token data
            tokens[platform] = {"token": encrypted_token, "base_url": base_url}

            # Save to file with secure permissions
            self._save_tokens(tokens)

        logger.info(
            f"Saved encrypted token for platform: {platform}",
            extra={"correlation_id": get_correlation_id()},
        )

    def get_token(self, platform: str) -> Optional[TokenData]:
        """
        Retrieve and decrypt a platform token.

        Args:
            platform: Platform name (github or gitlab)

        Returns:
            TokenData if token exists, None otherwise
        """
        if self._use_sqlite and self._sqlite_backend is not None:
            # SQLite backend (Story #702)
            token_row = self._sqlite_backend.get_token(platform)
            if token_row is None:
                return None
            # Decrypt token with graceful error handling (Issue #716 Bug 3)
            try:
                decrypted_token, used_fallback = self._decrypt_token(
                    token_row["encrypted_token"]
                )
            except Exception as e:
                # De-spam memo (Bug #1222): log WARNING once per distinct
                # (platform, ciphertext-hash) pair; downgrade to DEBUG on repeat.
                _enc = token_row["encrypted_token"]
                _key = (platform, sha256(_enc.encode()).hexdigest())
                with _DECRYPT_WARN_SEEN_LOCK:
                    _is_new = _key not in _DECRYPT_WARN_SEEN
                    if _is_new:
                        _DECRYPT_WARN_SEEN.add(_key)
                _msg = format_error_log(
                    "APP-GENERAL-061",
                    f"Failed to decrypt {platform} token, treating as unconfigured "
                    f"(token preserved in DB for recovery): {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
                if _is_new:
                    logger.warning(_msg)
                else:
                    logger.debug(_msg)
                return None
            # Clear any memo entry for this platform on successful decrypt so that
            # a genuinely new bad ciphertext (e.g. after token rotation) warns once again.
            _enc_success = token_row["encrypted_token"]
            _key_success = (platform, sha256(_enc_success.encode()).hexdigest())
            with _DECRYPT_WARN_SEEN_LOCK:
                _DECRYPT_WARN_SEEN.discard(_key_success)
            if used_fallback:
                new_enc = self._encrypt_token(decrypted_token)
                self._sqlite_backend.update_encrypted_token(platform, new_enc)
            return TokenData(
                platform=platform,
                token=decrypted_token,
                base_url=token_row.get("base_url"),
            )
        else:
            # JSON file storage (backward compatible)
            tokens = self._load_tokens()

            if platform not in tokens:
                return None

            token_data = tokens[platform]

            # Decrypt token with graceful error handling (Issue #716 Bug 3)
            try:
                decrypted_token, used_fallback = self._decrypt_token(
                    token_data["token"]
                )
            except Exception as e:
                logger.warning(
                    format_error_log(
                        "APP-GENERAL-062",
                        f"Failed to decrypt {platform} token, treating as unconfigured: {e}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                # Delete the corrupted token from storage
                del tokens[platform]
                self._save_tokens(tokens)
                return None

            if used_fallback:
                logger.warning(
                    "CITokenManager (JSON path): %s token decrypted via fallback — "
                    "re-encryption not possible without SQLite backend (Story #999)",
                    platform,
                )
            return TokenData(
                platform=platform,
                token=decrypted_token,
                base_url=token_data.get("base_url"),
            )

    def delete_token(self, platform: str) -> None:
        """
        Delete a platform token.

        Args:
            platform: Platform name (github or gitlab)
        """
        if self._use_sqlite and self._sqlite_backend is not None:
            # SQLite backend (Story #702)
            deleted = self._sqlite_backend.delete_token(platform)
            if deleted:
                logger.info(
                    f"Deleted token for platform: {platform}",
                    extra={"correlation_id": get_correlation_id()},
                )
        else:
            # JSON file storage (backward compatible)
            tokens = self._load_tokens()

            if platform in tokens:
                del tokens[platform]
                self._save_tokens(tokens)
                logger.info(
                    f"Deleted token for platform: {platform}",
                    extra={"correlation_id": get_correlation_id()},
                )

    def list_tokens(self) -> Dict[str, TokenStatus]:
        """
        List all platform token statuses.

        Returns:
            Dictionary mapping platform names to TokenStatus objects
        """
        # Known platforms
        platforms = ["github", "gitlab"]

        if self._use_sqlite and self._sqlite_backend is not None:
            # SQLite backend (Story #702)
            tokens = self._sqlite_backend.list_tokens()

            result = {}
            for platform in platforms:
                if platform in tokens:
                    result[platform] = TokenStatus(
                        platform=platform,
                        configured=True,
                        base_url=tokens[platform].get("base_url"),
                    )
                else:
                    result[platform] = TokenStatus(platform=platform, configured=False)

            return result
        else:
            # JSON file storage (backward compatible)
            tokens = self._load_tokens()

            result = {}
            for platform in platforms:
                if platform in tokens:
                    result[platform] = TokenStatus(
                        platform=platform,
                        configured=True,
                        base_url=tokens[platform].get("base_url"),
                    )
                else:
                    result[platform] = TokenStatus(platform=platform, configured=False)

            return result


_VALID_STORAGE_MODES = {"sqlite", "postgres"}


def create_token_manager(
    server_dir: str,
    db_path: str,
    storage_backend: Any = None,
    storage_mode: str = "sqlite",
) -> CITokenManager:
    """Single factory for CITokenManager — ensures consistent encryption key.

    All code paths that need a CITokenManager MUST use this factory to avoid
    encryption key mismatch bugs (Bug #639).

    Story #999: Calls ensure_encryption_key_salt() to seed .encryption_key_salt
    from .jwt_secret (in postgres mode) or hostname (in sqlite mode), then passes
    server_dir to CITokenManager so it reads the salt file for key derivation.

    Args:
        server_dir: Path to server directory (e.g., ~/.cidx-server)
        db_path: Path to SQLite database file
        storage_backend: Optional CITokensBackend for PG/cluster mode
        storage_mode: "sqlite" (standalone) or "postgres" (cluster)

    Raises:
        ValueError: If storage_mode is not "sqlite" or "postgres"
    """
    if storage_mode not in _VALID_STORAGE_MODES:
        raise ValueError(
            f"Invalid storage_mode {storage_mode!r}. Must be one of: {_VALID_STORAGE_MODES}"
        )

    from code_indexer.server.services.encryption_key_salt import (
        ensure_encryption_key_salt,
    )

    ensure_encryption_key_salt(Path(server_dir), storage_mode)

    return CITokenManager(
        server_dir_path=server_dir,
        use_sqlite=True,
        db_path=db_path,
        storage_backend=storage_backend,
        server_dir=server_dir,
    )
