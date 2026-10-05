"""
FastAPI authentication dependencies.

Provides dependency injection for JWT authentication and role-based access control.
"""

from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.middleware.audit_request_context import (
    AUTH_METHOD_JWT,
    AUTH_METHOD_MCP_CREDENTIAL,
    AUTH_METHOD_OAUTH_TOKEN,
    AUTH_METHOD_WEB_SESSION,
    note_auth_method,
)
from typing import Optional, TYPE_CHECKING, Dict, Any, Tuple, cast
from fastapi import Depends, HTTPException, status, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from datetime import datetime, timezone
import base64

import logging

from .jwt_manager import JWTManager, TokenExpiredError, InvalidTokenError
from .user_manager import UserManager, User, UserRole
from .api_key_manager import ApiKeyManager
from code_indexer.server.logging_utils import format_error_log

# Module-level singleton for TOTP step-up elevation (Story #923 AC5).
# Imported here so tests can swap the module attribute for fixture isolation.
from code_indexer.server.auth.elevated_session_manager import (
    elevated_session_manager,
    log_elevation_owner_mismatch,
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
# Bug #1144: API key bearer authentication — set by app_wiring.py alongside jwt_manager
api_key_manager: Optional[ApiKeyManager] = None
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


def credential_predates_account(issued_at: Any, user: User) -> bool:
    """True when a credential was issued before *user*'s account was created.

    Such a credential (JWT ``iat``, Web session ``created_at``) belongs to an
    earlier, deleted account with the same name and must not authenticate
    this one.  An account with no recorded creation instant (created before
    the instant was recorded) carries no restriction.  When the account has
    one, a credential whose issue time is missing or unreadable is refused.
    """
    created = user.account_created_at
    if created is None:
        return False
    try:
        issued = float(issued_at)
    except (TypeError, ValueError):
        return True
    return issued < created.timestamp()


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
        from code_indexer.server.auth.jwt_manager import original_auth_time

        if credential_predates_account(original_auth_time(payload), user):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token predates account",
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

    from code_indexer.server.auth.jwt_manager import original_auth_time

    # The refreshed cookie continues the same authentication: it keeps the
    # original auth_time, so the account check cannot be reset by refresh.
    new_token = jwt_manager.create_token(
        {
            "username": payload.get("username"),
            "role": payload.get("role"),
            "created_at": payload.get("created_at"),
            "auth_time": original_auth_time(payload),
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
            # The session JWT cookie is set by the Web UI login.
            note_auth_method(AUTH_METHOD_WEB_SESSION)
            return user
        # No auth method available
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials",
            headers={"WWW-Authenticate": _build_www_authenticate_header()},
        )

    token = credentials.credentials

    # Bug #1144: API key bearer dispatch — cidx_sk_ tokens handled here before JWT.
    # JWTs and OAuth opaque tokens never start with this prefix, so they fall through
    # to the OAuth / JWT paths below without any extra overhead.
    if token.startswith(ApiKeyManager.KEY_PREFIX):
        if api_key_manager is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Authentication not properly initialized",
            )
        api_user = api_key_manager.authenticate_bearer(token)
        if api_user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
                headers={"WWW-Authenticate": _build_www_authenticate_header()},
            )
        _check_non_sso_api_restriction(api_user)
        return api_user

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
                note_auth_method(AUTH_METHOD_OAUTH_TOKEN)
                return user

    # Fallback to JWT validation
    user = _validate_jwt_and_get_user(token)
    _check_non_sso_api_restriction(user)
    note_auth_method(AUTH_METHOD_JWT)
    return user


