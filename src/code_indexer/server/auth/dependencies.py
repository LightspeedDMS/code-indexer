"""
FastAPI authentication dependencies.

Provides dependency injection for JWT authentication and role-based access control.
"""

from code_indexer.server.middleware.correlation import get_correlation_id
from typing import Optional, TYPE_CHECKING, Dict, Any
from fastapi import Depends, HTTPException, status, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from functools import wraps
from datetime import datetime, timezone
import base64

import logging

from .jwt_manager import JWTManager, TokenExpiredError, InvalidTokenError
from .user_manager import UserManager, User
from code_indexer.server.logging_utils import format_error_log

# Module-level singleton for TOTP step-up elevation (Story #923 AC5).
# Imported here so tests can swap the module attribute for fixture isolation.
from code_indexer.server.auth.elevated_session_manager import (
    elevated_session_manager,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .oauth.oauth_manager import OAuthManager
    from .mcp_credential_manager import MCPCredentialManager
    from code_indexer.server.utils.config_manager import ServerConfig


# Global instances (will be initialized by app)
jwt_manager: Optional[JWTManager] = None
user_manager: Optional[UserManager] = None
oauth_manager: Optional["OAuthManager"] = (
    None  # Forward reference to avoid circular dependency
)
mcp_credential_manager: Optional["MCPCredentialManager"] = None
# Story #563: Server config reference for non-SSO API restriction check
server_config: Optional["ServerConfig"] = None

# Security scheme for bearer token authentication
# auto_error=False allows us to handle missing credentials manually and return 401 per MCP spec
security = HTTPBearer(auto_error=False)

# JWT cookie name — used for cookie-based auth (Web UI) and elevation-window lookup.
# Single source of truth; imported by mfa_routes and tests.
CIDX_SESSION_COOKIE = "cidx_session"


def _build_www_authenticate_header() -> str:
    """
    Build RFC 9728 compliant WWW-Authenticate header value.

    Per RFC 9728 Section 5.1, the header must include:
    - realm="mcp" - Protection space identifier
    - resource_metadata - OAuth authorization server discovery endpoint

    This enables Claude.ai and other MCP clients to discover OAuth endpoints.

    Returns:
        WWW-Authenticate header value with realm and resource_metadata parameters
    """
    # Build discovery URL from oauth_manager's issuer
    if oauth_manager:
        discovery_url = f"{oauth_manager.issuer}/.well-known/oauth-protected-resource"
        return f'Bearer realm="mcp", resource_metadata={discovery_url}'
    else:
        # Fallback to basic Bearer with realm if oauth_manager not initialized
        return 'Bearer realm="mcp"'


def _check_non_sso_api_restriction(user: User) -> None:
    """Check if non-SSO user is restricted from REST/MCP API access.

    Story #563: When restrict_non_sso_to_web_ui is enabled, non-SSO accounts
    are denied access to REST API and MCP endpoints (HTTP 403).
    SSO accounts are unaffected. Web UI routes are not affected because
    they use session-based auth via _hybrid_auth_impl(), not get_current_user().

    Args:
        user: Authenticated user to check

    Raises:
        HTTPException: 403 if user is non-SSO and restriction is enabled
    """
    if server_config is None:
        return
    web_sec = server_config.web_security_config
    if web_sec is None:
        return
    if not web_sec.restrict_non_sso_to_web_ui:
        return
    # Check if user is non-SSO (no OIDC identity)
    if user_manager and not user_manager.is_sso_user(user.username):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Non-SSO accounts are restricted to Web UI access only",
        )


def _validate_jwt_and_get_user(token: str) -> User:
    """Validate JWT token and return User object or raise HTTPException 401."""
    if not jwt_manager or not user_manager:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication not properly initialized",
        )

    try:
        payload = jwt_manager.validate_token(token)
        username = payload.get("username")

        if not username:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing username",
                headers={"WWW-Authenticate": _build_www_authenticate_header()},
            )

        # Check if token is blacklisted
        from code_indexer.server.app import is_token_blacklisted

        jti = payload.get("jti")
        if jti and is_token_blacklisted(jti):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has been revoked",
                headers={"WWW-Authenticate": _build_www_authenticate_header()},
            )

        user = user_manager.get_user(username)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
                headers={"WWW-Authenticate": _build_www_authenticate_header()},
            )

        return user

    except TokenExpiredError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": _build_www_authenticate_header()},
        )
    except InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": _build_www_authenticate_header()},
        )


