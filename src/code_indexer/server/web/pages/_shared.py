"""
Web UI shell helpers shared by every page module: templates, logger, CSRF and session gates.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

from code_indexer import __version__ as _cidx_version
from code_indexer.server.middleware.correlation import get_correlation_id
import logging
import secrets
from pathlib import Path
from typing import Any, Optional, cast
from urllib.parse import quote

from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from fastapi import Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from ...auth import dependencies
from ..auth import get_session_manager, SessionData
from ...services.ci_token_manager import CITokenManager
from code_indexer.server.logging_utils import format_error_log

# The logger keeps the name it had before routes.py was split, so log
# configuration and log searches by logger name are unaffected.
logger = logging.getLogger("code_indexer.server.web.routes")


def _get_token_manager() -> CITokenManager:
    """Create CITokenManager using shared factory (Bug #639, Finding 2 — Story #999)."""
    from ...services.config_service import get_config_service
    from ...services.ci_token_manager import create_token_manager

    config_service = get_config_service()
    config = config_service.get_config()
    server_dir = config_service.config_manager.server_dir
    db_path = server_dir / "data" / "cidx_server.db"
    storage_mode = config.storage_mode

    storage_backend = None
    if storage_mode == "postgres":
        if not config.postgres_dsn:
            raise ValueError(
                "_get_token_manager: storage_mode is 'postgres' but postgres_dsn is not configured"
            )
        from ...storage.postgres.connection_pool import ConnectionPool
        from ...storage.postgres.ci_tokens_backend import CITokensPostgresBackend

        pool = ConnectionPool(config.postgres_dsn, name="ci-tokens")
        storage_backend = CITokensPostgresBackend(pool)

    return create_token_manager(
        server_dir=str(server_dir),
        db_path=str(db_path),
        storage_backend=storage_backend,
        storage_mode=storage_mode,
    )


def _get_ssh_key_manager():
    """Create SSHKeyManager with SQLite backend (Story #702 migration)."""
    from ...services.config_service import get_config_service
    from ...services.ssh_key_manager import SSHKeyManager

    config_service = get_config_service()
    server_dir = config_service.config_manager.server_dir
    db_path = server_dir / "data" / "cidx_server.db"
    metadata_dir = server_dir / "data" / "ssh_keys"

    return SSHKeyManager(
        metadata_dir=metadata_dir,
        use_sqlite=True,
        db_path=db_path,
    )


# Get templates directory path
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Add enumerate to Jinja2 globals for honeycomb template (Story #712 AC1)
templates.env.globals["enumerate"] = enumerate


def _feature_offered(name: str) -> bool:
    from code_indexer.server.features.registry import feature_offered

    return feature_offered(name)


#: ``{% if feature_offered("wiki") %}``: controls of a feature add-on are
#: rendered only while it is loaded (see features.registry.feature_offered).
templates.env.globals["feature_offered"] = _feature_offered


# Helper function for server time in templates (Story #89)
def _get_server_time_for_template() -> str:
    """Get current server time for Jinja2 templates (Story #89)."""
    from datetime import datetime, timezone as tz

    current_time = datetime.now(tz.utc)
    return current_time.isoformat().replace("+00:00", "Z")


# Add server time function to Jinja2 globals for server clock (Story #89)
templates.env.globals["get_server_time"] = _get_server_time_for_template
# Cache busting: version appended to static asset URLs
templates.env.globals["static_version"] = _cidx_version


def _personal_api_keys_menu_visible(request: Any) -> bool:
    """Whether the nav shows the personal API Keys entry to this viewer.

    Creating a personal API key requires TOTP when elevation enforcement is
    on, so the entry is hidden until the viewer has MFA enabled. It is shown
    as before when enforcement is off, when the viewer's session cannot be
    resolved, or when the TOTP service is not wired (the creation gate does
    not block in that case either).
    """
    if not isinstance(request, Request):
        return True
    if not dependencies._is_elevation_enforcement_enabled():
        return True
    try:
        session = get_session_manager().get_session(request)
    except RuntimeError:
        return True
    if session is None:
        return True
    from .. import mfa_routes

    totp_service = mfa_routes.get_totp_service()
    if totp_service is None:
        return True
    return bool(totp_service.is_mfa_enabled(session.username))


templates.env.globals["personal_api_keys_menu_visible"] = (
    _personal_api_keys_menu_visible
)

# CSRF cookie name and settings
CSRF_COOKIE_NAME = "_csrf"
CSRF_MAX_AGE_SECONDS = 600  # 10 minutes


def _get_csrf_serializer() -> URLSafeTimedSerializer:
    """Get the CSRF token serializer using session manager's secret key."""
    session_manager = get_session_manager()
    # Access the secret key from the serializer
    return URLSafeTimedSerializer(session_manager._serializer.secret_key)


def _get_repo_category_service():
    """Create RepoCategoryService with database path (Story #183 helper)."""
    from ...services.config_service import get_config_service
    from ...services.repo_category_service import RepoCategoryService

    config_service = get_config_service()
    db_path = str(config_service.config_manager.server_dir / "data" / "cidx_server.db")

    return RepoCategoryService(db_path)


def generate_csrf_token() -> str:
    """Generate a new CSRF token."""
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str, path: str = "/") -> None:
    """
    Set a signed CSRF token cookie.

    Args:
        response: FastAPI Response object
        token: CSRF token to sign and store
        path: Cookie path (default: "/" for unified login)
    """
    serializer = _get_csrf_serializer()
    signed_value = serializer.dumps(token, salt="csrf-login")

    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=signed_value,
        httponly=True,
        secure=False,  # Set to True in production with HTTPS
        samesite="lax",  # Changed from strict to allow HTMX partial requests
        max_age=CSRF_MAX_AGE_SECONDS,
        path=path,  # Cookie path (default "/" for unified login)
    )