def require_permission(permission: str):
    """
    FastAPI dependency factory for requiring specific permissions.

    The returned callable depends on the same `get_current_user` dependency
    every route already uses, so a route can wire it as its sole
    `Depends(...)` for both authentication and authorization --
    `user: User = Depends(require_permission("repository:write"))`.

    Args:
        permission: Required permission string

    Returns:
        A dependency callable that resolves to the current User (via
        `Depends(get_current_user)`) and raises HTTPException(403) if that
        user lacks `permission`.
    """

    def _require_permission_dependency(
        current_user: User = Depends(get_current_user),
    ) -> User:
        if not current_user.has_permission(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions: {permission} required",
            )
        return current_user

    return _require_permission_dependency


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

    # Verify credentials using MCPCredentialManager (AC3-AC5).
    # Story #1491 AC1 (Finding B1, CRITICAL): verify_credential does a
    # bcrypt hash comparison (100-300ms pure CPU) and user_manager.get_user
    # does a synchronous user-DB read -- running either directly on the event
    # loop stalls EVERY concurrent request, not just this one. AC1 names BOTH
    # as blocking work that must leave the loop, so they share ONE
    # anyio.to_thread.run_sync boundary here rather than offloading bcrypt and
    # then immediately reading the DB back on the loop. One boundary (not two)
    # also halves the thread round-trips on the hottest MCP path.
    import anyio.to_thread

    def _verify_credential_and_load_user() -> Tuple[Optional[str], Optional[User]]:
        verified_user_id = mcp_credential_manager.verify_credential(
            client_id, client_secret
        )
        if not verified_user_id:
            return None, None
        return verified_user_id, user_manager.get_user(verified_user_id)

    # mypy: pre-commit's isolated mypy hook venv has no `anyio` stub package
    # installed (only types-PyYAML/types-requests/types-cachetools are
    # declared as additional_dependencies), so under ignore_missing_imports
    # anyio.to_thread.run_sync's return resolves to Any there -- even though
    # it resolves correctly with anyio actually installed. cast() restores
    # the real, already-known type here, taken from
    # _verify_credential_and_load_user's own declared return annotation
    # immediately above, so the narrowing below is real regardless of
    # anyio's resolution status in whichever environment mypy runs in.
    user_id, user = cast(
        Tuple[Optional[str], Optional[User]],
        await anyio.to_thread.run_sync(_verify_credential_and_load_user),
    )

    if not user_id:
        # Invalid credentials - return 401 (AC3)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": _build_www_authenticate_header()},
        )

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": _build_www_authenticate_header()},
        )

    # A separate, single-assignment name for the narrowed, definitely-non-None
    # user: mypy does not retain `if not user: raise` narrowing for a
    # variable captured by a nested function (the closure below) -- it uses
    # the variable's type across the WHOLE enclosing function instead, which
    # for `user` (assigned once, as Optional[User]) stays Optional[User]
    # even past the guard above. `authenticated_user` has exactly one
    # assignment, explicitly typed User, so the closure below never sees None.
    authenticated_user: User = user

    # v10.4.7: OAuth-MCP sessions are pre-elevated by virtue of holding the
    # credential. The credential was provisioned by a TOTP-elevated admin --
    # requiring per-call TOTP would double-step-up. Set the client_id as the
    # session key and open an elevation window so @require_mcp_elevation gates
    # fire uniformly across Bearer and OAuth paths.
    # client_id is used directly as the session key: MCP client IDs (mcp_...)
    # are already distinct from JWT JTI values (UUIDs). No construction needed.
    request.state.user_jti = client_id
    # An elevation window is valid only for the user who created it -- stash
    # the authenticated username alongside the session key.
    request.state.elevation_username = authenticated_user.username
    if elevated_session_manager is not None:
        try:
            client_ip = request.client.host if request.client else "unknown"

            # Story #1491 AC1: elevated_session_manager.create performs a
            # synchronous psycopg round-trip plus commit -- offload it too,
            # via a zero-arg sync closure (no new functools import needed).
            def _create_elevation_window() -> None:
                elevated_session_manager.create(
                    session_key=client_id,
                    username=authenticated_user.username,
                    elevated_from_ip=client_ip,
                    scope="full",
                )

            import anyio.to_thread

            await anyio.to_thread.run_sync(_create_elevation_window)
        except Exception as exc:
            # Defense-in-depth: if elevation manager is misconfigured, log and
            # let the request proceed. The decorator will surface "No active
            # elevation window" -- distinguishable from "no session key" (Gate 5).
            logger.warning(
                "v10.4.7: failed to pre-elevate oauth session for %s: %s",
                authenticated_user.username,
                exc,
                exc_info=True,
            )

    # Success - verify_credential() already updated last_used_at (AC5)
    note_auth_method(AUTH_METHOD_MCP_CREDENTIAL)
    return authenticated_user


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
                if user and not credential_predates_account(
                    session_data.issued_at, user
                ):
                    # Elevation windows opened through the Web UI (e.g.
                    # /admin/elevate) are keyed by the raw "session" cookie
                    # value -- the same value _hybrid_auth_impl's web-session
                    # branch stores as user_jti. Set it here too so
                    # _resolve_session_key() finds that same window instead
                    # of falling through to the unrelated cidx_session cookie.
                    request.state.user_jti = session_cookie
                    # An elevation window is valid only for the user who
                    # created it -- stash the authenticated username so any
                    # downstream elevation lookup binds to this identity.
                    request.state.elevation_username = user.username
                    note_auth_method(AUTH_METHOD_WEB_SESSION)
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
        resolved_user = get_current_user(request, credentials)
        # An elevation window is valid only for the user who created it --
        # stash the authenticated username so any downstream elevation
        # lookup binds to this identity, not to whatever session key gets
        # resolved from a possibly-unrelated cookie.
        request.state.elevation_username = resolved_user.username
        return resolved_user
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
        # Story #1491 AC1 (Finding B1, CRITICAL): get_current_user performs
        # a synchronous user DB read plus JWT-blacklist read (and, for
        # cookie/Bearer JWTs, jwt_manager.validate_token) -- offload it to a
        # worker thread so it never blocks the shared event loop. A zero-arg
        # lambda closure avoids introducing a new functools import.
        # HTTPException raised inside get_current_user propagates through
        # run_sync unchanged (anyio re-raises worker-thread exceptions as-is).
        import anyio.to_thread

        # mypy: pre-commit's isolated mypy hook venv has no `anyio` stub
        # package installed, so under ignore_missing_imports
        # anyio.to_thread.run_sync's return resolves to Any there. cast()
        # restores the real type here, taken from get_current_user's own
        # declared return annotation (called inside the lambda immediately
        # below), into a fresh, single-assignment name: mypy does not retain
        # narrowing for a variable captured by a nested function (the
        # `_create_oauth_bearer_elevation_window` closure further down), and
        # `user` already has an earlier assignment typed Optional[User]
        # (from get_mcp_user_from_credentials above) that a second,
        # same-named assignment cannot un-widen for that closure.
        resolved_user: User = cast(
            User,
            await anyio.to_thread.run_sync(
                lambda: get_current_user(request, credentials)
            ),
        )
        # An elevation window is valid only for the user who created it --
        # stash the authenticated username now, before any session-key
        # resolution below, so it is set even on the path where jti
        # extraction fails entirely (non-JWT, non-OAuth bearer credential).
        request.state.elevation_username = resolved_user.username
        # Extract jti for elevation key — Bearer path or cookie fallback path.
        # token is only set when Authorization: Bearer ... is present; when the
        # client authenticates via cidx_session cookie, token is None and we must
        # fall back to the cookie value so that elevation works for cookie-authed
        # /mcp clients (Issue: cookie-auth /mcp path never sets user_jti).
        _jti_token = token or request.cookies.get(CIDX_SESSION_COOKIE)
        _user_jti_set = False
        if _jti_token and jwt_manager:
            try:
                # Story #1491 AC1: JWT signature validation is CPU-bound
                # sync work -- offload it, matching the primary
                # get_current_user call above.
                import anyio.to_thread

                payload = await anyio.to_thread.run_sync(
                    jwt_manager.validate_token, _jti_token
                )
                jti = payload.get("jti")
                if jti:
                    request.state.user_jti = str(jti)
                    _user_jti_set = True
            except (TokenExpiredError, InvalidTokenError) as e:
                logger.debug(
                    "MCP jti extraction after auth: %s — elevation unavailable", e
                )
        # v10.4.8: OAuth opaque Bearer tokens (issued via /oauth/token) are NOT
        # JWTs — jwt_manager.validate_token() above silently fails for them.
        # If JWT validation didn't set user_jti but oauth_manager recognizes the
        # token, pre-elevate the session by deriving session_key from the token.
        # OAuth tokens are pre-elevated by virtue of being issued via OAuth flow
        # (already a step-up artifact). Mirrors v10.4.7's fix at the Basic-auth
        # client-credentials path. Bearer JWT login tokens keep existing
        # behavior (user_jti from jti claim, explicit /auth/elevate required).
        if not _user_jti_set and token and oauth_manager:
            try:
                oauth_result = oauth_manager.validate_token(token)
            except Exception as e:
                logger.debug("MCP OAuth token validation failed: %s", e)
                oauth_result = None
            if oauth_result:
                import hashlib

                token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
                session_key = f"oauth:{token_hash}"
                request.state.user_jti = session_key
                if elevated_session_manager is not None:
                    try:
                        client_ip = request.client.host if request.client else "unknown"

                        # Story #1491 AC1: offload the sync DB round-trip.
                        def _create_oauth_bearer_elevation_window() -> None:
                            elevated_session_manager.create(
                                session_key=session_key,
                                username=resolved_user.username,
                                elevated_from_ip=client_ip,
                                scope="full",
                            )

                        import anyio.to_thread

                        await anyio.to_thread.run_sync(
                            _create_oauth_bearer_elevation_window
                        )
                    except Exception as exc:
                        logger.warning(
                            "v10.4.8: failed to pre-elevate OAuth Bearer "
                            "session for %s: %s",
                            resolved_user.username,
                            exc,
                            exc_info=True,
                        )
        return resolved_user
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
            if credential_predates_account(session.issued_at, user):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Session predates account",
                    headers={"WWW-Authenticate": _build_www_authenticate_header()},
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
            # An elevation window is valid only for the user who created it --
            # stash the authenticated username alongside the session key so
            # every downstream elevation lookup binds to this identity.
            request.state.elevation_username = user.username
            note_auth_method(AUTH_METHOD_WEB_SESSION)
            return user
        else:
            logger.debug(f"Hybrid auth ({auth_type}): Session invalid")

    # Fall back to token-based auth only if no session cookie exists
    if not session_cookie_value and credentials:
        try:
            current_user = get_current_user(request, credentials)

            # An elevation window is valid only for the user who created it --
            # bind every downstream elevation lookup to the identity this
            # credential actually authenticated, not to whatever session key
            # gets resolved from a possibly-unrelated cookie.
            request.state.elevation_username = current_user.username

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