def _should_refresh_token(payload: Dict[str, Any]) -> bool:
    """Check if token has passed 50% of its lifetime."""
    try:
        iat = float(payload.get("iat", 0))
        exp = float(payload.get("exp", 0))
    except Exception:
        return False

    if exp <= iat:
        return False

    now = datetime.now(timezone.utc).timestamp()
    lifetime = exp - iat
    elapsed = now - iat
    return elapsed > (lifetime * 0.5)


def _refresh_jwt_cookie(response: Response, payload: Dict[str, Any]) -> None:
    """Create new JWT with preserved claims and set as secure cookie.

    The old token's JTI is blacklisted to prevent token reuse and ensure
    that only the most recent token remains valid.
    """
    import logging

    if not jwt_manager:
        logging.getLogger(__name__).error(
            "JWT manager not initialized - cannot refresh cookie"
        )
        return

    # Blacklist old token BEFORE creating new one to prevent reuse
    old_jti = payload.get("jti")
    if old_jti:
        from code_indexer.server.app import blacklist_token

        blacklist_token(old_jti)

    new_token = jwt_manager.create_token(
        {
            "username": payload.get("username"),
            "role": payload.get("role"),
            "created_at": payload.get("created_at"),
        }
    )

    response.set_cookie(
        key=CIDX_SESSION_COOKIE,
        value=new_token,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
        max_age=jwt_manager.token_expiration_minutes * 60,
    )


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> User:
    """
    Get current authenticated user from OAuth or JWT token.

    Validates OAuth tokens first (if oauth_manager is available), then falls back to JWT.
    This allows both OAuth 2.1 tokens and legacy JWT tokens to work.

    Args:
        credentials: Bearer token from Authorization header

    Returns:
        Current User object

    Raises:
        HTTPException: If authentication fails
    """
    if not jwt_manager or not user_manager:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication not properly initialized",
        )

    # Handle missing credentials (per MCP spec RFC 9728, return 401 not 403)
    if credentials is None:
        # No Authorization header - check for JWT cookie
        token = request.cookies.get(CIDX_SESSION_COOKIE)
        if token:
            # Validate cookie JWT using same logic as Bearer
            user = _validate_jwt_and_get_user(token)
            _check_non_sso_api_restriction(user)
            return user
        # No auth method available
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials",
            headers={"WWW-Authenticate": _build_www_authenticate_header()},
        )

    token = credentials.credentials

    # Try OAuth token validation first (if oauth_manager is available)
    if oauth_manager:
        oauth_result = oauth_manager.validate_token(token)
        if oauth_result:
            # Valid OAuth token - get user
            username = oauth_result.get("user_id")
            if username:
                user = user_manager.get_user(username)  # type: ignore[assignment]
                if user is None:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="User not found",
                        headers={"WWW-Authenticate": _build_www_authenticate_header()},
                    )
                _check_non_sso_api_restriction(user)
                return user

    # Fallback to JWT validation
    user = _validate_jwt_and_get_user(token)
    _check_non_sso_api_restriction(user)
    return user


def require_permission(permission: str):
    """
    Decorator factory for requiring specific permissions.

    Args:
        permission: Required permission string

    Returns:
        Decorator function
    """

    def decorator(func):
        @wraps(func)
        def wrapper(current_user: User = Depends(get_current_user), *args, **kwargs):
            if not current_user.has_permission(permission):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Insufficient permissions: {permission} required",
                )
            return func(current_user, *args, **kwargs)

        return wrapper

    return decorator


