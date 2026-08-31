"""
Authentication error handler for standardized security responses.

Implements secure error handling that prevents user enumeration, timing attacks,
and information leakage while maintaining comprehensive audit logging.

Following CLAUDE.md Foundation #1: NO MOCKS - Real security implementation only.
"""

import time
import hashlib
import secrets
from enum import Enum
from typing import Dict, Any, Optional

import bcrypt

# datetime imports removed - not needed for this implementation

from .audit_logger import password_audit_logger
from .timing_attack_prevention import TimingAttackPrevention

# Static bcrypt hash used ONLY for timing-equalization work when no real
# credential hash exists to compare against (e.g. non-existent user on the
# failed-auth path). Cost factor 12 matches PasswordManager's BcryptHasher
# default, so bcrypt.checkpw() against this hash takes comparable time to a
# real credential verification -- preserving the timing-attack-prevention
# contract documented in timing_attack_prevention.py:54-62. Generated once,
# offline via bcrypt.hashpw(b"dummy", bcrypt.gensalt(12)); the password
# behind it is not secret (it protects nothing) and is never used to
# authenticate anything real. Bug/Story #1494 AC4, Finding C8: replaces a
# pure-Python hashlib.sha256 loop that held the GIL for its whole duration
# -- real bcrypt.checkpw releases the GIL instead, matching the success
# (real-credential) verification path.
_DUMMY_BCRYPT_HASH = b"$2b$12$HAOEkeX.snpyOGQadhLkOeNxDcfS.JRTlxn/Uxrcq5gtPiMxYEdy."
_DUMMY_BCRYPT_PASSWORD = b"dummy-password-for-timing-equalization"

# _perform_security_work() timing-equalization tuning constants.
_SECURITY_WORK_DUMMY_DATA_SIZE_BYTES = 32
_SECURITY_WORK_HASH_ITERATIONS = 5
_SECURITY_WORK_MAX_RANDOM_DELAY_MS = 10


class AuthErrorType(Enum):
    """Standardized authentication error types for internal categorization."""

    INVALID_CREDENTIALS = "invalid_credentials"
    ACCOUNT_LOCKED = "account_locked"
    ACCOUNT_DISABLED = "account_disabled"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    GENERIC_AUTH_FAILURE = "generic_auth_failure"


class AuthError(Exception):
    """
    Authentication error with separation of public and internal information.

    Ensures that sensitive details are logged internally but never exposed
    to clients, preventing information leakage attacks.
    """

    def __init__(
        self,
        error_type: AuthErrorType,
        public_message: str,
        internal_message: str,
        user_context: str,
    ):
        """
        Initialize authentication error.

        Args:
            error_type: Internal categorization of the error
            public_message: Safe message to return to client
            internal_message: Detailed message for internal logging
            user_context: Username or user identifier for logging
        """
        self.error_type = error_type
        self.public_message = public_message
        self.internal_message = internal_message
        self.user_context = user_context

        # Exception str() should only show public message
        super().__init__(public_message)

    def __str__(self) -> str:
        """Return only public message to prevent information leakage."""
        return self.public_message


