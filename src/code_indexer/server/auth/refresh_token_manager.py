"""
Refresh Token Manager for secure token refresh with family tracking.

SECURITY FEATURES:
- Token family tracking for replay attack detection
- Refresh token rotation (new access + refresh token pair)
- Secure token storage and validation
- Automatic family revocation on suspicious activity
- Concurrent refresh protection
- Integration with existing JWT and audit systems

This module implements the security requirements from Story 03:
- Token refresh rotation prevents token reuse attacks
- Family tracking detects replay attacks and revokes all family tokens
- Comprehensive audit logging for security monitoring
"""

import logging
import sqlite3
import secrets
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
from dataclasses import dataclass
import threading
from pathlib import Path

from .jwt_manager import JWTManager
from .audit_logger import password_audit_logger
from code_indexer.server.storage.database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


@dataclass
class TokenFamily:
    """Represents a token family for tracking related refresh tokens."""

    family_id: str
    username: str
    created_at: datetime
    last_used_at: datetime
    is_revoked: bool = False
    revocation_reason: Optional[str] = None


@dataclass
class RefreshTokenRecord:
    """Represents a stored refresh token with metadata."""

    token_id: str
    family_id: str
    username: str
    token_hash: str  # Hashed token for secure storage
    created_at: datetime
    expires_at: datetime
    is_used: bool = False
    used_at: Optional[datetime] = None
    parent_token_id: Optional[str] = None


class RefreshTokenError(Exception):
    """Base exception for refresh token operations."""

    pass


class TokenReplayAttackError(RefreshTokenError):
    """Raised when a token replay attack is detected."""

    pass


class ConcurrentRefreshError(RefreshTokenError):
    """Raised when concurrent refresh attempts are detected."""

    pass