def get_current_admin_user(current_user: User = Depends(get_current_user)) -> User:
    """
    Get current user and ensure they have admin role.

    Args:
        current_user: Current authenticated user

    Returns:
        User with admin role

    Raises:
        HTTPException: If user is not admin
    """
    if not current_user.has_permission("manage_users"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required"
        )
    return current_user


def get_current_power_user(current_user: User = Depends(get_current_user)) -> User:
    """
    Get current user and ensure they have power user or admin role.

    Args:
        current_user: Current authenticated user

    Returns:
        User with power user or admin role

    Raises:
        HTTPException: If user doesn't have sufficient permissions
    """
    if not current_user.has_permission("activate_repos"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Power user or admin access required",
        )
    return current_user


async def get_mcp_user_from_credentials(request: Request) -> Optional[User]:
    """
    Authenticate using MCP client credentials.

    Checks Basic auth header, then client_secret_post body.
    Returns User if authenticated, None if no credentials present.
    Raises HTTPException(401) if credentials present but invalid.

    Per Story #616 AC1-AC2:
    - Basic auth: Authorization header with "Basic base64(client_id:client_secret)"
    - client_secret_post: POST body with client_id and client_secret fields

    Args:
        request: FastAPI Request object

    Returns:
        User object if MCP credentials valid, None if no MCP credentials present

    Raises:
        HTTPException: 401 if credentials present but invalid
    """
    if not mcp_credential_manager or not user_manager:
        return None

    client_id: Optional[str] = None
    client_secret: Optional[str] = None

    # Check Basic auth header (AC1)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            # Decode base64 credentials
            encoded = auth_header[6:]  # Remove "Basic " prefix
            decoded = base64.b64decode(encoded).decode("utf-8")

            # Split on first colon only (client_secret may contain colons)
            if ":" in decoded:
                client_id, client_secret = decoded.split(":", 1)
        except Exception:
            # Invalid Basic auth format - return 401
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": _build_www_authenticate_header()},
            )

    # Check client_secret_post in body (AC2)
    if not client_id and request.method == "POST":
        try:
            # Check if body has already been parsed and cached
            if hasattr(request.state, "_json"):
                body = request.state._json
            else:
                # Try to parse JSON body
                body = await request.json()

            if isinstance(body, dict):
                body_client_id = body.get("client_id")
                body_client_secret = body.get("client_secret")

                if body_client_id and body_client_secret:
                    client_id = body_client_id
                    client_secret = body_client_secret
        except Exception:
            # Body not JSON, already consumed, or parse error - no client_secret_post present
            pass

    # If no MCP credentials found, return None (no error)
    if not client_id or not client_secret:
        return None

    # Verify credentials using MCPCredentialManager (AC3-AC5)
    user_id = mcp_credential_manager.verify_credential(client_id, client_secret)

    if not user_id:
        # Invalid credentials - return 401 (AC3)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": _build_www_authenticate_header()},
        )

    # Get User object
    user = user_manager.get_user(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": _build_www_authenticate_header()},
        )

    # Success - verify_credential() already updated last_used_at (AC5)
    return user


def get_current_user_web_or_api(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> User:
    """
    Get current authenticated user from web UI session OR API credentials.

    Authentication priority:
    1. Web UI session cookie ("session") via SessionManager
    2. JWT cookie ("cidx_session") or Bearer token (existing API auth)
    3. 401 Unauthorized if neither present

    This enables the same endpoint to be accessed from both:
    - Web UI (using itsdangerous session cookies)
    - API clients (using JWT tokens or Bearer auth)

    Args:
        request: FastAPI Request object
        credentials: Optional Bearer token from Authorization header

    Returns:
        Authenticated User object

    Raises:
        HTTPException: 401 if authentication fails
    """
    import logging

    logger = logging.getLogger(__name__)

    if not user_manager:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication not properly initialized",
        )

    # Priority 1: Try web UI session cookie
    session_cookie = request.cookies.get("session")
    if session_cookie:
        try:
            from code_indexer.server.web.auth import get_session_manager

            session_manager = get_session_manager()
            session_data = session_manager.get_session(request)

            if session_data:
                # Valid web session - get User object
                user = user_manager.get_user(session_data.username)
                if user:
                    return user
        except Exception as e:
            # Web session validation failed - fall through to JWT/Bearer auth
            logger.debug(
                "Web session validation failed, falling back to JWT/Bearer: %s",
                e,
                extra={"correlation_id": get_correlation_id()},
            )

    # Priority 2: Fall back to JWT/Bearer authentication
    try:
        return get_current_user(request, credentials)
    except HTTPException as exc:
        # Story #563: Let 403 (non-SSO restriction) pass through unchanged
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            raise
        # Re-raise auth failures with proper WWW-Authenticate header
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": _build_www_authenticate_header()},
        )


