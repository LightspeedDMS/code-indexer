"""
CI Token Manager Service.

Manages GitHub and GitLab API tokens with AES-256-CBC encryption.
Tokens are stored in ~/.cidx-server/ci_tokens.json with 0600 permissions.
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import json
import logging
import re
import threading
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set, Tuple, cast

from code_indexer.server.logging_utils import format_error_log
from .locked_secret_file import file_lock, write_atomically
from .stored_secret_key import StoredSecretCipher

logger = logging.getLogger(__name__)


def _record_ci_token(actor: str, action_type: str, platform: str, outcome: str) -> None:
    """Record one CI token row: the platform only, never the token."""
    from .audit_outcome import conforming_details, record_outcome

    record_outcome(
        actor=actor,
        action_type=action_type,
        target_type="ci_token",
        target_id=platform,
        outcome=outcome,
        details=conforming_details(action_type, platform=platform),
    )


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
    # The stored token cannot be decrypted with any available key.
    needs_reentry: bool = False


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

        if server_dir_path:
            self.server_dir = Path(server_dir_path)
        else:
            self.server_dir = Path.home() / ".cidx-server"

        key_dir = Path(server_dir) if server_dir else self.server_dir
        self._cipher = StoredSecretCipher(
            key_dir, "CITokenManager", cluster_secret=cluster_secret
        )

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

    def _encrypt_token(self, token: str) -> str:
        """Encrypt a token for storage (v2 when the cluster gate allows)."""
        return self._cipher.encrypt(token)

    def _decrypt_token(self, encrypted_token: str) -> Tuple[str, bool]:
        """(plaintext, is_legacy); raises a ValueError subclass when unreadable."""
        return self._cipher.decrypt(encrypted_token)

    def _read_stored_token(
        self, platform: str, encrypted_token: str, marked: bool = False
    ) -> str:
        """Decrypt a stored token, rewriting a legacy value as v2 (CAS).

        An undecryptable value is marked as needing re-entry (never deleted)
        and its ValueError propagates."""
        backend = self._sqlite_backend
        assert backend is not None, "storage backend required"
        try:
            plaintext = self._cipher.read(
                encrypted_token,
                lambda old, new: backend.replace_encrypted_token_if_unchanged(
                    platform, old, new
                ),
            )
            # Readable again (e.g. the right key restored): drop a stale mark.
            # Only a marked row is written; an unmarked read stays read-only.
            if marked:
                backend.clear_needs_reentry(platform, encrypted_token)
            return plaintext
        except ValueError:
            try:
                backend.mark_needs_reentry(
                    platform, encrypted_token, node_id=self._cipher.node_id
                )
            except Exception:
                logger.warning(
                    "Could not mark %s CI token as needing re-entry",
                    platform,
                    exc_info=True,
                )
            raise

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
        # Callers hold file_lock(self.token_file); unique 0600 temp + replace.
        self.server_dir.mkdir(parents=True, exist_ok=True)
        write_atomically(self.token_file, json.dumps(tokens, indent=2))

    def _update_json_entry(
        self, platform: str, expected_token: str, mutate: Callable[[Dict], None]
    ) -> None:
        """Apply *mutate* to the stored entry only while it still holds
        *expected_token* (a concurrent save is never overwritten)."""
        self.server_dir.mkdir(parents=True, exist_ok=True)
        with file_lock(self.token_file):
            tokens = self._load_tokens()
            entry = tokens.get(platform)
            if entry is not None and entry.get("token") == expected_token:
                mutate(entry)
                self._save_tokens(tokens)

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
            # JSON file storage (backward compatible); a new value drops any
            # needs-re-entry mark.
            self.server_dir.mkdir(parents=True, exist_ok=True)
            with file_lock(self.token_file):
                tokens = self._load_tokens()
                tokens[platform] = {"token": encrypted_token, "base_url": base_url}
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
                decrypted_token = self._read_stored_token(
                    platform,
                    token_row["encrypted_token"],
                    marked=bool(token_row.get("needs_reentry_at")),
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

            stored = token_data["token"]
            # Decrypt with graceful error handling (Issue #716 Bug 3); the
            # stored value is never deleted.
            try:
                decrypted_token, is_legacy = self._decrypt_token(stored)
            except Exception as e:
                logger.warning(
                    format_error_log(
                        "APP-GENERAL-062",
                        f"Failed to decrypt {platform} token, treating as "
                        f"unconfigured (kept, needs re-entry): {type(e).__name__}",
                    )
                )
                self._update_json_entry(
                    platform, stored, lambda e: e.__setitem__("needs_reentry", True)
                )
                return None

            if is_legacy:
                new_value = self._cipher.rewrite_value(decrypted_token)
                if new_value is not None:
                    self._update_json_entry(
                        platform,
                        stored,
                        lambda e: e.update(token=new_value, needs_reentry=False),
                    )
            return TokenData(
                platform=platform,
                token=decrypted_token,
                base_url=token_data.get("base_url"),
            )

    def delete_token(self, platform: str) -> bool:
        """
        Delete a platform token.

        Args:
            platform: Platform name (github or gitlab)

        Returns:
            True when a stored token was removed, False when none existed.
        """
        deleted = False
        if self._use_sqlite and self._sqlite_backend is not None:
            # SQLite backend (Story #702)
            deleted = bool(self._sqlite_backend.delete_token(platform))
        else:
            # JSON file storage (backward compatible): an explicit delete
            self.server_dir.mkdir(parents=True, exist_ok=True)
            with file_lock(self.token_file):
                tokens = self._load_tokens()
                if platform in tokens:
                    del tokens[platform]
                    self._save_tokens(tokens)
                    deleted = True
        if deleted:
            logger.info(
                f"Deleted token for platform: {platform}",
                extra={"correlation_id": get_correlation_id()},
            )
        return deleted

    # ------------------------------------------------------------------
    # Audited entry points (the Web configuration door calls these).  The
    # row names the platform only; the token never reaches it.
    # ------------------------------------------------------------------

    def save_token_audited(
        self,
        platform: str,
        token: str,
        base_url: Optional[str] = None,
        *,
        actor: str,
    ) -> None:
        """:meth:`save_token`, recording ``ci_token_set``."""
        try:
            self.save_token(platform, token, base_url=base_url)
        except Exception:
            _record_ci_token(actor, "ci_token_set", platform, "failure")
            raise
        _record_ci_token(actor, "ci_token_set", platform, "success")

    def delete_token_audited(self, platform: str, *, actor: str) -> bool:
        """:meth:`delete_token`, recording ``ci_token_deleted``.

        Deleting a platform with no stored token is recorded as a failure.
        """
        try:
            deleted = self.delete_token(platform)
        except Exception:
            _record_ci_token(actor, "ci_token_deleted", platform, "failure")
            raise
        _record_ci_token(
            actor, "ci_token_deleted", platform, "success" if deleted else "failure"
        )
        return deleted

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
            marked = {m["id"] for m in self._sqlite_backend.list_needs_reentry()}

            result = {}
            for platform in platforms:
                if platform in tokens:
                    result[platform] = TokenStatus(
                        platform=platform,
                        configured=True,
                        base_url=tokens[platform].get("base_url"),
                        needs_reentry=platform in marked,
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
                        needs_reentry=bool(tokens[platform].get("needs_reentry")),
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

    The stored-secret key lives in server_dir (solo) or cluster_secrets
    (cluster). In postgres mode the legacy salt file is still seeded: legacy
    writes use it while a cluster node cannot yet read v2 values.

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

    if storage_mode == "postgres":
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