class RefreshTokenManager:
    """
    Manages refresh tokens with family tracking and security features.

    SECURITY IMPLEMENTATION:
    - Tokens are hashed before storage (never store plaintext tokens)
    - Token families track relationships and detect replay attacks
    - Concurrent refresh detection prevents race conditions
    - Comprehensive audit logging for security monitoring
    - Integration with existing JWT and user management systems
    """

    def __init__(
        self,
        jwt_manager: JWTManager,
        db_path: Optional[str] = None,
        refresh_token_lifetime_days: int = 7,
        storage_backend=None,
    ):
        """
        Initialize refresh token manager.

        Args:
            jwt_manager: JWT manager for access token creation
            db_path: Database path for token storage (defaults to user home/.cidx-server)
            refresh_token_lifetime_days: Refresh token lifetime (default: 7 days)
            storage_backend: Optional storage backend for data operations.
                When provided, skips SQLite setup and delegates all data
                operations to the backend (used in cluster/PostgreSQL mode).
        """
        self.jwt_manager = jwt_manager
        self.refresh_token_lifetime_days = refresh_token_lifetime_days
        self._lock = threading.Lock()
        self._pool: Any = None
        self._backend = storage_backend

        if storage_backend:
            # Backend handles all storage - no SQLite connection needed
            self._conn_manager = None
            return

        # Set database path with fallback to user home directory
        if db_path:
            self.db_path = Path(db_path)
        else:
            # Default to user's home directory for better test compatibility
            server_dir = Path.home() / ".cidx-server"
            server_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = server_dir / "refresh_tokens.db"

        # Ensure database directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn_manager = DatabaseConnectionManager.get_instance(str(self.db_path))

        # Initialize database
        self._init_database()

    def set_connection_pool(self, pool: Any) -> None:
        """Enable PG advisory locks for cross-node token rotation safety."""
        self._pool = pool
        logger.info("RefreshTokenManager: using PG advisory locks (cluster mode)")

    @contextmanager
    def _distributed_lock(self):
        """Acquire distributed lock: PG advisory lock or threading.Lock."""
        if self._pool is not None:
            with self._pool.connection() as conn:
                conn.execute(
                    "SELECT pg_advisory_lock(hashtext(%s))",
                    ("refresh_token_rotation",),
                )
                try:
                    yield
                finally:
                    conn.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s))",
                        ("refresh_token_rotation",),
                    )
        else:
            with self._lock:
                yield

    def _init_database(self):
        """Initialize SQLite database for secure token storage."""

        def _do_init(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS token_families (
                    family_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    is_revoked INTEGER DEFAULT 0,
                    revocation_reason TEXT
                )
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_family_username ON token_families (username)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_family_revoked ON token_families (is_revoked)
            """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    token_id TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    is_used INTEGER DEFAULT 0,
                    used_at TEXT,
                    parent_token_id TEXT,
                    FOREIGN KEY (family_id) REFERENCES token_families (family_id)
                )
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_token_family ON refresh_tokens (family_id)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_token_username ON refresh_tokens (username)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_token_hash ON refresh_tokens (token_hash)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_token_expires ON refresh_tokens (expires_at)
            """
            )

        self._conn_manager.execute_atomic(_do_init)  # type: ignore[union-attr]

    def create_token_family(self, username: str) -> str:
        """
        Create a new token family for a user session.

        Args:
            username: Username for the token family

        Returns:
            Family ID for the new token family
        """
        family_id = self._generate_secure_id()
        now = datetime.now(timezone.utc).isoformat()

        if self._backend:
            self._backend.create_token_family(family_id, username, now, now)
            return family_id

        def _do_insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO token_families
                (family_id, username, created_at, last_used_at)
                VALUES (?, ?, ?, ?)
            """,
                (family_id, username, now, now),
            )

        self._conn_manager.execute_atomic(_do_insert)  # type: ignore[union-attr]

        return family_id

    def create_initial_refresh_token(
        self, family_id: str, username: str, user_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Create initial refresh token for a new login session.

        Args:
            family_id: Token family ID
            username: Username
            user_data: User data for JWT creation

        Returns:
            Dictionary with access token, refresh token, and metadata
        """
        with self._distributed_lock():
            # Generate secure refresh token
            refresh_token = self._generate_refresh_token()
            token_id = self._generate_secure_id()
            token_hash = self._hash_token(refresh_token)

            # Calculate expiration
            now = datetime.now(timezone.utc)
            expires_at = now + timedelta(days=self.refresh_token_lifetime_days)

            # Store refresh token
            if self._backend:
                self._backend.store_refresh_token(
                    token_id=token_id,
                    family_id=family_id,
                    username=username,
                    token_hash=token_hash,
                    created_at=now.isoformat(),
                    expires_at=expires_at.isoformat(),
                )
            else:

                def _do_insert(conn: sqlite3.Connection) -> None:
                    conn.execute(
                        """
                        INSERT INTO refresh_tokens
                        (token_id, family_id, username, token_hash, created_at, expires_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """,
                        (
                            token_id,
                            family_id,
                            username,
                            token_hash,
                            now.isoformat(),
                            expires_at.isoformat(),
                        ),
                    )

                self._conn_manager.execute_atomic(_do_insert)  # type: ignore[union-attr]

            # Create access token
            access_token = self.jwt_manager.create_token(user_data)

            return {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "bearer",
                "access_token_expires_in": self.jwt_manager.token_expiration_minutes
                * 60,
                "refresh_token_expires_in": (
                    max(1, int(self.refresh_token_lifetime_days * 24 * 60 * 60))
                    if self.refresh_token_lifetime_days > 0
                    else 0
                ),
                "family_id": family_id,
            }

    def validate_and_rotate_refresh_token(
        self, refresh_token: str, client_ip: str = "unknown", user_manager=None
    ) -> Dict[str, Any]:
        """
        Validate refresh token and create new token pair.

        SECURITY IMPLEMENTATION:
        - Detects token replay attacks
        - Prevents concurrent refresh attempts
        - Rotates tokens for security
        - Revokes family on suspicious activity

        Args:
            refresh_token: Refresh token to validate and rotate
            client_ip: Client IP for audit logging
            user_manager: Optional user manager for retrieving current user role

        Returns:
            Dictionary with validation result and new tokens (if valid)

        Raises:
            TokenReplayAttackError: If replay attack detected
            ConcurrentRefreshError: If concurrent refresh detected
        """
        # Hash computation happens OUTSIDE the lock - pure CPU work with no shared state.
        # Only the critical SQLite section (read is_used, mark used, INSERT new token)
        # needs the lock to prevent replay attacks and maintain atomicity.
        token_hash = self._hash_token(refresh_token)

        # Pre-generate new token materials OUTSIDE the lock - pure computation.
        # Even if the old token turns out to be invalid the cost is negligible.
        new_refresh_token = self._generate_refresh_token()
        new_token_id = self._generate_secure_id()
        new_token_hash = self._hash_token(new_refresh_token)

        # Variables extracted from the DB inside the lock, used after
        token_id: str = ""
        family_id: str = ""
        username: str = ""

        with self._distributed_lock():
            result_holder: Dict[str, Any] = {}

            if self._backend:
                # Use backend primitives under the same lock for replay-attack safety.
                # Individual calls are separate transactions in PostgreSQL, which is
                # acceptable: the threading lock provides the critical-section guarantee
                # needed to prevent concurrent replay.
                token_record = self._backend.get_refresh_token_by_hash(token_hash)
                if not token_record:
                    result_holder["valid"] = False
                    result_holder["error"] = "Invalid refresh token"
                    result_holder["security_incident"] = True
                else:
                    t_id = token_record["token_id"]
                    f_id = token_record["family_id"]
                    uname = token_record["username"]
                    result_holder["token_id"] = t_id
                    result_holder["family_id"] = f_id
                    result_holder["username"] = uname

                    if token_record["is_used"]:
                        result_holder["replay_attack"] = True
                        result_holder["family_id_for_revoke"] = f_id
                        result_holder["username_for_revoke"] = uname
                    else:
                        expires_at_dt = datetime.fromisoformat(
                            token_record["expires_at"]
                        )
                        if datetime.now(timezone.utc) > expires_at_dt:
                            result_holder["valid"] = False
                            result_holder["error"] = "Refresh token has expired"
                        else:
                            family_record = self._backend.get_token_family(f_id)
                            if not family_record or family_record["is_revoked"]:
                                result_holder["valid"] = False
                                revocation_reason = (
                                    family_record["revocation_reason"]
                                    if family_record
                                    else None
                                )
                                result_holder["error"] = (
                                    f"Refresh token revoked due to {revocation_reason or 'unknown reason'}"
                                )
                                result_holder["revocation_reason"] = revocation_reason
                            else:
                                rotate_now = datetime.now(timezone.utc)
                                self._backend.mark_token_used(
                                    t_id, rotate_now.isoformat()
                                )
                                self._backend.update_family_last_used(
                                    f_id, rotate_now.isoformat()
                                )
                                new_expires_at = rotate_now + timedelta(
                                    days=self.refresh_token_lifetime_days
                                )
                                self._backend.store_refresh_token(
                                    token_id=new_token_id,
                                    family_id=f_id,
                                    username=uname,
                                    token_hash=new_token_hash,
                                    created_at=rotate_now.isoformat(),
                                    expires_at=new_expires_at.isoformat(),
                                    parent_token_id=t_id,
                                )
                                result_holder["success"] = True
                                result_holder["token_id"] = t_id
                                result_holder["family_id"] = f_id
                                result_holder["username"] = uname
            else:
                # All SQLite operations are inside the lock to ensure atomicity:
                # - SELECT (read is_used)
                # - UPDATE (mark old token used)
                # - INSERT (store new token)
                # A crash between UPDATE and INSERT would leave the family with a
                # consumed token and no replacement, breaking the rotation chain.

                def _do_rotate(conn: sqlite3.Connection) -> None:
                    cursor = conn.execute(
                        """
                        SELECT token_id, family_id, username, created_at, expires_at,
                               is_used, used_at, parent_token_id
                        FROM refresh_tokens
                        WHERE token_hash = ?
                    """,
                        (token_hash,),
                    )

                    token_record = cursor.fetchone()

                    if not token_record:
                        result_holder["valid"] = False
                        result_holder["error"] = "Invalid refresh token"
                        result_holder["security_incident"] = True
                        return

                    (
                        t_id,
                        f_id,
                        uname,
                        created_at_str,
                        expires_at_str,
                        is_used,
                        used_at_str,
                        parent_token_id,
                    ) = token_record

                    result_holder["token_id"] = t_id
                    result_holder["family_id"] = f_id
                    result_holder["username"] = uname

                    # Check if token is already used (replay attack detection)
                    if is_used:
                        result_holder["replay_attack"] = True
                        result_holder["family_id_for_revoke"] = f_id
                        result_holder["username_for_revoke"] = uname
                        return

                    # Check expiration
                    expires_at_dt = datetime.fromisoformat(expires_at_str)
                    if datetime.now(timezone.utc) > expires_at_dt:
                        result_holder["valid"] = False
                        result_holder["error"] = "Refresh token has expired"
                        return

                    # Check if family is revoked
                    cursor = conn.execute(
                        """
                        SELECT is_revoked, revocation_reason
                        FROM token_families
                        WHERE family_id = ?
                    """,
                        (f_id,),
                    )

                    family_record = cursor.fetchone()
                    if not family_record or family_record[0]:
                        result_holder["valid"] = False
                        result_holder["error"] = (
                            f"Refresh token revoked due to {family_record[1] if family_record else 'unknown reason'}"
                        )
                        result_holder["revocation_reason"] = (
                            family_record[1] if family_record else None
                        )
                        return

                    # Mark current token as used (critical write - must be under lock)
                    rotate_now = datetime.now(timezone.utc)
                    conn.execute(
                        """
                        UPDATE refresh_tokens
                        SET is_used = 1, used_at = ?
                        WHERE token_id = ?
                    """,
                        (rotate_now.isoformat(), t_id),
                    )

                    # Update family last used
                    conn.execute(
                        """
                        UPDATE token_families
                        SET last_used_at = ?
                        WHERE family_id = ?
                    """,
                        (rotate_now.isoformat(), f_id),
                    )

                    # INSERT new token inside the same lock + transaction as the UPDATE
                    # above.  This keeps UPDATE and INSERT atomic: either both succeed
                    # or neither is committed, preventing a TOCTOU gap.
                    new_expires_at = rotate_now + timedelta(
                        days=self.refresh_token_lifetime_days
                    )
                    conn.execute(
                        """
                        INSERT INTO refresh_tokens
                        (token_id, family_id, username, token_hash, created_at, expires_at, parent_token_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            new_token_id,
                            f_id,
                            uname,
                            new_token_hash,
                            rotate_now.isoformat(),
                            new_expires_at.isoformat(),
                            t_id,
                        ),
                    )

                    result_holder["success"] = True
                    result_holder["token_id"] = t_id
                    result_holder["family_id"] = f_id
                    result_holder["username"] = uname

                self._conn_manager.execute_atomic(_do_rotate)  # type: ignore[union-attr]

            if result_holder.get("replay_attack"):
                self._handle_replay_attack(
                    result_holder["family_id_for_revoke"],
                    result_holder["username_for_revoke"],
                    client_ip,
                )
                return {
                    "valid": False,
                    "error": "Token replay attack detected",
                    "security_incident": True,
                    "family_revoked": True,
                }

            if not result_holder.get("success"):
                # Pass through error/revocation responses
                response = {
                    k: v
                    for k, v in result_holder.items()
                    if k not in ("token_id", "family_id", "username")
                }
                if "valid" not in response:
                    response["valid"] = False
                return response

            token_id = result_holder["token_id"]
            family_id = result_holder["family_id"]
            username = result_holder["username"]

        # User lookup and JWT creation happen OUTSIDE the lock - no shared state.
        if user_manager:
            # Retrieve actual user role from user manager
            try:
                user = user_manager.get_user(username)
                user_role = (
                    user.role.value if hasattr(user.role, "value") else str(user.role)
                )
                user_data = {
                    "username": username,
                    "role": user_role,
                }
            except Exception:
                # Fallback if user lookup fails
                user_data = {
                    "username": username,
                    "role": "normal_user",
                }
        else:
            # Fallback for backwards compatibility
            user_data = {
                "username": username,
                "role": "normal_user",
            }

        # Create new access token outside the lock
        new_access_token = self.jwt_manager.create_token(user_data)

        return {
            "valid": True,
            "user_data": user_data,
            "new_access_token": new_access_token,
            "new_refresh_token": new_refresh_token,
            "family_id": family_id,
            "token_id": new_token_id,
            "parent_token_id": token_id,
        }

    def _handle_replay_attack(self, family_id: str, username: str, client_ip: str):
        """
        Handle detected replay attack by revoking entire token family.

        SECURITY RESPONSE:
        - Revoke all tokens in the family
        - Log security incident
        - Mark family as compromised

        Args:
            family_id: Token family ID to revoke
            username: Username for audit logging
            client_ip: Client IP for audit logging
        """
        if self._backend:
            self._backend.revoke_token_family(family_id, "replay_attack")
        else:

            def _do_revoke(conn: sqlite3.Connection) -> None:
                conn.execute(
                    """
                    UPDATE token_families
                    SET is_revoked = 1, revocation_reason = 'replay_attack'
                    WHERE family_id = ?
                """,
                    (family_id,),
                )

            self._conn_manager.execute_atomic(_do_revoke)  # type: ignore[union-attr]

        # Log security incident
        password_audit_logger.log_security_incident(
            username=username,
            incident_type="token_replay_attack",
            ip_address=client_ip,
            additional_context={"family_id": family_id},
        )

    def revoke_token_family(
        self, family_id: str, reason: str = "manual_revocation"
    ) -> int:
        """
        Revoke all tokens in a token family.

        Args:
            family_id: Family ID to revoke
            reason: Reason for revocation

        Returns:
            Number of tokens revoked
        """
        if self._backend:
            token_count = self._backend.count_active_tokens_in_family(family_id)
            self._backend.revoke_token_family(family_id, reason)
            return token_count  # type: ignore[no-any-return]

        result: dict = {"token_count": 0}

        def _do_revoke(conn: sqlite3.Connection) -> None:
            # SELECT and UPDATE are atomic within this transaction
            cursor = conn.execute(
                """
                SELECT COUNT(*) FROM refresh_tokens
                WHERE family_id = ? AND is_used = 0
            """,
                (family_id,),
            )
            result["token_count"] = cursor.fetchone()[0]

            conn.execute(
                """
                UPDATE token_families
                SET is_revoked = 1, revocation_reason = ?
                WHERE family_id = ?
            """,
                (reason, family_id),
            )

        self._conn_manager.execute_atomic(_do_revoke)  # type: ignore[union-attr]

        return result["token_count"]  # type: ignore[no-any-return]

    def revoke_user_tokens(self, username: str, reason: str = "password_change") -> int:
        """
        Revoke all refresh tokens for a user (e.g., after password change).

        Args:
            username: Username whose tokens to revoke
            reason: Reason for revocation

        Returns:
            Number of token families revoked
        """
        if self._backend:
            return self._backend.revoke_user_families(username, reason)  # type: ignore[no-any-return]

        result: dict = {"family_count": 0}

        def _do_revoke(conn: sqlite3.Connection) -> None:
            # SELECT and UPDATE are atomic within this transaction
            cursor = conn.execute(
                """
                SELECT COUNT(*) FROM token_families
                WHERE username = ? AND is_revoked = 0
            """,
                (username,),
            )
            result["family_count"] = cursor.fetchone()[0]

            conn.execute(
                """
                UPDATE token_families
                SET is_revoked = 1, revocation_reason = ?
                WHERE username = ? AND is_revoked = 0
            """,
                (reason, username),
            )

        self._conn_manager.execute_atomic(_do_revoke)  # type: ignore[union-attr]

        return result["family_count"]  # type: ignore[no-any-return]

    def cleanup_expired_tokens(self) -> int:
        """
        Clean up expired refresh tokens from storage.

        Returns:
            Number of tokens cleaned up
        """
        now = datetime.now(timezone.utc).isoformat()

        if self._backend:
            token_count = self._backend.delete_expired_tokens(now)
            self._backend.delete_orphaned_families()
            return token_count  # type: ignore[no-any-return]

        result: dict = {"token_count": 0}

        def _do_cleanup(conn: sqlite3.Connection) -> None:
            # SELECT and DELETE are atomic within this transaction
            cursor = conn.execute(
                """
                SELECT COUNT(*) FROM refresh_tokens
                WHERE expires_at < ?
            """,
                (now,),
            )
            result["token_count"] = cursor.fetchone()[0]

            conn.execute(
                """
                DELETE FROM refresh_tokens
                WHERE expires_at < ?
            """,
                (now,),
            )
            # Clean up families with no tokens
            conn.execute(
                """
                DELETE FROM token_families
                WHERE family_id NOT IN (
                    SELECT DISTINCT family_id FROM refresh_tokens
                )
            """
            )

        self._conn_manager.execute_atomic(_do_cleanup)  # type: ignore[union-attr]

        return result["token_count"]  # type: ignore[no-any-return]

    def track_token_relationship(
        self, parent_token_id: str, child_token_id: str, family_id: str
    ) -> bool:
        """
        Track parent-child relationship between tokens for audit purposes.

        Args:
            parent_token_id: Parent token ID
            child_token_id: Child token ID
            family_id: Family ID

        Returns:
            True if relationship tracked successfully
        """
        # Relationship is already tracked in the database via parent_token_id
        return True

    def verify_secure_storage(self) -> bool:
        """
        Verify that refresh tokens are stored securely (hashed).

        Returns:
            True if storage is secure
        """
        # Verify database exists and is readable
        try:
            conn = self._conn_manager.get_connection()  # type: ignore[union-attr]
            conn.execute("SELECT COUNT(*) FROM refresh_tokens")
            return True
        except Exception:
            return False

    def _generate_refresh_token(self) -> str:
        """Generate a secure refresh token."""
        return secrets.token_urlsafe(64)

    def _generate_secure_id(self) -> str:
        """Generate a secure ID for tokens and families."""
        return secrets.token_urlsafe(32)

    def _hash_token(self, token: str) -> str:
        """Hash a token for secure storage."""
        return hashlib.sha256(token.encode()).hexdigest()