async def get_current_user_for_mcp(request: Request) -> User:
    """
    Get authenticated user for /mcp endpoint.

    Authentication priority per Story #616 AC6:
    1. MCP credentials (Basic auth or client_secret_post)
    2. OAuth/JWT tokens (existing authentication)
    3. 401 Unauthorized if none present

    Args:
        request: FastAPI Request object

    Returns:
        Authenticated User object

    Raises:
        HTTPException: 401 if authentication fails
    """
    # Priority 1: Try MCP credentials
    user = await get_mcp_user_from_credentials(request)
    if user:
        return user

    # Priority 2: Fall back to OAuth/JWT (existing auth)
    # Extract credentials from request for get_current_user
    credentials: Optional[HTTPAuthorizationCredentials] = None
    token: Optional[str] = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]  # Remove "Bearer " prefix
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    try:
        user = get_current_user(request, credentials)
        # Extract jti for elevation key — Bearer path or cookie fallback path.
        # token is only set when Authorization: Bearer ... is present; when the
        # client authenticates via cidx_session cookie, token is None and we must
        # fall back to the cookie value so that elevation works for cookie-authed
        # /mcp clients (Issue: cookie-auth /mcp path never sets user_jti).
        _jti_token = token or request.cookies.get(CIDX_SESSION_COOKIE)
        if _jti_token and jwt_manager:
            try:
                payload = jwt_manager.validate_token(_jti_token)
                jti = payload.get("jti")
                if jti:
                    request.state.user_jti = str(jti)
            except (TokenExpiredError, InvalidTokenError) as e:
                logger.debug(
                    "MCP jti extraction after auth: %s — elevation unavailable", e
                )
        return user
    except HTTPException as exc:
        # Story #563: Let 403 (non-SSO restriction) pass through unchanged
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            raise
        # Re-raise auth failures with proper WWW-Authenticate header
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": _build_www_authenticate_header()},
        )


