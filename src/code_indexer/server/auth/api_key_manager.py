"""API Key generation and validation manager."""

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple, TYPE_CHECKING

from .password_manager import PasswordManager

if TYPE_CHECKING:
    from .user_manager import User, UserManager

logger = logging.getLogger(__name__)


class ApiKeyManager:
    """Manages API key generation and validation."""

    KEY_PREFIX = "cidx_sk_"
    KEY_LENGTH = 16  # 16 bytes = 32 hex chars = 128-bit entropy

    def __init__(self, user_manager: Optional["UserManager"] = None) -> None:
        """
        Initialize API key manager.

        Args:
            user_manager: UserManager instance for storing API keys
        """
        self.user_manager = user_manager
        self.password_manager = PasswordManager()

    def generate_key(
        self, username: str, name: Optional[str] = None
    ) -> Tuple[str, str]:
        """
        Generate a new API key and store it for the user.

        Args:
            username: Username to associate the key with
            name: Optional name for the key

        Returns:
            Tuple of (raw_key, key_id)
        """
        # Generate random bytes and convert to hex
        random_bytes = secrets.token_hex(self.KEY_LENGTH)
        raw_key = f"{self.KEY_PREFIX}{random_bytes}"

        # Extract key prefix for display (first 12 chars: "cidx_sk_" + first 4 hex chars)
        key_prefix = raw_key[:12]

        # Generate unique key ID
        key_id = str(uuid.uuid4())

        # Hash the key for storage (bcrypt — slow, but run once at creation)
        key_hash = self.password_manager.hash_password(raw_key)

        # SHA-256 for O(1) bearer-auth lookup (Bug #1144)
        key_sha256 = hashlib.sha256(raw_key.encode()).hexdigest()

        # Timestamp
        created_at = datetime.now(timezone.utc).isoformat()

        # Store in user's api_keys array
        if self.user_manager:
            self.user_manager.add_api_key(
                username=username,
                key_id=key_id,
                key_hash=key_hash,
                key_prefix=key_prefix,
                name=name,
                created_at=created_at,
                key_sha256=key_sha256,
            )

        return raw_key, key_id

    def validate_key(self, raw_key: str, stored_hash: str) -> bool:
        """
        Validate a raw API key against stored hash.

        Args:
            raw_key: Raw API key from request
            stored_hash: Stored hash from database

        Returns:
            True if key is valid, False otherwise
        """
        result = self.password_manager.verify_password(raw_key, stored_hash)
        return bool(result)  # Explicit cast to satisfy mypy

    def authenticate_bearer(self, raw_key: str) -> Optional["User"]:
        """
        Authenticate an API key provided as a Bearer token (Bug #1144).

        Security design: Option B (SHA-256 lookup + bcrypt confirm).
        1. Prefix fast-reject: if token does not start with KEY_PREFIX → None (no DB hit).
        2. SHA-256 lookup: deterministic indexed SELECT, never scans all rows.
        3. bcrypt confirm: defense-in-depth against SHA-256 collision.
        4. Fetch full User object for the owning username.

        Security invariants:
        - NEVER log raw_key or key_sha256 — logged only key_id / key_prefix.
        - NEVER cache the lookup — live DB read on every call → revocation immediate.
        - Legacy rows (NULL key_sha256) return None without scanning bcrypt hashes.

        Args:
            raw_key: Raw Bearer token string from Authorization header.

        Returns:
            User object if valid, None otherwise.
        """
        # Step 1: Prefix fast-reject — JWTs and OAuth tokens never start with KEY_PREFIX
        if not raw_key.startswith(self.KEY_PREFIX):
            return None

        if self.user_manager is None:
            logger.warning("authenticate_bearer: user_manager not set")
            return None

        # Step 2: SHA-256 lookup (indexed, O(1))
        sha256_hex = hashlib.sha256(raw_key.encode()).hexdigest()
        record = self.user_manager.get_api_key_by_sha256(sha256_hex)
        if record is None:
            # Key not found (includes legacy NULL-sha256 rows)
            return None

        # Step 3: bcrypt confirm (defense-in-depth vs SHA-256 collision)
        stored_hash = record.get("key_hash", "")
        if not stored_hash or not self.validate_key(raw_key, stored_hash):
            return None

        # Step 4: Fetch full User for the owning username
        username = record.get("username", "")
        user = self.user_manager.get_user(username)
        if user is None:
            return None

        key_id = record.get("key_id", "unknown")
        key_prefix = record.get("key_prefix", "cidx_sk_****")

        logger.debug(
            "authenticate_bearer: success for username=%s key_id=%s key_prefix=%s",
            username,
            key_id,
            key_prefix,
        )
        return user
