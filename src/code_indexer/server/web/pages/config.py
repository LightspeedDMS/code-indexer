"""
Admin configuration pages.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

from code_indexer.server.middleware.correlation import get_correlation_id
import asyncio
import functools
import anyio
import anyio.from_thread
import anyio.to_thread
from typing import Any, Dict, List, Optional, Tuple, cast
from fastapi import APIRouter, Depends, Request, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from ...auth import dependencies
from ..auth import SessionData
from ...services.ci_token_manager import TokenData, TokenValidationError
from ...services.config_service import BootstrapFileNotWritten
from code_indexer.server.logging_utils import format_error_log

from ._shared import (
    _create_login_redirect,
    _get_ssh_key_manager,
    _get_token_manager,
    _require_admin_session,
    generate_csrf_token,
    get_csrf_token_from_cookie,
    logger,
    set_csrf_cookie,
    templates,
    validate_login_csrf_token,
)
from .config_values import (
    RESTART_REQUIRED_FIELDS,
    _VALID_CONFIG_SECTIONS,
    _get_current_config,
    _get_qec_total_entries,
    _validate_config_section,
)

web_router = APIRouter()


def _ci_token_display(
    token_data: Optional[TokenData],
) -> Optional[Dict[str, Optional[str]]]:
    """What the Config page may show of a stored CI token: the masked token
    (at most its last 4 characters) and the base URL. The raw token never
    enters the template context. None when no token is stored."""
    if token_data is None:
        return None
    from code_indexer.utils.credential_redaction import mask_stored_secret

    return {
        "masked_token": mask_stored_secret(token_data.token),
        "base_url": token_data.base_url,
    }


class _OidcReloadFailed(Exception):
    """The OIDC test-reload rejected a candidate configuration."""


def _prepare_oidc_candidate_from_worker_thread(
    candidate, prepared: Dict[str, Any]
) -> None:
    """``before_publish`` for an OIDC section change.

    Runs on the worker thread of the audited change: builds and initializes
    OIDC managers for the CANDIDATE configuration WITHOUT making them live,
    and keeps them in *prepared*.  A failure raises :class:`_OidcReloadFailed`,
    so nothing is published.  The caller swaps them in with
    :func:`_install_oidc_managers` only after persistence.
    """
    try:
        prepared["managers"] = _prepare_oidc_managers(candidate)
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-047",
                f"Failed to reload OIDC configuration: {e}",
            ),
            exc_info=True,
        )
        raise _OidcReloadFailed(str(e)) from e
    logger.info(
        "OIDC configuration validated and reloaded successfully",
        extra={"correlation_id": get_correlation_id()},
    )


def _install_oidc_managers(prepared: Dict[str, Any]) -> None:
    """Make the managers prepared for a PUBLISHED OIDC change live.

    A no-op when nothing was prepared (not an OIDC section change).
    """
    from ...auth.oidc import routes as oidc_routes

    if "managers" not in prepared:
        return
    oidc_routes.oidc_manager, oidc_routes.state_manager = prepared["managers"]


def _prepare_oidc_managers(config) -> Tuple[Any, Any]:
    """Build (oidc_manager, state_manager) for *config* WITHOUT making them live.

    Runs on a worker thread (the ``before_publish`` step).  Returns
    (None, None) when OIDC is disabled in *config* (installing that clears
    the live managers).  Never touches the live module globals.
    """
    oidc_config = config.oidc_provider_config
    if oidc_config is None or not oidc_config.enabled:
        logger.info(
            "OIDC is disabled, the live OIDC managers will be cleared",
            extra={"correlation_id": get_correlation_id()},
        )
        return None, None
    state_manager = _build_oidc_state_manager()
    oidc_manager = anyio.from_thread.run(_initialized_oidc_manager, oidc_config)
    logger.info(
        f"OIDC managers prepared for provider: {oidc_config.provider_name} (will initialize on next login)",
        extra={"correlation_id": get_correlation_id()},
    )
    return oidc_manager, state_manager


def _build_oidc_state_manager() -> Any:
    """A StateManager wired exactly as startup wires it; call OFF the event loop.

    Construction opens its SQLite store and ensures its schema.  In cluster
    mode the shared PostgreSQL pool (critical pool first, else the general
    pool -- the startup rule) is attached so SSO state is shared across nodes.
    """
    from ... import app as app_module
    from ...auth.oidc import state_manager as state_module

    state_manager = state_module.StateManager()
    registry = getattr(cast(Any, app_module.app).state, "backend_registry", None)
    pool = (
        (registry.critical_connection_pool or registry.connection_pool)
        if registry is not None
        else None
    )
    if pool is not None:
        state_manager.set_connection_pool(pool)
    return state_manager


async def _initialized_oidc_manager(oidc_config) -> Any:
    """Construct and initialize an OIDCManager for *oidc_config* (event loop)."""
    from ... import app as app_module
    from ...auth.oidc.oidc_manager import OIDCManager

    # Reuse existing user_manager and jwt_manager from module level
    logger.info(
        f"Creating new OIDC manager with config: email_claim={oidc_config.email_claim}, username_claim={oidc_config.username_claim}",
        extra={"correlation_id": get_correlation_id()},
    )
    oidc_manager = OIDCManager(
        config=oidc_config,
        user_manager=app_module.user_manager,
        jwt_manager=app_module.jwt_manager,
    )

    # Initialize OIDC database schema (no network calls)
    # Provider metadata will be discovered lazily on next SSO login attempt
    await oidc_manager.initialize()

    # Inject GroupAccessManager into OIDCManager for SSO provisioning (Story #708)
    if (
        hasattr(cast(Any, app_module.app).state, "group_manager")
        and cast(Any, app_module.app).state.group_manager
    ):
        oidc_manager.group_manager = app_module.app.state.group_manager  # type: ignore[attr-defined]
        logger.info(
            "GroupAccessManager injected into reloaded OIDCManager for SSO auto-provisioning",
            extra={"correlation_id": get_correlation_id()},
        )
    return oidc_manager


def _create_config_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
    validation_errors: Optional[dict] = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Create config page response with all necessary context.

    Issue #1554: ``status_code`` defaults to 200 (byte-identical to every
    pre-existing caller). Callers rendering a REJECTED write must pass the
    correct 4xx/5xx so the HTTP status code alone -- not just the rendered
    HTML body -- tells a caller whether the write succeeded.
    """
    csrf_token = generate_csrf_token()
    config = _get_current_config()
    # SIEM delivery read-only status: this process's in-memory view (no I/O).
    from ...services.siem_delivery.config_view import status_block as _siem_status

    config["siem_delivery_status"] = _siem_status(
        getattr(request.app.state, "siem_delivery_scheduler", None),
        getattr(request.app.state, "siem_delivery_startup_error", None),
    )

    # Only the masked display form reaches the template. Reading the tokens
    # first marks an undecryptable one, so the status below is current.
    token_manager = _get_token_manager()
    github_token_data = _ci_token_display(token_manager.get_token("github"))
    gitlab_token_data = _ci_token_display(token_manager.get_token("gitlab"))
    api_keys_status = token_manager.list_tokens()

    response = templates.TemplateResponse(
        request,
        "config.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "config",
            "show_nav": True,
            "csrf_token": csrf_token,
            "config": config,
            "success_message": success_message,
            "error_message": error_message,
            "validation_errors": validation_errors or {},
            "api_keys_status": api_keys_status,
            "github_token_data": github_token_data,
            "gitlab_token_data": gitlab_token_data,
            "restart_required_fields": RESTART_REQUIRED_FIELDS,
        },
        status_code=status_code,
    )

    set_csrf_cookie(response, csrf_token)
    return response