def _hybrid_auth_impl(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials],
    require_admin: bool = False,
) -> User:
    """
    Internal implementation for hybrid authentication.

    Args:
        request: FastAPI Request object
        credentials: Optional bearer token credentials
        require_admin: If True, require admin role

    Returns:
        Authenticated User object

    Raises:
        HTTPException: If authentication fails
    """
    from code_indexer.server.web.auth import get_session_manager, SESSION_COOKIE_NAME
    import logging

    logger = logging.getLogger(__name__)
    auth_type = "admin" if require_admin else "user"

    # Try session-based auth first (for web UI)
    session_manager = get_session_manager()
    session_cookie_value = request.cookies.get(SESSION_COOKIE_NAME)

    logger.info(
        f"Hybrid auth ({auth_type}): session_cookie={'present' if session_cookie_value else 'absent'}"
    )

    if session_cookie_value:
        session = session_manager.get_session(request)
        logger.info(
            f"Hybrid auth ({auth_type}): session={'valid' if session else 'invalid'}, "
            f"username={session.username if session else None}, "
            f"role={session.role if session else None}"
        )

        # Bug #67 fix: Always fetch user from database to get current role
        # Session role may be stale if admin changed it after login
        if session:
            if not user_manager:
                logger.error(
                    format_error_log(
                        "AUTH-GENERAL-001",
                        f"Hybrid auth ({auth_type}): user_manager not initialized",
                    )
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="User manager not initialized",
                )

            # Fetch user from database to get CURRENT role (not cached session role)
            user = user_manager.get_user(session.username)
            logger.debug(
                f"Hybrid auth ({auth_type}): user lookup for {session.username}: {user is not None}"
            )

            if not user:
                # Session is valid but user not found - user was deleted
                logger.error(
                    format_error_log(
                        "AUTH-GENERAL-002",
                        f"Hybrid auth ({auth_type}): User {session.username} not found in database",
                    )
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"User '{session.username}' not found in user database",
                )

            # Check admin requirement using DATABASE role, not session role
            if require_admin and not user.has_permission("manage_users"):
                logger.debug(
                    f"Hybrid auth ({auth_type}): Session valid but user lacks admin permission "
                    f"(session_role={session.role}, db_role={user.role.value})"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Admin access required",
                )

            logger.info(
                f"Hybrid auth ({auth_type}): Session auth SUCCESS for {session.username}"
            )
            request.state.user_jti = (
                session_cookie_value  # enables elevation session key resolution
            )
            return user
        else:
            logger.debug(f"Hybrid auth ({auth_type}): Session invalid")

    # Fall back to token-based auth only if no session cookie exists
    if not session_cookie_value and credentials:
        try:
            current_user = get_current_user(request, credentials)

            # Set user_jti for elevation session key resolution.
            # Session-cookie path sets this at the session success block above;
            # Bearer token path must set it here from the JWT jti claim.
            if jwt_manager:
                try:
                    payload = jwt_manager.validate_token(credentials.credentials)
                    jti = payload.get("jti")
                    if jti:
                        request.state.user_jti = jti
                except (InvalidTokenError, TokenExpiredError) as exc:
                    # Non-JWT credentials (OAuth, opaque tokens) and expired tokens
                    # have no extractable jti; elevation simply won't be available.
                    logger.debug(
                        f"Hybrid auth ({auth_type}): jti extraction skipped — {exc}"
                    )

            # Check admin requirement for token auth
            if require_admin and not current_user.has_permission("manage_users"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Admin access required",
                )

            logger.info(
                f"Hybrid auth ({auth_type}): Token auth SUCCESS for {current_user.username}"
            )
            return current_user
        except HTTPException:
            raise

    # No valid authentication found
    logger.warning(
        format_error_log(
            "AUTH-GENERAL-003",
            f"Hybrid auth ({auth_type}): No valid authentication found",
        )
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": _build_www_authenticate_header()},
    )