# Stable internal FastAPI route paths for MFA setup — not environment-specific;
# the router registers both paths unconditionally in all deployments.
# _TOTP_SETUP_URL renders via a session with role=="admin" (_get_session_username);
# _USER_TOTP_SETUP_URL renders for any authenticated session (_get_any_session_username).
_TOTP_SETUP_URL = "/admin/mfa/setup"
_USER_TOTP_SETUP_URL = "/user/mfa/setup"


def _mfa_setup_url_for_role(role: UserRole) -> str:
    """Return the MFA setup page appropriate to `role`.

    Elevation is available to every TOTP-enrolled user, not only admins, but
    the admin setup page is gated to an admin-role session -- pointing a
    non-admin caller at it would be a dead end. Only ADMIN gets the admin
    page; every other role gets the self-service one.
    """
    return _TOTP_SETUP_URL if role == UserRole.ADMIN else _USER_TOTP_SETUP_URL


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


def _totp_setup_required_exc(setup_url: str = _TOTP_SETUP_URL) -> HTTPException:
    """403 — caller has TOTP not yet set up; directs to `setup_url`."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": _ERROR_TOTP_SETUP_REQUIRED, "setup_url": setup_url},
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
    """Raise 403 totp_setup_required when the caller has no TOTP MFA enabled.

    Shared by the admin-only require_elevation() gate and the self-service
    elevation gate (any role) -- the setup_url in the raised exception is
    resolved from the CALLER's own role so a non-admin is never pointed at
    the admin-only setup page.

    Design: fail-open on non-HTTP exceptions (e.g. TOTPService DB unavailable).
    TOTPService availability must not block access entirely — the elevation
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
            raise _totp_setup_required_exc(_mfa_setup_url_for_role(user.role))
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
    username: Optional[str] = None,
) -> None:
    """Resolve session key, validate elevation window, and check scope.

    An elevation window is valid only for the user who created it: the
    lookup is bound to the authenticating user via touch_atomic_for_user(),
    never the unqualified session-key-only touch_atomic(). `username` is the
    identity this request actually authenticated as -- callers that already
    resolved it (e.g. require_elevation()'s `_check`) pass it explicitly;
    callers that only have `request` fall back to `request.state.elevation_username`,
    stashed by the auth-resolution dependency (get_current_user_web_or_api,
    _hybrid_auth_impl, get_mcp_user_from_credentials, get_current_user_for_mcp)
    at the same point it resolved that same user.

    Raises 403 elevation_required when: no session key, no resolvable
    authenticated username, window absent/expired/owned by a different user,
    or session scope is insufficient for required_scope.
    """
    session_key = _resolve_session_key(request)
    if not session_key:
        raise _elevation_required_exc()

    resolved_username = username or getattr(
        getattr(request, "state", None), "elevation_username", None
    )
    if not resolved_username:
        raise _elevation_required_exc()
    resolved_username = str(resolved_username)

    session = manager.touch_atomic_for_user(session_key, resolved_username)
    if session is None:
        log_elevation_owner_mismatch(manager, session_key, resolved_username)
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

    Kill switch: passes through (enforcement disabled) when
    elevation_enforcement_enabled is False, the config service is unavailable, or
    no elevated_session_manager is wired: the protected route runs with no
    elevation check (pinned by test_require_elevation_kill_switch_passthrough.py).
    Only ``POST /auth/elevate`` answers 503 ``elevation_enforcement_disabled``.

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
        _check_session_window(
            request, required_scope, elevated_session_manager, user.username
        )
        return user

    return _check