class AuthErrorHandler:
    """
    Authentication error handler with security-focused response standardization.

    Security Features:
    - Generic error messages for all authentication failures
    - Constant-time responses to prevent timing attacks
    - Dummy password hashing for non-existent users
    - Comprehensive audit logging of detailed error information
    - Standardized response format across all auth endpoints
    """

    def __init__(self, minimum_response_time_ms: int = 100):
        """
        Initialize authentication error handler.

        Args:
            minimum_response_time_ms: Minimum response time in milliseconds
        """
        self.minimum_response_time_seconds = minimum_response_time_ms / 1000.0
        self.timing_prevention = TimingAttackPrevention(minimum_response_time_ms)
        # Bug #1671: route through the SAME shared singleton startup/lifespan.py
        # wires via password_audit_logger.set_audit_service(audit_service) --
        # never construct a private PasswordChangeAuditLogger() here, since a
        # private instance never receives that wiring and every audit write it
        # attempts silently no-ops (structurally disconnected from the real
        # AuditLogService). Matches the established pattern already used by
        # refresh_token_manager.py, oauth/routes.py, inline_auth.py, and
        # mcp/handlers/admin/__init__.py.
        self.audit_logger = password_audit_logger

        # Generic messages that don't leak information
        self._generic_messages = {
            "auth_failure": "Invalid credentials",
            "registration_success": "Registration initiated. Please check your email.",
            "password_reset_success": "Password reset email sent if account exists",
        }

    def create_error_response(
        self,
        error_type: AuthErrorType,
        user_context: str,
        internal_message: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create standardized error response with timing attack prevention.

        Args:
            error_type: Type of authentication error
            user_context: Username or user identifier
            internal_message: Detailed message for audit logging
            ip_address: Client IP address for audit logging
            user_agent: Client user agent for audit logging

        Returns:
            Standardized error response dictionary
        """

        def create_response() -> Dict[str, Any]:
            # Log detailed information internally
            if hasattr(self.audit_logger, "log_authentication_failure"):
                additional_context = {}
                if ip_address:
                    additional_context["ip_address"] = ip_address
                if user_agent:
                    additional_context["user_agent"] = user_agent

                self.audit_logger.log_authentication_failure(
                    username=user_context,
                    error_type=error_type.value,
                    message=internal_message
                    or f"Authentication failed: {error_type.value}",
                    additional_context=(
                        additional_context if additional_context else None
                    ),
                )

            # Perform dummy work to normalize timing
            self._perform_security_work()

            # Return generic response that doesn't leak information
            return {
                "message": self._generic_messages["auth_failure"],
                "status_code": 401,
            }

        # Execute with constant timing
        result = self.timing_prevention.constant_time_execute(create_response)
        return result  # type: ignore[no-any-return]

    def perform_dummy_password_work(self) -> None:
        """
        Perform dummy password hashing work for timing consistency.

        When a user doesn't exist, we still need to perform password-like
        work to prevent timing-based user enumeration attacks. Story #1494
        AC4 (Finding C8): this now calls the REAL bcrypt.checkpw against a
        static dummy hash rather than faking the timing with a pure-Python
        hash loop -- real bcrypt releases the GIL during its work, matching
        the success (real-credential) verification path exactly, so a
        credential-stuffing burst no longer produces GIL-held CPU here
        while the success path stays GIL-free.
        """
        # Return value intentionally discarded -- only the GIL-releasing
        # timing work matters here, never the (meaningless) match outcome.
        _ = bcrypt.checkpw(_DUMMY_BCRYPT_PASSWORD, _DUMMY_BCRYPT_HASH)

    def create_registration_response(
        self,
        email: str,
        account_exists: bool,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create standardized registration response regardless of account existence.

        Args:
            email: Email address used for registration
            account_exists: Whether account already exists (internal use only)
            ip_address: Client IP for audit logging
            user_agent: Client user agent for audit logging

        Returns:
            Standardized registration response
        """

        def create_response() -> Dict[str, Any]:
            # Log registration attempt internally
            if hasattr(self.audit_logger, "log_registration_attempt"):
                additional_context = {"account_exists": account_exists, "email": email}
                if ip_address:
                    additional_context["ip_address"] = ip_address
                if user_agent:
                    additional_context["user_agent"] = user_agent

                self.audit_logger.log_registration_attempt(
                    email=email,
                    success=not account_exists,  # New registration is success
                    message=f"Registration attempt for {'existing' if account_exists else 'new'} account",
                    additional_context=additional_context,
                )

            # Always perform some work for timing consistency
            if account_exists:
                # If account exists, perform dummy password work
                self.perform_dummy_password_work()
            else:
                # If new account, simulate account creation work
                self._perform_security_work()

            return {
                "message": self._generic_messages["registration_success"],
                "status_code": 200,
            }

        result = self.timing_prevention.constant_time_execute(create_response)
        return result  # type: ignore[no-any-return]

    def create_password_reset_response(
        self,
        email: str,
        account_exists: bool,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create standardized password reset response regardless of account existence.

        Args:
            email: Email address for password reset
            account_exists: Whether account exists (internal use only)
            ip_address: Client IP for audit logging
            user_agent: Client user agent for audit logging

        Returns:
            Standardized password reset response
        """

        def create_response() -> Dict[str, Any]:
            # Log password reset attempt internally
            if hasattr(self.audit_logger, "log_password_reset_attempt"):
                additional_context = {"account_exists": account_exists, "email": email}
                if ip_address:
                    additional_context["ip_address"] = ip_address
                if user_agent:
                    additional_context["user_agent"] = user_agent

                self.audit_logger.log_password_reset_attempt(
                    email=email,
                    success=account_exists,  # Only existing accounts get real reset
                    message=f"Password reset attempt for {'existing' if account_exists else 'non-existent'} account",
                    additional_context=additional_context,
                )

            # Perform work for timing consistency
            self._perform_security_work()

            return {
                "message": self._generic_messages["password_reset_success"],
                "status_code": 200,
            }

        result = self.timing_prevention.constant_time_execute(create_response)
        return result  # type: ignore[no-any-return]

    def _perform_security_work(self) -> None:
        """
        Perform consistent security work to normalize timing across operations.

        This ensures all authentication operations take similar time
        regardless of the specific path taken.
        """
        # Generate some random work similar to what real auth operations do
        dummy_data = secrets.token_bytes(_SECURITY_WORK_DUMMY_DATA_SIZE_BYTES)

        # Perform hash operations similar to password validation
        for _ in range(_SECURITY_WORK_HASH_ITERATIONS):
            dummy_data = hashlib.sha256(dummy_data).digest()

        # Add a small random delay to prevent precise timing analysis
        time.sleep(
            secrets.randbelow(_SECURITY_WORK_MAX_RANDOM_DELAY_MS) / 1000.0
        )  # 0-9ms random


# Global instance for use across authentication endpoints
auth_error_handler = AuthErrorHandler()
