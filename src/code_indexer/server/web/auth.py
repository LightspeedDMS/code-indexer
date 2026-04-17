"""
Web UI Session Management using itsdangerous.

Provides secure session management with signed cookies for the admin web interface.
"""

import secrets
import time
from typing import Optional, Tuple
from dataclasses import dataclass

from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from fastapi import Request, Response, HTTPException, status


# Session timeout in seconds (8 hours)
SESSION_TIMEOUT_SECONDS = 8 * 60 * 60

# Cookie settings
SESSION_COOKIE_NAME = "session"


def should_use_secure_cookies(config) -> bool:
    """Determine if secure cookies should be used based on server configuration."""
    localhost_hosts = ("127.0.0.1", "localhost", "::1")
    return config.host not in localhost_hosts


CSRF_COOKIE_NAME = "csrf_token"


@dataclass
class SessionData:
    """Data stored in a session."""

    username: str
    role: str
    csrf_token: str
    created_at: float
    session_timeout: int = SESSION_TIMEOUT_SECONDS


class SessionManager:
    """
    Manages web UI sessions using itsdangerous signed cookies.

    Features:
    - Signed session cookies using URLSafeTimedSerializer
    - CSRF token per session
    - 8-hour session timeout
    - httpOnly cookies
    """

    def __init__(self, secret_key: str, config, web_security_config=None):
        """
        Initialize session manager.

        Args:
            secret_key: Secret key for signing cookies
            config: Server configuration for cookie security settings
            web_security_config: Web security config with session timeouts
        """
        self._serializer = URLSafeTimedSerializer(secret_key)
        self._salt = "web-session"
        self._config = config
        self._web_security_config = web_security_config

    def _get_timeout_for_role(self, role: str) -> int:
        """Return session timeout in seconds based on user role."""
        if self._web_security_config is not None:
            if role == "admin":
                return int(self._web_security_config.admin_session_timeout_seconds)
            return int(self._web_security_config.web_session_timeout_seconds)
        return SESSION_TIMEOUT_SECONDS

    def create_session(
        self,
        response: Response,
        username: str,
        role: str,
    ) -> str:
        """
        Create a new session and set the session cookie.

        Args:
            response: FastAPI Response object
            username: User's username
            role: User's role

        Returns:
            CSRF token for the session
        """
        csrf_token = secrets.token_urlsafe(32)
        created_at = time.time()

        # Story #564: Use admin timeout for admin role, default for others
        session_timeout = self._get_timeout_for_role(role)

        session_data = {
            "username": username,
            "role": role,
            "csrf_token": csrf_token,
            "created_at": created_at,
            "session_timeout": session_timeout,
        }

        # Sign the session data
        signed_value = self._serializer.dumps(session_data, salt=self._salt)

        # Set the session cookie
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=signed_value,
            httponly=True,
            secure=should_use_secure_cookies(self._config),
            samesite="lax",
            max_age=session_timeout,
        )

        return csrf_token

    def get_session(self, request: Request) -> Optional[SessionData]:
        """
        Get and validate session from request cookies.

        Args:
            request: FastAPI Request object

        Returns:
            SessionData if valid session exists, None otherwise
        """
        session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
        if not session_cookie:
            return None

        try:
            # Load without max_age to read stored session_timeout
            data = self._serializer.loads(
                session_cookie,
                salt=self._salt,
            )

            # Story #564: Use stored timeout, fall back to default for old sessions
            max_age = data.get("session_timeout", SESSION_TIMEOUT_SECONDS)

            # Re-validate with the correct timeout
            data = self._serializer.loads(
                session_cookie,
                salt=self._salt,
                max_age=max_age,
            )

            return SessionData(
                username=data["username"],
                role=data["role"],
                csrf_token=data["csrf_token"],
                created_at=data["created_at"],
                session_timeout=int(max_age),
            )

        except SignatureExpired:
            # Session has expired
            return None
        except BadSignature:
            # Invalid signature - tampered cookie
            return None
        except (KeyError, TypeError):
            # Invalid session data structure
            return None

    def _should_refresh_session(self, session: SessionData) -> bool:
        """
        Return True when the session has consumed more than 50% of its lifetime.

        Mirrors the JWT sliding-window pattern so active users are never logged
        out due to session expiry while they are still working.

        Args:
            session: Validated SessionData containing created_at and session_timeout

        Returns:
            True if elapsed time > 50% of session_timeout, False otherwise
        """
        elapsed = time.time() - session.created_at
        return elapsed >= session.session_timeout * 0.5

    def get_and_refresh_session(
        self, request: Request, response: Response
    ) -> Optional[SessionData]:
        """
        Get and validate session from request cookies, re-issuing the cookie
        when the session has consumed more than 50% of its lifetime.

        This implements a sliding-window expiry: active users keep their session
        alive as long as they keep making requests, while inactive users are
        logged out after session_timeout seconds.

        CSRF token, samesite, secure, and httponly flags are all preserved on
        the refreshed cookie.

        Args:
            request: FastAPI Request object
            response: FastAPI Response object (used to set refreshed cookie)

        Returns:
            SessionData if valid session exists, None otherwise
        """
        session = self.get_session(request)
        if session is None:
            return None

        if self._should_refresh_session(session):
            session_data = {
                "username": session.username,
                "role": session.role,
                "csrf_token": session.csrf_token,
                "created_at": time.time(),
                "session_timeout": session.session_timeout,
            }
            signed_value = self._serializer.dumps(session_data, salt=self._salt)
            response.set_cookie(
                key=SESSION_COOKIE_NAME,
                value=signed_value,
                httponly=True,
                secure=should_use_secure_cookies(self._config),
                samesite="lax",
                max_age=session.session_timeout,
            )

        return session

    def is_session_expired(self, request: Request) -> bool:
        """
        Check if the session cookie exists but is expired.

        Args:
            request: FastAPI Request object

        Returns:
            True if session cookie exists but is expired, False otherwise
        """
        session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
        if not session_cookie:
            return False

        try:
            # Load without max_age to read stored session_timeout
            data = self._serializer.loads(
                session_cookie,
                salt=self._salt,
            )
            max_age = data.get("session_timeout", SESSION_TIMEOUT_SECONDS)

            # Re-validate with the correct timeout
            self._serializer.loads(
                session_cookie,
                salt=self._salt,
                max_age=max_age,
            )
            return False  # Not expired
        except SignatureExpired:
            return True  # Expired
        except BadSignature:
            return False  # Invalid, not expired

    def clear_session(self, response: Response) -> None:
        """
        Clear the session cookie.

        Args:
            response: FastAPI Response object
        """
        response.delete_cookie(
            key=SESSION_COOKIE_NAME,
            httponly=True,
            secure=should_use_secure_cookies(self._config),
            samesite="lax",
        )

    def validate_csrf_token(
        self,
        request: Request,
        submitted_token: Optional[str],
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate CSRF token against session.

        Args:
            request: FastAPI Request object
            submitted_token: CSRF token from form submission

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not submitted_token:
            return False, "CSRF token missing"

        session = self.get_session(request)
        if not session:
            return False, "No valid session"

        if not secrets.compare_digest(session.csrf_token, submitted_token):
            return False, "Invalid CSRF token"

        return True, None


# Global session manager instance - will be initialized with secret key
_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """Get the global session manager instance."""
    global _session_manager
    if _session_manager is None:
        raise RuntimeError(
            "Session manager not initialized. Call init_session_manager first."
        )
    return _session_manager


def init_session_manager(
    secret_key: str, config, web_security_config=None
) -> SessionManager:
    """
    Initialize the global session manager.

    Args:
        secret_key: Secret key for signing cookies
        config: Server configuration
        web_security_config: Web security config with session timeouts

    Returns:
        Initialized SessionManager
    """
    global _session_manager
    _session_manager = SessionManager(secret_key, config, web_security_config)
    return _session_manager


def require_admin_session(request: Request, response: Response) -> SessionData:
    """
    Dependency to require valid admin session.

    Re-issues the session cookie via sliding-window refresh when more than 50 %
    of the session lifetime has elapsed, so active admins are never logged out
    due to session expiry while they are working (Bug #726).

    Args:
        request: FastAPI Request object
        response: FastAPI Response object (used to issue refreshed cookie)

    Returns:
        SessionData for authenticated admin

    Raises:
        HTTPException: If not authenticated or not admin
    """
    session_manager = get_session_manager()
    session = session_manager.get_and_refresh_session(request, response)

    if not session:
        # Redirect to unified login with current path as redirect_to
        from urllib.parse import quote

        current_path = str(request.url.path)
        if request.url.query:
            current_path += f"?{request.url.query}"
        redirect_url = f"/login?redirect_to={quote(current_path)}"
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": redirect_url},
        )

    if session.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    return session


def require_user_session(request: Request, response: Response) -> SessionData:
    """
    Dependency to require valid user session (any authenticated user).

    Re-issues the session cookie via sliding-window refresh when more than 50 %
    of the session lifetime has elapsed, so active users are never logged out
    due to session expiry while they are working (Bug #726).

    Args:
        request: FastAPI Request object
        response: FastAPI Response object (used to issue refreshed cookie)

    Returns:
        SessionData for authenticated user

    Raises:
        HTTPException: If not authenticated
    """
    session_manager = get_session_manager()
    session = session_manager.get_and_refresh_session(request, response)

    if not session:
        # Redirect to unified login with current path as redirect_to
        from urllib.parse import quote

        current_path = str(request.url.path)
        if request.url.query:
            current_path += f"?{request.url.query}"
        redirect_url = f"/login?redirect_to={quote(current_path)}"
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": redirect_url},
        )

    return session