def _bearer_jwt_jti(
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Optional[str]:
    """Return the jti of a Bearer JWT, or None for any other credential."""
    if credentials is None or jwt_manager is None:
        return None
    try:
        payload = jwt_manager.validate_token(credentials.credentials)
    except (InvalidTokenError, TokenExpiredError):
        return None
    jti = payload.get("jti")
    return str(jti) if jti else None


def require_self_elevation(
    request: Request,
    current_user: User = Depends(get_current_user_web_or_api),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> User:
    """TOTP-elevation gate for SELF-service credential mutations (any role).

    Applies to actions on the caller's OWN account that every authenticated
    user may perform (e.g. creating or deleting their own MCP credential or
    personal API key, managing their own git-forge credential). It mirrors
    require_elevation()'s kill-switch / TOTP-setup / session-window logic,
    but resolves the caller via get_current_user_web_or_api (ANY
    authenticated user) instead of the admin-only resolver, so it never
    grants or requires a role. The MCP twins use @require_mcp_elevation(),
    which has the same role-agnostic semantics.

    The elevation window must be owned by the caller: the window lookup is
    bound to `current_user.username`. The window key is the web-session
    cookie for Web UI callers; for a Bearer JWT caller it is the token's jti,
    the same key /auth/elevate stores that caller's window under.

    With enforcement off (or no elevation manager wired) the request
    proceeds unchanged.
    """
    if not _is_elevation_enforcement_enabled() or elevated_session_manager is None:
        return current_user
    _check_totp_setup(current_user)
    if getattr(request.state, "user_jti", None) is None:
        jti = _bearer_jwt_jti(credentials)
        if jti is not None:
            request.state.user_jti = jti
    _check_session_window(
        request, "full", elevated_session_manager, current_user.username
    )
    return current_user


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