def validate_login_csrf_token(request: Request, submitted_token: Optional[str]) -> bool:
    """
    Validate CSRF token for login form using signed cookie.

    Args:
        request: FastAPI Request object
        submitted_token: CSRF token from form submission

    Returns:
        True if valid, False otherwise
    """
    logger.debug(
        "CSRF validation: submitted_token=%s, has_csrf_cookie=%s, all_cookies=%s",
        submitted_token[:20] + "..." if submitted_token else None,
        CSRF_COOKIE_NAME in request.cookies,
        list(request.cookies.keys()),
        extra={"correlation_id": get_correlation_id()},
    )

    if not submitted_token:
        logger.debug(
            "CSRF validation failed: no submitted_token",
            extra={"correlation_id": get_correlation_id()},
        )
        return False

    csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
    if not csrf_cookie:
        logger.debug(
            "CSRF validation failed: no csrf_cookie in request",
            extra={"correlation_id": get_correlation_id()},
        )
        return False

    try:
        serializer = _get_csrf_serializer()
        stored_token = serializer.loads(
            csrf_cookie,
            salt="csrf-login",
            max_age=CSRF_MAX_AGE_SECONDS,
        )
        result = secrets.compare_digest(stored_token, submitted_token)
        logger.debug(
            "CSRF validation result: %s (stored=%s, submitted=%s)",
            result,
            stored_token[:20] + "..." if stored_token else None,
            submitted_token[:20] + "..." if submitted_token else None,
            extra={"correlation_id": get_correlation_id()},
        )
        return result
    except (SignatureExpired, BadSignature) as e:
        logger.debug(
            "CSRF validation failed: %s",
            type(e).__name__,
            extra={"correlation_id": get_correlation_id()},
        )
        return False


def get_csrf_token_from_cookie(request: Request) -> Optional[str]:
    """
    Retrieve existing CSRF token from cookie.

    Args:
        request: FastAPI Request object

    Returns:
        CSRF token if valid cookie exists, None otherwise
    """
    csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
    if not csrf_cookie:
        return None

    try:
        serializer = _get_csrf_serializer()
        token = serializer.loads(
            csrf_cookie,
            salt="csrf-login",
            max_age=CSRF_MAX_AGE_SECONDS,
        )
        return cast(Optional[str], token)
    except (SignatureExpired, BadSignature):
        return None


# Placeholder routes for other admin pages
# These will redirect to login if not authenticated


def _create_login_redirect(request: Request) -> RedirectResponse:
    """Create redirect to unified login with redirect_to parameter."""
    from urllib.parse import quote

    current_path = str(request.url.path)
    if request.url.query:
        current_path += f"?{request.url.query}"

    redirect_url = f"/login?redirect_to={quote(current_path)}"
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