def get_current_user_hybrid(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> User:
    """
    Get current user supporting both session-based and token-based authentication.

    This function tries session-based authentication first (for web UI),
    then falls back to token-based authentication (for API clients).

    Args:
        request: FastAPI Request object
        credentials: Optional bearer token credentials

    Returns:
        Authenticated User object

    Raises:
        HTTPException: If authentication fails
    """
    return _hybrid_auth_impl(request, credentials, require_admin=False)


def get_current_admin_user_hybrid(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> User:
    """
    Get current admin user supporting both session-based and token-based authentication.

    This dependency tries session-based auth first (for web UI), then falls back to
    token-based auth (for API clients).

    Args:
        request: FastAPI request object
        credentials: Optional bearer token credentials

    Returns:
        User with admin role

    Raises:
        HTTPException: If not authenticated or not admin
    """
    return _hybrid_auth_impl(request, credentials, require_admin=True)


# Three canonical error codes per Story #923 AC5 and Codex review.
# _ERROR_ELEVATION_FAILED is reserved for the /auth/elevate endpoint (not used here).
_ERROR_TOTP_SETUP_REQUIRED = "totp_setup_required"
_ERROR_ELEVATION_REQUIRED = "elevation_required"
_ERROR_ELEVATION_FAILED = "elevation_failed"  # reserved; used by /auth/elevate

# Stable internal FastAPI route path for MFA setup — not environment-specific;
# the router registers this path unconditionally in all deployments.
_TOTP_SETUP_URL = "/admin/mfa/setup"

# Scope hierarchy: rank 0 = broadest ("full"), rank 1 = narrower ("totp_repair").
# A session satisfies required_scope R when session_rank <= required_rank.
# Scopes absent from this dict receive len(_SCOPE_RANK) = least-privileged rank.
_SCOPE_RANK: Dict[str, int] = {"full": 0, "totp_repair": 1}

# ---------------------------------------------------------------------------
# Exception builder helpers — one per error kind, no inline construction.
# ---------------------------------------------------------------------------


def _elevation_required_exc(message: Optional[str] = None) -> HTTPException:
    """403 — no active elevation window, or scope insufficient."""
    detail: Dict[str, Any] = {"error": _ERROR_ELEVATION_REQUIRED}
    if message:
        detail["message"] = message
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _totp_setup_required_exc() -> HTTPException:
    """403 — admin has TOTP not yet set up; directs to setup_url."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": _ERROR_TOTP_SETUP_REQUIRED, "setup_url": _TOTP_SETUP_URL},
    )


# ---------------------------------------------------------------------------
# Focused single-responsibility helpers called from _check.
# ---------------------------------------------------------------------------


def _is_elevation_enforcement_enabled() -> bool:
    """Read kill switch from runtime config (Story #923 AC5, Codex M12).

    Returns False (fails closed) when config service raises, so the 503
    kill-switch path is taken rather than silently bypassing enforcement.
    """
    try:
        from code_indexer.server.services.config_service import get_config_service

        config = get_config_service().get_config()
        return bool(getattr(config, "elevation_enforcement_enabled", False))
    except Exception:
        logger.warning(
            "require_elevation: could not read config; treating elevation as disabled",
            exc_info=True,
        )
        return False


def _check_totp_setup(user: User) -> None:
    """Raise 403 totp_setup_required when admin has no TOTP MFA enabled.

    Design: fail-open on non-HTTP exceptions (e.g. TOTPService DB unavailable).
    TOTPService availability must not block admin access entirely — the elevation
    window check that follows is the authoritative gate (Story #923 AC5 spec).
    Logs a warning so operators can detect persistent TOTPService failures.
    """
    try:
        from code_indexer.server.web.mfa_routes import get_totp_service

        totp_service = get_totp_service()
        if totp_service is None:
            # Lifespan didn't wire totp service (test/dev). Fail-open per AC5
            # design: TOTPService availability must not block admin access.
            return
        if not totp_service.is_mfa_enabled(user.username):
            raise _totp_setup_required_exc()
    except HTTPException:
        raise
    except Exception:
        logger.warning(
            "require_elevation: TOTP setup check failed for %s; skipping setup gate",
            user.username,
            exc_info=True,
        )


def _resolve_session_key(request: Request) -> Optional[str]:
    """Return JTI from request state (Bearer) or cidx_session cookie (Web UI)."""
    jti = getattr(getattr(request, "state", None), "user_jti", None)
    if jti:
        return str(jti)
    cookie = request.cookies.get(CIDX_SESSION_COOKIE)
    return str(cookie) if cookie is not None else None


def _check_scope(session_scope: Optional[str], required_scope: str) -> None:
    """Raise 403 elevation_required when session scope is insufficient.

    Unknown/missing session scopes receive least-privileged rank — no fallback
    to "full" to avoid incorrectly granting broad access on missing metadata.
    """
    session_rank = _SCOPE_RANK.get(session_scope or "", len(_SCOPE_RANK))
    required_rank = _SCOPE_RANK[required_scope]
    if session_rank > required_rank:
        raise _elevation_required_exc(
            f"Scope {required_scope!r} required; current window is scope={session_scope!r}."
        )


def _check_session_window(
    request: Request,
    required_scope: str,
    manager: Any,
) -> None:
    """Resolve session key, validate elevation window, and check scope.

    Raises 403 elevation_required when: no session key, window absent/expired,
    or session scope is insufficient for required_scope.
    """
    session_key = _resolve_session_key(request)
    if not session_key:
        raise _elevation_required_exc()

    session = manager.touch_atomic(session_key)
    if session is None:
        raise _elevation_required_exc()

    _check_scope(getattr(session, "scope", None), required_scope)


def require_elevation(required_scope: str = "full"):
    """Build a FastAPI dependency that enforces an active TOTP elevation window.

    Story #923 AC5. Chains after get_current_admin_user_hybrid (admin gate
    already enforced). Returns a callable dependency so callers can specify
    required_scope: 'full' (default) for sensitive admin ops; 'totp_repair' for
    TOTP-fix endpoints accessible via recovery codes.

    Scope hierarchy (broadest first): full (rank 0) > totp_repair (rank 1).
    A session with scope S satisfies required_scope R when rank(S) <= rank(R).
    Unknown/missing session scopes receive the highest rank (least privileged).

    Three canonical error codes:
      - totp_setup_required (403): admin has no TOTP MFA enabled -> setup_url body
      - elevation_required  (403): no active elevation window or scope insufficient
      - elevation_failed    (401): reserved for /auth/elevate endpoint (not raised here)

    Kill switch: returns 503 (per Codex M4/M12) when elevation_enforcement_enabled
    is False or config service is unavailable.

    Args:
        required_scope: One of "full" or "totp_repair". ValueError on unknown value
            (programmer error at call site — not a runtime auth failure).
    """
    if required_scope not in _SCOPE_RANK:
        raise ValueError(
            f"required_scope must be one of {sorted(_SCOPE_RANK)}, got {required_scope!r}"
        )

    def _check(
        request: Request,
        user: User = Depends(get_current_admin_user_hybrid),
    ) -> User:
        # Kill switch: when elevation enforcement is administratively disabled OR
        # the elevated_session_manager singleton was never initialised (optional
        # subsystem on this deployment), bypass all elevation checks and let the
        # request proceed.  The protected endpoint runs as if no elevation gate
        # existed.  See test_require_elevation_kill_switch_passthrough.py for the
        # corrected contract.  When enforcement is ON and the manager is present,
        # normal TOTP-setup + session-window checks apply.
        if not _is_elevation_enforcement_enabled() or elevated_session_manager is None:
            return user
        _check_totp_setup(user)
        _check_session_window(request, required_scope, elevated_session_manager)
        return user

    return _check


def require_localhost(request: Request) -> None:
    """Reject requests not originating from loopback (Story #924).

    Story #924 -- maintenance mode enter/exit endpoints are auto-updater
    driven (system processes, not humans). Restrict to loopback so:
      - The local auto-updater (running as systemd service) can call them
      - Network-side admins cannot DoS the server by toggling maintenance
      - No TOTP elevation needed (auto-updater can't satisfy TOTP prompt)

    Loopback whitelist (validated via ipaddress module):
      127.0.0.0/8 (IPv4 loopback -- is_loopback is True)
      ::1 (IPv6 loopback -- is_loopback is True)
      ::ffff:127.x.x.x (IPv4-mapped IPv6 loopback -- mapped IPv4 is_loopback)

    For reverse-proxied deployments, the proxy must NOT pass X-Forwarded-For
    or similar headers for these endpoints -- the request.client.host check
    here only sees the IMMEDIATE peer (which is the proxy, not the original
    caller). If a proxy fronts these endpoints in production, this control
    is degraded to "anyone the proxy reaches" -- operator must lock down
    the proxy's exposure.

    Raises:
        HTTPException 403: when request.client.host is not a valid loopback address
    """
    import ipaddress

    if request.client is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Localhost-only endpoint",
        )
    host = request.client.host
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Localhost-only endpoint",
        )
    # IPv4-mapped IPv6 addresses (::ffff:127.x.x.x) report is_loopback=False in
    # Python's ipaddress module, so check the mapped IPv4 address explicitly.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        is_local = addr.ipv4_mapped.is_loopback
    else:
        is_local = addr.is_loopback
    if not is_local:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Localhost-only endpoint; rejected request from {host}",
        )
