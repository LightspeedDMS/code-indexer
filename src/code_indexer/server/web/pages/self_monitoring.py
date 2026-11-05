"""
Admin self-monitoring pages.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

from code_indexer.server.middleware.correlation import get_correlation_id
import asyncio
import functools
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

import anyio.to_thread

from fastapi import APIRouter, Depends, Request, Form, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from ...auth import dependencies
from ..auth import SessionData
from ...services.config_service import get_config_service
from code_indexer.server.logging_utils import format_error_log, get_log_extra
from code_indexer.server.storage.database_manager import DatabaseConnectionManager

from ._shared import (
    _create_login_redirect,
    _get_token_manager,
    _require_admin_session,
    generate_csrf_token,
    logger,
    set_csrf_cookie,
    templates,
    validate_login_csrf_token,
)

web_router = APIRouter()

# Self-Monitoring constants (Story #74)
SCAN_HISTORY_LIMIT = 50
ISSUES_HISTORY_LIMIT = 100


# ==============================================================================
# Self-Monitoring Routes (Story #74 - Epic #71)
# ==============================================================================


def _add_scan_duration(scans: List[Dict]) -> None:
    """Add a 'duration' field to each scan dict in-place."""
    from datetime import datetime

    for scan in scans:
        if scan.get("completed_at") and scan.get("started_at"):
            try:
                started = datetime.fromisoformat(scan["started_at"])
                completed = datetime.fromisoformat(scan["completed_at"])
                duration_seconds = (completed - started).total_seconds()
                minutes = int(duration_seconds // 60)
                seconds = int(duration_seconds % 60)
                scan["duration"] = f"{minutes}m {seconds}s"
            except (ValueError, TypeError):
                scan["duration"] = "N/A"
        elif scan.get("status") == "RUNNING":
            scan["duration"] = "In progress"
        else:
            scan["duration"] = "N/A"


def _load_self_monitoring_data(
    db_path: Path,
    session: SessionData,
    backend: Optional[Any] = None,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Load self-monitoring scans and issues from database (Story #74 AC4, AC5).

    In postgres mode, delegates to the SelfMonitoringBackend protocol methods
    instead of direct SQLite access.

    Args:
        db_path:  Path to SQLite database (used when backend is None)
        session:  Current user session (for logging)
        backend:  Optional SelfMonitoringBackend; when provided, used instead
                  of direct SQLite access.

    Returns:
        Tuple of (scans, issues) as lists of dicts
    """
    if backend is not None:
        try:
            scans = backend.list_scans(limit=SCAN_HISTORY_LIMIT)
            _add_scan_duration(scans)
            issues = backend.list_issues(limit=ISSUES_HISTORY_LIMIT)
            return scans, issues
        except Exception as e:
            logger.error(
                format_error_log(
                    "WEB-SELF-MONITORING-001",
                    f"Failed to load self-monitoring data via backend: {e}",
                ),
                extra=get_log_extra("WEB-SELF-MONITORING-001"),
            )
            return [], []

    scans = []
    issues = []

    try:
        # Bug #1532 follow-up: route the raw connection through
        # guarded_connection() so close_all() cannot close it mid-read.
        with DatabaseConnectionManager.get_instance(
            str(db_path)
        ).guarded_connection() as conn:
            cursor = conn.cursor()
            cursor.row_factory = cast(Any, sqlite3.Row)

            # Load scans (most recent first)
            cursor.execute(
                """
                SELECT scan_id, started_at, completed_at, status,
                       log_id_start, log_id_end, issues_created, error_message
                FROM self_monitoring_scans
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (SCAN_HISTORY_LIMIT,),
            )
            scans = [dict(row) for row in cursor.fetchall()]

            # Load issues (most recent first)
            cursor.execute(
                """
                SELECT id, scan_id, github_issue_number, github_issue_url,
                       classification, title, fingerprint,
                       source_log_ids, source_files, created_at
                FROM self_monitoring_issues
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (ISSUES_HISTORY_LIMIT,),
            )
            issues = [dict(row) for row in cursor.fetchall()]
        _add_scan_duration(scans)
    except Exception as e:
        logger.error(
            format_error_log(
                "WEB-SELF-MONITORING-001",
                f"Failed to load self-monitoring data: {e}",
            ),
            extra=get_log_extra("WEB-SELF-MONITORING-001"),
        )

    return scans, issues