def _elevation_page_redirect(
    request: Request, username: str
) -> Optional[RedirectResponse]:
    """Elevation gate for full Web pages.

    With enforcement on and no valid elevation window owned by `username`,
    return a redirect to the elevation page, which returns here afterwards
    (query string kept; the elevate page's ``_sanitize_next`` bounds the
    target). Returns None when the page may render. Enforcement off passes
    through. (`quote` is the module-level urllib.parse import.)
    """
    if not dependencies._is_elevation_enforcement_enabled():
        return None
    from .. import mfa_routes

    # Error dict when no valid window exists; None when the window is valid.
    elev_err = mfa_routes._check_elevation_window(request, username)
    if elev_err is None:
        return None
    return_to = request.url.path
    if request.url.query:
        return_to = f"{return_to}?{request.url.query}"
    return RedirectResponse(
        f"{mfa_routes._ELEVATE_PAGE}?next={quote(return_to, safe='')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _require_admin_session(request: Request) -> Optional[SessionData]:
    """Check for valid admin session, return None if not authenticated."""
    try:
        session_manager = get_session_manager()
    except RuntimeError:
        # Session manager not initialized (e.g. in test environments without
        # full server setup) — treat as unauthenticated.
        return None
    session = session_manager.get_session(request)

    if not session or session.role != "admin":
        return None

    return session


def _get_users_list():
    """Get list of all users from user manager."""
    user_manager = dependencies.user_manager
    if not user_manager:
        return []
    users = user_manager.get_all_users()
    return sorted(users, key=lambda u: u.username.lower())


def _get_user_mfa_status(username: str) -> bool:
    """Check if a user has MFA enabled (Story #559).

    Uses module attribute access (not from-import) to get the runtime
    value of _totp_service, which is set after server startup.
    """
    try:
        from .. import mfa_routes

        svc = mfa_routes._totp_service
        if svc is not None:
            return bool(svc.is_mfa_enabled(username))
    except ImportError:
        pass
    return False


def _get_group_manager():
    """Get GroupAccessManager from app state."""
    from code_indexer.server import app as app_module

    manager = getattr(cast(Any, app_module.app).state, "group_manager", None)
    if manager is None:
        raise RuntimeError(
            "group_manager not initialized. "
            "Server must set app.state.group_manager during startup."
        )
    return manager


def _get_golden_repo_manager():
    """Get golden repository manager from app state."""
    from code_indexer.server import app as app_module

    manager = getattr(cast(Any, app_module.app).state, "golden_repo_manager", None)
    if manager is None:
        raise RuntimeError(
            "golden_repo_manager not initialized. "
            "Server must set app.state.golden_repo_manager during startup."
        )
    return manager


def _get_activated_repo_manager():
    """Get activated repository manager from app state.

    Bug #1670: this previously constructed a brand-new
    ActivatedRepoManager(data_dir=...) on every call, which is NEVER wired
    with set_connection_pool() -- so it always fell back to the local
    per-node JSON-file store regardless of storage_mode. In cluster
    (postgres) mode, activation/deactivation/wiki-toggle writes go through
    the properly-wired app.state.activated_repo_manager singleton (its
    _pool is set post-hoc in lifespan.py), so a fresh, pool-less instance
    here could never see those rows -- confirmed live: toggle_user_wiki_enabled
    raised "Repository '...' not found" for a repo GET /api/repos correctly
    listed as active on the same node.

    Fixed to resolve the SAME shared singleton the working paths use,
    mirroring this file's own _get_golden_repo_manager() and the
    (already-correct) _get_activated_repo_manager() in
    routers/activated_repos.py.
    """
    from code_indexer.server import app as app_module

    manager = getattr(cast(Any, app_module.app).state, "activated_repo_manager", None)
    if manager is None:
        raise RuntimeError(
            "activated_repo_manager not initialized. "
            "Server must set app.state.activated_repo_manager during startup."
        )
    return manager


def _get_background_job_manager():
    """Get the background job manager instance."""
    try:
        from ...app import background_job_manager

        return background_job_manager
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-029", f"Failed to get background job manager: {e}"
            ),
            exc_info=True,
            extra={"correlation_id": get_correlation_id()},
        )
        return None


def _detect_language_from_path(file_path: str) -> str:
    """Detect programming language from file path extension."""
    ext_to_lang = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".jsx": "javascript",
        ".java": "java",
        ".c": "c",
        ".cpp": "cpp",
        ".h": "c",
        ".hpp": "cpp",
        ".cs": "csharp",
        ".go": "go",
        ".rs": "rust",
        ".rb": "ruby",
        ".php": "php",
        ".html": "html",
        ".css": "css",
        ".sql": "sql",
        ".md": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".xml": "xml",
        ".sh": "bash",
        ".bash": "bash",
    }
    from pathlib import Path

    ext = Path(file_path).suffix.lower()
    return ext_to_lang.get(ext, "plaintext")


# =============================================================================
# User Self-Service Routes (Any Authenticated User)
# =============================================================================


def _require_authenticated_session(request: Request) -> Optional[SessionData]:
    """Check for valid authenticated session (any role), return None if not authenticated."""
    session_manager = get_session_manager()
    session = session_manager.get_session(request)

    if not session:
        return None

    return session