@web_router.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    """Configuration management page - view and edit CIDX configuration."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_config_page_response(request, session)


# NOTE: This specific route MUST come BEFORE /config/{section} to avoid being
# caught by the parameterized route. FastAPI matches routes in order of definition.
@web_router.post(
    "/config/reset",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def reset_config(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """Reset configuration to defaults."""
    from ...services.config_service import get_config_service

    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_config_page_response(
            request,
            session,
            error_message="Invalid CSRF token",
            status_code=status.HTTP_403_FORBIDDEN,
        )

    # Reset to defaults using ConfigService (one audited change)
    try:
        get_config_service().reset_to_defaults_audited(actor=session.username)

        return _create_config_page_response(
            request,
            session,
            success_message=(
                "Runtime settings reset to defaults. Bootstrap settings, the "
                "bind address, port, worker count, log level, stored keys, "
                "security settings and deployment identity were kept."
            ),
        )
    except Exception as e:
        logger.error(
            format_error_log("STORE-GENERAL-045", f"Failed to reset config: {e}"),
            extra={"correlation_id": get_correlation_id()},
        )
        return _create_config_page_response(
            request,
            session,
            error_message=f"Failed to reset configuration: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


# NOTE: This specific route MUST come BEFORE /config/{section} to avoid being
# caught by the parameterized route. FastAPI matches routes in order of definition.
@web_router.post(
    "/config/langfuse_pull",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
async def update_langfuse_pull_config(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """Update Langfuse Trace Pull configuration (Story #164)."""
    from ...services.config_service import (
        LangfusePullProjectsInvalid,
        get_config_service,
    )

    session = await asyncio.to_thread(_require_admin_session, request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    if not validate_login_csrf_token(request, csrf_token):
        return _create_config_page_response(
            request,
            session,
            error_message="Invalid CSRF token",
            status_code=status.HTTP_403_FORBIDDEN,
        )

    form_data = await request.form()
    config_service = get_config_service()

    try:
        # Scalar settings plus the projects JSON, as ONE audited change.
        updates: List[Tuple[str, str, Any]] = [
            ("langfuse", "pull_enabled", form_data.get("pull_enabled", "false")),
            (
                "langfuse",
                "pull_host",
                form_data.get("pull_host", "https://cloud.langfuse.com"),
            ),
            (
                "langfuse",
                "pull_sync_interval_seconds",
                form_data.get("pull_sync_interval_seconds", "300"),
            ),
            (
                "langfuse",
                "pull_trace_age_days",
                form_data.get("pull_trace_age_days", "30"),
            ),
            (
                "langfuse",
                "pull_max_concurrent_observations",
                form_data.get("pull_max_concurrent_observations", "5"),
            ),
        ]
        projects_json = form_data.get("pull_projects", "[]")
        if projects_json:
            updates.append(("langfuse", "pull_projects", projects_json))
        try:
            await asyncio.to_thread(
                functools.partial(
                    config_service.update_settings_audited,
                    updates,
                    actor=session.username,
                )
            )
        except LangfusePullProjectsInvalid as rejected:
            # The project list was refused (a project with no secret key or
            # a duplicate public key): nothing was published.
            logger.warning(
                f"Langfuse pull config save rejected: {rejected}",
                extra={"correlation_id": get_correlation_id()},
            )
            return _create_config_page_response(
                request,
                session,
                error_message=f"Configuration not saved: {rejected}",
                validation_errors={"langfuse_pull": str(rejected)},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        return _create_config_page_response(
            request,
            session,
            success_message="Langfuse Trace Pull configuration saved",
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-046", f"Failed to update Langfuse pull config: {e}"
            ),
            extra={"correlation_id": get_correlation_id()},
        )
        return _create_config_page_response(
            request,
            session,
            error_message=f"Failed to save configuration: {str(e)}",
            validation_errors={"langfuse_pull": str(e)},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@web_router.post(
    "/config/cidx_meta_backup",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
async def update_cidx_meta_backup_config(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """Update cidx-meta backup config with SSH validation and bootstrap."""
    from ...services.cidx_meta_backup.bootstrap import CidxMetaBackupBootstrap
    from ...services.config_service import get_config_service
    from code_indexer.utils.git_remote_url import git_remote_host

    session = await asyncio.to_thread(_require_admin_session, request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    if not validate_login_csrf_token(request, csrf_token):
        return _create_config_page_response(
            request,
            session,
            error_message="Invalid CSRF token",
            status_code=status.HTTP_403_FORBIDDEN,
        )

    form_data = await request.form()
    enabled = str(form_data.get("enabled", "false")).lower() == "true"
    remote_url = str(form_data.get("remote_url", "")).strip()

    if remote_url and not (
        remote_url.startswith("file://")
        or remote_url.startswith("https://")
        or remote_url.startswith("http://")
    ):
        hostname = git_remote_host(remote_url)
        key_list = _get_ssh_key_manager().list_keys()
        if hostname and not any(hostname in key.hosts for key in key_list.managed):
            return _create_config_page_response(
                request,
                session,
                error_message=f"No SSH key configured for {hostname}",
                validation_errors={
                    "cidx_meta_backup": f"No SSH key configured for {hostname}"
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    def _save_and_bootstrap() -> None:
        config_service = get_config_service()
        config_service.update_settings_audited(
            [
                ("cidx_meta_backup", "enabled", enabled),
                ("cidx_meta_backup", "remote_url", remote_url),
            ],
            actor=session.username,
        )
        if remote_url:
            from ...services.cidx_meta_backup import get_cidx_meta_path

            repo_root = get_cidx_meta_path(config_service.config_manager.server_dir)
            CidxMetaBackupBootstrap().bootstrap(str(repo_root), remote_url)

    try:
        # One audited change plus the bootstrap, off the event loop.
        await asyncio.to_thread(_save_and_bootstrap)

        return _create_config_page_response(
            request,
            session,
            success_message="cidx-meta backup configuration saved",
        )
    except Exception as e:
        return _create_config_page_response(
            request,
            session,
            error_message=f"Failed to save configuration: {str(e)}",
            validation_errors={"cidx_meta_backup": str(e)},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


# NOTE: This specific route MUST come BEFORE /config/{section} to avoid being
# caught by the parameterized route. FastAPI matches routes in order of definition.
@web_router.post(
    "/config/query-embedding-cache/clear",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def clear_query_embedding_cache_table(
    request: Request,
):
    """Truncate the query_embedding_cache table and return the updated count display (Story #1156).

    Admin-only action: deletes all rows from the persisted query embedding cache
    via the active backend (SQLite or PostgreSQL) and resets the in-process count
    memo to 0.  Returns an HTML fragment suitable for HTMX outerHTML swap of
    #display-query_embedding_cache so the count refreshes immediately to 0.

    Clearing an already-empty cache is a no-op success (AC7).
    Works in both solo (SQLite) and cluster (PostgreSQL) modes (AC6).
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # HTMX sends CSRF token as X-CSRF-Token header (consistent with other HTMX
    # endpoints in this file, e.g. the GitHub/GitLab API key delete handlers).
    csrf_from_header = request.headers.get("X-CSRF-Token")
    if not validate_login_csrf_token(request, csrf_from_header):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    try:
        from ...services.governed_call import get_query_embedding_cache

        cache = get_query_embedding_cache()
        if cache is not None:
            cache.clear_all()
        # Re-read the live count (should be 0 after clear_all).
        new_count = _get_qec_total_entries()
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-046", f"Failed to clear query embedding cache: {e}"
            ),
            extra={"correlation_id": get_correlation_id()},
        )
        return HTMLResponse(
            content=f"Failed to clear query embedding cache: {e}", status_code=500
        )

    # Return an HTML fragment that replaces #display-query_embedding_cache.
    # This is the minimal display div so HTMX outerHTML swap refreshes the count.
    config = _get_current_config()
    # Inject the freshly cleared count so the template sees 0 immediately.
    config["query_embedding_cache"]["total_cached_entries"] = new_count
    csrf_token_new = generate_csrf_token()
    return templates.TemplateResponse(
        request,
        "partials/qec_display.html",
        {
            "request": request,
            "config": config,
            "csrf_token": csrf_token_new,
        },
    )


@web_router.post(
    "/config/{section}",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
async def update_config_section(
    request: Request,
    section: str,
    csrf_token: Optional[str] = Form(None),
):
    """Update configuration for a specific section."""
    from ...services.config_service import get_config_service

    session = await asyncio.to_thread(_require_admin_session, request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_config_page_response(
            request,
            session,
            error_message="Invalid CSRF token",
            status_code=status.HTTP_403_FORBIDDEN,
        )

    # Validate section
    if section not in _VALID_CONFIG_SECTIONS:
        return _create_config_page_response(
            request,
            session,
            error_message=f"Invalid section: {section}",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Get form data
    form_data = await request.form()
    data = {k: v for k, v in form_data.items() if k != "csrf_token"}

    # The server host is stripped ONCE here, so the change guardrail, the
    # validation and the stored value all see the same normalised string.
    _persisted_host: Optional[str] = None
    if section == "server" and data.get("host") is not None:
        data["host"] = str(data["host"]).strip()
        _persisted_host = get_config_service().get_config().host

    # Validate configuration
    error = _validate_config_section(section, data, persisted_host=_persisted_host)
    if error is None and section == "siem_delivery":
        # The destination allowlist depends on THIS process's fault-injection
        # gate (a loopback harness endpoint is accepted only behind it).
        from ...services.siem_delivery.config_view import (
            validate_form as _siem_validate,
        )

        _siem_current = get_config_service().get_config().siem_delivery_config
        assert _siem_current is not None  # ServerConfig.__post_init__
        error = _siem_validate(
            _siem_current,
            data,
            harness_active=getattr(request.app.state, "fault_injection_service", None)
            is not None,
        )
    if error:
        return _create_config_page_response(
            request,
            session,
            error_message=error,
            validation_errors={section: error},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Save configuration using ConfigService
    try:
        config_service = get_config_service()

        if section == "totp_elevation":
            # Bug #943 Fix #1/#2/#3: atomic batch path — validate final tuple,
            # rollback on save failure, hot-reload live session manager.
            from code_indexer.server.auth.elevated_session_manager import (
                elevated_session_manager as _esm,
            )
            from code_indexer.server.services.config_service import (
                _TOTP_TRUTHY as _TOTP_TRUTHY_SET,
            )

            _raw_enabled = data.get("elevation_enforcement_enabled")
            _raw_idle = data.get("elevation_idle_timeout_seconds")
            _raw_max_age = data.get("elevation_max_age_seconds")

            # All 3 fields are required. The template uses a hidden fallback
            # input for elevation_enforcement_enabled so it is always submitted.
            if _raw_enabled is None or _raw_idle is None or _raw_max_age is None:
                return _create_config_page_response(
                    request,
                    session,
                    error_message=(
                        "Missing required field: elevation_enforcement_enabled, "
                        "elevation_idle_timeout_seconds, or elevation_max_age_seconds"
                    ),
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            try:
                _idle = int(str(_raw_idle))
                _max_age = int(str(_raw_max_age))
            except (ValueError, TypeError) as _e:
                return _create_config_page_response(
                    request,
                    session,
                    error_message=f"Invalid numeric value: {_e}",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            _enabled = str(_raw_enabled).lower() in _TOTP_TRUTHY_SET
            # One audited change, off the event loop.
            await anyio.to_thread.run_sync(
                functools.partial(
                    config_service.update_totp_elevation_audited,
                    enabled=_enabled,
                    idle_timeout_seconds=_idle,
                    max_age_seconds=_max_age,
                    session_manager=_esm,
                    actor=session.username,
                )
            )
            return _create_config_page_response(
                request,
                session,
                success_message=f"{section.title()} configuration saved successfully",
            )

        # Story #1195 AC2: Read pre-change host/port BEFORE any mutation (Opus nit).
        # This snapshot is the authoritative "what was persisted" value so the
        # guardrail compares the incoming form value against the ORIGINAL, not an
        # already-mutated in-memory config.
        _pre_change_config = config_service.get_config()

        # Story #1195 AC2: Server-side host/port confirmation guardrail.
        # Client-side modal is UX-only; a direct POST bypasses it, so enforcement
        # MUST be server-side.  HAProxy and firewall are bound to the existing
        # host/port — a silent change would sever cluster connectivity.
        # Guardrail fires ONLY when host or port TRULY differs from the persisted
        # value AND the explicit confirm flag is absent from the form submission.
        if section == "server":
            _incoming_host = str(data.get("host", _pre_change_config.host))
            _incoming_port_raw = data.get("port", str(_pre_change_config.port))
            try:
                _incoming_port = int(str(_incoming_port_raw))
            except (ValueError, TypeError):
                _incoming_port = _pre_change_config.port
            _host_changed = _incoming_host != _pre_change_config.host
            _port_changed = _incoming_port != _pre_change_config.port
            _confirm_flag = bool(data.get("confirm_host_port_change", ""))
            if (_host_changed or _port_changed) and not _confirm_flag:
                return _create_config_page_response(
                    request,
                    session,
                    error_message=(
                        "Changing host or port requires explicit confirmation — "
                        "HAProxy backend and firewall port-lock warning. "
                        "Please confirm the change via the confirmation dialog."
                    ),
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

        # Strip the confirm flag from data before passing to update_setting
        # (it is a UI-only field, not a config key).
        data.pop("confirm_host_port_change", None)

        # Bug #1180: the search_event_log UI section renders two fields that
        # belong to different config categories:
        #   - search_event_log_retention_days -> category "search_event_log"
        #   - export_retention_days           -> category "export"
        # Without this override, every key is sent under the URL section name,
        # causing _update_search_event_log_setting to raise ValueError on
        # export_retention_days.
        _section_key_category_overrides: dict = {}
        if section == "search_event_log":
            _section_key_category_overrides = {"export_retention_days": "export"}
        _updates = [
            (_section_key_category_overrides.get(key, section), key, value)
            for key, value in data.items()
        ]

        # Story #1400 CRITICAL 6: the whole section is applied as ONE atomic,
        # audited change (validate-copy-then-publish -- a rejected batch
        # never touches the live config, no per-key partial application),
        # off the event loop.  OIDC builds managers for the CANDIDATE before
        # anything is published (a failed build publishes nothing) and they
        # go live only once the change is published.
        prepared_oidc: Dict[str, Any] = {}
        before_publish = (
            functools.partial(
                _prepare_oidc_candidate_from_worker_thread, prepared=prepared_oidc
            )
            if section == "oidc"
            else None
        )
        try:
            await anyio.to_thread.run_sync(
                functools.partial(
                    config_service.update_settings_audited,
                    _updates,
                    actor=session.username,
                    before_publish=before_publish,
                )
            )
        except _OidcReloadFailed as e:
            return _create_config_page_response(
                request,
                session,
                error_message=f"Invalid OIDC configuration: {str(e)}. Changes not saved.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        except BootstrapFileNotWritten:
            _install_oidc_managers(prepared_oidc)  # published all the same
            raise
        _install_oidc_managers(prepared_oidc)

        logger.info(
            f"Saved {section} configuration with {len(data)} settings",
            extra={"correlation_id": get_correlation_id()},
        )

        return _create_config_page_response(
            request,
            session,
            success_message=f"{section.title()} configuration saved successfully",
        )
    except ValueError as e:
        return _create_config_page_response(
            request,
            session,
            error_message=f"Failed to save configuration: {str(e)}",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-048", f"Failed to save config section {section}: {e}"
            ),
            extra={"correlation_id": get_correlation_id()},
        )
        return _create_config_page_response(
            request,
            session,
            error_message=f"Failed to save configuration: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@web_router.get("/partials/config-section", response_class=HTMLResponse)
def config_section_partial(
    request: Request,
    section: Optional[str] = None,
):
    """
    Partial refresh endpoint for config section.

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Reuse existing CSRF token from cookie instead of generating new one
    csrf_token = get_csrf_token_from_cookie(request)
    if not csrf_token:
        # Fallback: generate new token if cookie missing/invalid
        csrf_token = generate_csrf_token()
    config = _get_current_config()
    # SIEM delivery read-only status: this process's in-memory view (no I/O).
    from ...services.siem_delivery.config_view import status_block as _siem_status

    config["siem_delivery_status"] = _siem_status(
        getattr(request.app.state, "siem_delivery_scheduler", None),
        getattr(request.app.state, "siem_delivery_startup_error", None),
    )

    # Load API keys status
    token_manager = _get_token_manager()
    api_keys_status = token_manager.list_tokens()
    github_token_data = _ci_token_display(token_manager.get_token("github"))
    gitlab_token_data = _ci_token_display(token_manager.get_token("gitlab"))

    response = templates.TemplateResponse(
        request,
        "partials/config_section.html",
        {
            "request": request,
            "csrf_token": csrf_token,
            "config": config,
            "validation_errors": {},
            "api_keys_status": api_keys_status,
            "github_token_data": github_token_data,
            "gitlab_token_data": gitlab_token_data,
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


# =============================================================================
# API Keys Management
# =============================================================================


@web_router.post(
    "/config/api-keys/{platform}",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def save_api_key(
    request: Request,
    platform: str,
    csrf_token: Optional[str] = Form(None),
    token: str = Form(...),
    api_url: Optional[str] = Form(None),
):
    """Save API key for CI/CD platform (GitHub or GitLab)."""
    # Require admin authentication
    session = _require_admin_session(request)
    if not session:
        return RedirectResponse(
            url="/user/login", status_code=status.HTTP_303_SEE_OTHER
        )

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_config_page_response(
            request,
            session,
            error_message="Invalid CSRF token",
            status_code=status.HTTP_403_FORBIDDEN,
        )

    # Validate platform
    if platform not in ["github", "gitlab"]:
        return _create_config_page_response(
            request,
            session,
            error_message=f"Invalid platform: {platform}",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    from ...services.token_encryption import StoredSecretLegacyWriteRefused

    # Save token using CITokenManager - use same server_dir as config service
    try:
        token_manager = _get_token_manager()
        # Strip whitespace from token before validation (Issue #716 Bug 2a)
        token = token.strip()
        token_manager.save_token_audited(
            platform, token, base_url=api_url, actor=session.username
        )

        platform_name = "GitHub" if platform == "github" else "GitLab"
        return _create_config_page_response(
            request,
            session,
            success_message=f"{platform_name} API key saved successfully",
        )
    except TokenValidationError as e:
        return _create_config_page_response(
            request,
            session,
            error_message=f"Invalid token format: {str(e)}",
            validation_errors={"api_keys": str(e)},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except StoredSecretLegacyWriteRefused as e:
        logger.warning(
            "%s API key save refused while the cluster upgrade is in progress",
            platform,
        )
        return _create_config_page_response(
            request,
            session,
            error_message=str(e),
            status_code=status.HTTP_409_CONFLICT,
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-050", f"Failed to save {platform} API key: {e}"
            ),
            extra={"correlation_id": get_correlation_id()},
        )
        return _create_config_page_response(
            request,
            session,
            error_message=f"Failed to save API key: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@web_router.delete(
    "/config/api-keys/{platform}",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def delete_api_key(
    request: Request,
    platform: str,
):
    """Delete API key for CI/CD platform."""
    # Require admin authentication
    session = _require_admin_session(request)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )

    # Validate CSRF token from header (HTMX sends it as X-CSRF-Token)
    csrf_from_header = request.headers.get("X-CSRF-Token")
    if not validate_login_csrf_token(request, csrf_from_header):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token"
        )

    # Validate platform
    if platform not in ["github", "gitlab"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid platform: {platform}",
        )

    # Delete token using CITokenManager - use same server_dir as config service
    try:
        token_manager = _get_token_manager()
        token_manager.delete_token_audited(platform, actor=session.username)

        platform_name = "GitHub" if platform == "github" else "GitLab"
        logger.info(
            f"{platform_name} API key deleted successfully",
            extra={"correlation_id": get_correlation_id()},
        )

        # Return success HTML fragment (HTMX expects HTML response)
        return HTMLResponse(
            content=f'<div class="alert success">{platform_name} API key deleted</div>',
            status_code=200,
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "SVC-GENERAL-015", f"Failed to delete {platform} API key: {e}"
            ),
            extra={"correlation_id": get_correlation_id()},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete API key: {str(e)}",
        )