def _load_default_prompt() -> str:
    """
    Load default self-monitoring prompt template (Story #74).

    Returns:
        Default prompt text, or empty string if file not found
    """
    default_prompt_path = (
        Path(__file__).parent.parent.parent
        / "self_monitoring"
        / "prompts"
        / "default_analysis_prompt.md"
    )
    if default_prompt_path.exists():
        return default_prompt_path.read_text()
    return ""


def _get_last_scan_time(
    db_path: Path,
    backend: Optional[Any] = None,
) -> Optional[str]:
    """
    Get timestamp of most recent scan (Bug #129 Fix - Problem 1).

    In postgres mode, delegates to the SelfMonitoringBackend protocol.

    Args:
        db_path:  Path to SQLite database (used when backend is None)
        backend:  Optional SelfMonitoringBackend; when provided, used instead
                  of direct SQLite access.

    Returns:
        started_at timestamp of most recent scan, or None if no scans exist
    """
    if backend is not None:
        try:
            return backend.get_last_started_at()  # type: ignore[no-any-return]
        except Exception as e:
            logger.error(
                format_error_log(
                    "WEB-SELF-MONITORING-002",
                    f"Failed to get last scan time via backend: {e}",
                ),
                extra=get_log_extra("WEB-SELF-MONITORING-002"),
            )
            return None

    try:
        # Bug #1532 follow-up: route the raw connection through
        # guarded_connection() so close_all() cannot close it mid-read.
        with DatabaseConnectionManager.get_instance(
            str(db_path)
        ).guarded_connection() as conn:
            cursor = conn.cursor()
            cursor.row_factory = cast(Any, sqlite3.Row)
            cursor.execute("""
                SELECT started_at
                FROM self_monitoring_scans
                ORDER BY started_at DESC
                LIMIT 1
                """)
            row = cursor.fetchone()
            return row["started_at"] if row else None
    except Exception as e:
        logger.error(
            format_error_log(
                "WEB-SELF-MONITORING-002",
                f"Failed to get last scan time: {e}",
            ),
            extra=get_log_extra("WEB-SELF-MONITORING-002"),
        )
        return None


def _calculate_next_scan_time(
    last_scan_time: Optional[str], cadence_minutes: int
) -> Optional[str]:
    """
    Calculate next scan time based on last scan + cadence (Bug #129 Fix - Problem 2).

    Args:
        last_scan_time: ISO timestamp of last scan (or None)
        cadence_minutes: Scan cadence in minutes

    Returns:
        ISO timestamp of next scan, or None if cannot calculate
    """
    if not last_scan_time:
        return None

    try:
        from datetime import datetime, timedelta

        last_scan_dt = datetime.fromisoformat(last_scan_time)
        next_scan_dt = last_scan_dt + timedelta(minutes=cadence_minutes)
        return next_scan_dt.isoformat()
    except Exception as e:
        logger.error(
            format_error_log(
                "WEB-SELF-MONITORING-003",
                f"Failed to calculate next scan time: {e}",
            ),
            extra=get_log_extra("WEB-SELF-MONITORING-003"),
        )
        return None


def _get_scan_status(
    db_path: Path,
    backend: Optional[Any] = None,
) -> str:
    """
    Get current scan status (Bug #129 Fix - Problem 3).

    In postgres mode, delegates to the SelfMonitoringBackend protocol.

    Args:
        db_path:  Path to SQLite database (used when backend is None)
        backend:  Optional SelfMonitoringBackend; when provided, used instead
                  of direct SQLite access.

    Returns:
        "Running..." if self_monitoring job is running, "Idle" otherwise
    """
    if backend is not None:
        try:
            count = backend.get_running_scan_count()
            return "Running..." if count > 0 else "Idle"
        except Exception as e:
            logger.error(
                format_error_log(
                    "WEB-SELF-MONITORING-004",
                    f"Failed to get scan status via backend: {e}",
                ),
                extra=get_log_extra("WEB-SELF-MONITORING-004"),
            )
            return "Idle"

    try:
        # Bug #1532 follow-up: route the raw connection through
        # guarded_connection() so close_all() cannot close it mid-read.
        with DatabaseConnectionManager.get_instance(
            str(db_path)
        ).guarded_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*)
                FROM self_monitoring_scans
                WHERE completed_at IS NULL
                """)
            count = cursor.fetchone()[0]
            return "Running..." if count > 0 else "Idle"
    except Exception as e:
        logger.error(
            format_error_log(
                "WEB-SELF-MONITORING-004",
                f"Failed to get scan status: {e}",
            ),
            extra=get_log_extra("WEB-SELF-MONITORING-004"),
        )
        return "Idle"


def _create_self_monitoring_page_response(
    request: Request,
    session: SessionData,
    self_monitoring_config,
    scans: List[Dict],
    issues: List[Dict],
    db_path: Path,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
    backend: Optional[Any] = None,
):
    """
    Build self-monitoring page response with template context (Story #74, Bug #129).

    Args:
        request: FastAPI request
        session: Current user session
        self_monitoring_config: SelfMonitoringConfig instance
        scans: Scan history list
        issues: Issues history list
        db_path: Path to database (for status calculations - Bug #129, SQLite mode)
        success_message: Optional success message to display
        error_message: Optional error message to display
        backend: Optional SelfMonitoringBackend; when provided (PG mode), used
                 instead of direct SQLite for status queries.

    Returns:
        TemplateResponse with CSRF cookie set

    Note (Story #566): default_prompt and current_prompt parameters removed.
    The prompt is now managed exclusively in code via default_analysis_prompt.md.
    """
    # Bug #129 Fix: Calculate status values from database (or backend in PG mode)
    last_scan = _get_last_scan_time(db_path, backend=backend)
    next_scan = _calculate_next_scan_time(
        last_scan, self_monitoring_config.cadence_minutes
    )
    scan_status = _get_scan_status(db_path, backend=backend)

    # Format values for display
    last_scan_display = last_scan if last_scan else "Never"
    next_scan_display = next_scan if next_scan else "N/A"

    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "self_monitoring.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "self-monitoring",
            "show_nav": True,
            "csrf_token": csrf_token,
            "config": self_monitoring_config,
            "scans": scans,
            "issues": issues,
            "success_message": success_message,
            "error_message": error_message,
            # Bug #129 Fix: Pass calculated status values to template
            "last_scan": last_scan_display,
            "next_scan": next_scan_display,
            "scan_status": scan_status,
        },
    )
    set_csrf_cookie(response, csrf_token, path="/")
    return response


@web_router.get("/self-monitoring", response_class=HTMLResponse)
def self_monitoring_page(request: Request):
    """
    Self-Monitoring configuration and monitoring page (Story #74).

    Displays:
    - AC2: Status section (enabled/disabled, last scan, next scan)
    - AC3: Configuration section (enable toggle, cadence, model)
    - AC4: Scan history from self_monitoring_scans table
    - AC5: Created issues from self_monitoring_issues table

    Requires authenticated admin session.
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    # Get configuration
    config_service = get_config_service()
    config = config_service.get_config()
    self_monitoring_config = config.self_monitoring_config

    # Load scan history and issues from database (or backend in PG mode)
    server_dir = config_service.config_manager.server_dir
    db_path = server_dir / "data" / "cidx_server.db"
    _backend_registry = getattr(request.app.state, "backend_registry", None)
    _sm_backend = (
        _backend_registry.self_monitoring if _backend_registry is not None else None
    )
    scans, issues = _load_self_monitoring_data(db_path, session, backend=_sm_backend)

    return _create_self_monitoring_page_response(
        request,
        session,
        self_monitoring_config,
        scans,
        issues,
        db_path,
        backend=_sm_backend,
    )


@web_router.post(
    "/self-monitoring",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
async def save_self_monitoring_config(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """
    Save self-monitoring configuration (Story #74 AC6).

    Updates SelfMonitoringConfig from form data including enable/disable,
    cadence, and model.

    Requires authenticated admin session and valid CSRF token.
    """
    session = await asyncio.to_thread(_require_admin_session, request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    config_service = get_config_service()
    config = config_service.get_config()

    # Validate CSRF token
    _backend_registry = getattr(request.app.state, "backend_registry", None)
    _sm_backend = (
        _backend_registry.self_monitoring if _backend_registry is not None else None
    )
    if not validate_login_csrf_token(request, csrf_token):
        server_dir = config_service.config_manager.server_dir
        db_path = server_dir / "data" / "cidx_server.db"
        scans, issues = _load_self_monitoring_data(
            db_path, session, backend=_sm_backend
        )
        return _create_self_monitoring_page_response(
            request,
            session,
            config.self_monitoring_config,
            scans,
            issues,
            db_path,
            error_message="Invalid CSRF token",
            backend=_sm_backend,
        )

    # Parse form data
    # Story #566: prompt_template is intentionally not read from form data.
    # The analysis prompt is managed exclusively in code via default_analysis_prompt.md.
    form_data = await request.form()

    enabled = form_data.get("enabled") == "on"

    try:
        cadence_minutes = int(form_data.get("cadence_minutes", "60"))  # type: ignore[arg-type]
    except ValueError:
        logger.debug("Invalid cadence_minutes value in form, defaulting to 60")
        cadence_minutes = 60

    model = form_data.get("model", "opus").strip()  # type: ignore[union-attr]
    if model not in ("opus", "sonnet", "haiku"):
        model = "opus"

    # Update configuration (enabled, cadence, and model only) as ONE audited
    # change on a candidate copy, off the event loop.
    def _set_self_monitoring(candidate) -> None:
        candidate.self_monitoring_config.enabled = enabled
        candidate.self_monitoring_config.cadence_minutes = cadence_minutes
        candidate.self_monitoring_config.model = model

    config = await asyncio.to_thread(
        functools.partial(
            config_service.apply_audited_change,
            _set_self_monitoring,
            actor=session.username,
            target_id="self_monitoring",
        )
    )

    # Bug #128: Start/stop service based on enabled flag
    service = getattr(request.app.state, "self_monitoring_service", None)

    if service is not None:
        # Critical: Update service's internal _enabled flag BEFORE start/stop
        # This ensures trigger_scan() works after enabling via toggle
        service._enabled = enabled

        if enabled and not service.is_running:
            service.start()
            logger.info("Self-monitoring service started via configuration toggle")
        elif not enabled and service.is_running:
            service.stop()
            logger.info("Self-monitoring service stopped via configuration toggle")

    # Re-render page with success message
    server_dir = config_service.config_manager.server_dir
    db_path = server_dir / "data" / "cidx_server.db"
    scans, issues = _load_self_monitoring_data(db_path, session, backend=_sm_backend)

    return _create_self_monitoring_page_response(
        request,
        session,
        config.self_monitoring_config,
        scans,
        issues,
        db_path,
        success_message="Self-monitoring configuration saved successfully",
        backend=_sm_backend,
    )


@web_router.post(
    "/self-monitoring/run-now",
    response_class=JSONResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
async def trigger_manual_scan(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """
    Manually trigger a self-monitoring scan (Story #75 AC1, AC2).

    Submits a scan job to the background job queue. Returns immediately with
    queued status - scan executes asynchronously.

    Returns:
        JSON response with status and scan_id:
        - Success: {"status": "queued", "scan_id": "..."}
        - Error: {"status": "error", "error": "..."}

    Requires authenticated admin session and valid CSRF token.
    """
    logger.debug("[SELF-MON-DEBUG] trigger_manual_scan: Entry - endpoint called")

    session = await asyncio.to_thread(_require_admin_session, request)
    if not session:
        logger.debug("[SELF-MON-DEBUG] trigger_manual_scan: No admin session found")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        logger.debug("[SELF-MON-DEBUG] trigger_manual_scan: Invalid CSRF token")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token"
        )

    logger.debug("[SELF-MON-DEBUG] trigger_manual_scan: Auth validated, loading config")

    # Get self-monitoring service from app state
    # The service is initialized in app.py during startup
    from code_indexer.server.self_monitoring.service import SelfMonitoringService
    import os
    from pathlib import Path

    config_service = get_config_service()
    config = config_service.get_config()

    logger.debug(
        f"[SELF-MON-DEBUG] trigger_manual_scan: Config loaded - enabled={config.self_monitoring_config.enabled}, cadence_minutes={config.self_monitoring_config.cadence_minutes}, model={config.self_monitoring_config.model}"  # type: ignore[union-attr]
    )

    # Get background job manager from app state
    job_manager = getattr(request.app.state, "background_job_manager", None)
    logger.debug(
        f"[SELF-MON-DEBUG] trigger_manual_scan: job_manager={job_manager is not None}"
    )

    # Get database paths from app state and config (Bug #87)
    server_data_dir = os.environ.get(
        "CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server")
    )
    db_path = str(Path(server_data_dir) / "data" / "cidx_server.db")
    log_db_path = getattr(request.app.state, "log_db_path", None)
    if log_db_path:
        log_db_path = str(log_db_path)

    logger.debug(
        f"[SELF-MON-DEBUG] trigger_manual_scan: Database paths - db_path={db_path}, log_db_path={log_db_path}"
    )

    # Get repo_root and github_repo from app.state (auto-detected at startup)
    # Bug Fix: MONITOR-GENERAL-011 - CIDX_REPO_ROOT env var ensures reliable detection
    repo_root = getattr(request.app.state, "self_monitoring_repo_root", None)
    github_repo = getattr(request.app.state, "self_monitoring_github_repo", None)

    logger.debug(
        f"[SELF-MON-DEBUG] trigger_manual_scan: Repo config - repo_root={repo_root}, github_repo={github_repo}"
    )

    if not github_repo:
        logger.debug(
            "[SELF-MON-DEBUG] trigger_manual_scan: github_repo is None - returning error"
        )
        logger.error(
            format_error_log(
                "MONITOR-GENERAL-011",
                "Self-monitoring: github_repo auto-detection failed. "
                "Ensure CIDX_REPO_ROOT environment variable is set correctly in systemd service.",
            ),
            extra={"correlation_id": get_correlation_id()},
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error",
                "error": "GitHub repository auto-detection failed. Check server logs.",
            },
        )

    # Get GitHub token for authentication (Bug #87)
    github_token_data = await anyio.to_thread.run_sync(
        lambda: _get_token_manager().get_token("github")
    )
    github_token = github_token_data.token if github_token_data else None

    logger.debug(
        f"[SELF-MON-DEBUG] trigger_manual_scan: GitHub token retrieved - has_token={github_token is not None}"
    )

    # Get server name for issue identification (Bug #87)
    server_name = config.service_display_name or "Neo"

    logger.debug(f"[SELF-MON-DEBUG] trigger_manual_scan: server_name={server_name}")

    # Derive self-monitoring backend from registry (Bug #1140).
    # Mirrors the lifespan.py wiring so manual scans write to PG in cluster
    # mode instead of node-local SQLite.
    backend_registry = getattr(request.app.state, "backend_registry", None)
    _sm_backend = (
        backend_registry.self_monitoring if backend_registry is not None else None
    )

    # Create service instance with current configuration (Bug #87)
    logger.debug(
        "[SELF-MON-DEBUG] trigger_manual_scan: Creating SelfMonitoringService instance"
    )
    service = SelfMonitoringService(
        enabled=config.self_monitoring_config.enabled,  # type: ignore[union-attr]
        cadence_minutes=config.self_monitoring_config.cadence_minutes,  # type: ignore[union-attr]
        job_manager=job_manager,
        db_path=db_path,
        log_db_path=log_db_path,
        github_repo=github_repo,
        model=config.self_monitoring_config.model,  # type: ignore[union-attr]
        repo_root=str(repo_root) if repo_root else None,
        github_token=github_token,
        server_name=server_name,
        storage_backend=_sm_backend,
    )

    # Trigger the scan
    logger.debug("[SELF-MON-DEBUG] trigger_manual_scan: Calling service.trigger_scan()")
    result = service.trigger_scan()
    logger.debug(
        f"[SELF-MON-DEBUG] trigger_manual_scan: trigger_scan returned - result={result}"
    )

    # Return JSON response
    if result["status"] == "error":
        logger.debug(
            f"[SELF-MON-DEBUG] trigger_manual_scan: Returning error response - {result}"
        )
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=result)

    logger.debug(
        f"[SELF-MON-DEBUG] trigger_manual_scan: Returning success response - {result}"
    )
    return JSONResponse(status_code=status.HTTP_200_OK, content=result)
