"""
Admin server restart endpoint.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

import json
import os
import sys
import threading
import time

from fastapi import APIRouter, Depends, Request, HTTPException, status
from fastapi.responses import JSONResponse
from ...auth import dependencies
from ...services.config_service import ConfigChangeConflict, get_config_service
from code_indexer.server.auto_update.deployment_executor import RESTART_SIGNAL_PATH

from ._shared import (
    _require_admin_session,
    logger,
    validate_login_csrf_token,
)

web_router = APIRouter()

# Rate limiting for restart endpoint (Code Review Issue #6)
_restart_in_progress = False
_restart_lock = threading.Lock()


def _delayed_restart(delay: int = 2) -> None:
    """
    Execute server restart after a delay.

    Story #205: Server Restart from Diagnostics Tab

    This function sleeps for the specified delay to allow the HTTP response
    to complete, then restarts the server using the appropriate method:
    - Systemd mode: Writes restart signal file for auto-updater pickup
    - Dev mode: Uses os.execv to re-exec the current process

    Args:
        delay: Seconds to wait before restarting (default: 2)
    """
    global _restart_in_progress

    # Sleep to allow HTTP response to complete
    time.sleep(delay)

    # Log before restarting
    logger.info("Executing server restart now")

    # Detect if running under systemd
    is_systemd = os.environ.get("INVOCATION_ID") is not None

    if is_systemd:
        # Story #355: Systemd mode - write signal file for auto-updater to pick up.
        # This avoids needing sudo/privilege escalation (NoNewPrivileges=true compatible).
        from datetime import datetime as _datetime

        signal_data = {
            "timestamp": _datetime.now().isoformat(),
            "reason": "diagnostics_restart",
        }
        try:
            RESTART_SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            RESTART_SIGNAL_PATH.write_text(json.dumps(signal_data))
            logger.info(
                "Restart signal written to %s, waiting for auto-updater to execute",
                RESTART_SIGNAL_PATH,
            )
        except OSError as e:
            logger.error("Failed to write restart signal file: %s", e)
        # Reset flag immediately - we won't block waiting for the auto-updater
        with _restart_lock:
            _restart_in_progress = False
    else:
        # Dev mode: re-exec the current process (Code Review Issue #4)
        logger.info("Restarting via os.execv (dev mode)")
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except OSError as e:
            logger.error("os.execv failed: %s", e)
            # Reset flag on failure (Code Review Finding #1)
            with _restart_lock:
                _restart_in_progress = False


def _schedule_delayed_restart(delay: int = 2) -> None:
    """
    Schedule a delayed restart on a background daemon thread.

    Story #205: Server Restart from Diagnostics Tab

    Creates a daemon thread that will execute the restart after a delay.
    The daemon thread ensures the restart doesn't block the HTTP response.

    Args:
        delay: Seconds to wait before restarting (default: 2)
    """
    restart_thread = threading.Thread(
        target=_delayed_restart, args=(delay,), daemon=True
    )
    restart_thread.start()


def _restart_request_failed(exc: Exception) -> JSONResponse:
    """The restart request raised, so nothing is restarting: release the
    in-progress flag (a later restart stays possible) and tell the UI."""
    global _restart_in_progress
    with _restart_lock:
        _restart_in_progress = False
    logger.error("Server restart request failed: %s", exc, exc_info=True)
    if isinstance(exc, ConfigChangeConflict):
        return JSONResponse(
            status_code=409,
            content={
                "message": "The configuration kept changing concurrently; "
                "no restart was requested. Try again."
            },
        )
    return JSONResponse(
        status_code=500,
        content={
            "message": "The restart request failed; no restart was requested. "
            "See the server log."
        },
    )


@web_router.post(
    "/restart",
    response_class=JSONResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def restart_server(request: Request) -> JSONResponse:
    """
    Restart the CIDX server (admin only).

    Story #205: Server Restart from Diagnostics Tab

    This endpoint allows admin users to restart the CIDX server from the
    Diagnostics tab without requiring SSH access. The restart is delayed
    by 2 seconds to allow the HTTP 202 response to complete.

    Authentication:
        Requires admin session (checked via _require_admin_session)

    Security:
        Requires valid CSRF token in X-CSRF-Token header

    Rate Limiting:
        Only one restart can be in progress at a time (returns 409 if concurrent)

    Returns:
        202 Accepted with message about restart in progress
        409 Conflict if restart already in progress

    Raises:
        HTTPException 403: If user is not admin or CSRF token is invalid
    """
    global _restart_in_progress

    # Check for admin session
    session = _require_admin_session(request)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required"
        )

    # Validate CSRF token (Code Review Issues #1 and #2)
    csrf_from_header = request.headers.get("X-CSRF-Token")
    if not validate_login_csrf_token(request, csrf_from_header):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token"
        )

    # Rate limiting: Check if restart already in progress (Code Review Issue #6)
    with _restart_lock:
        if _restart_in_progress:
            return JSONResponse(
                status_code=409, content={"message": "Restart already in progress"}
            )
        _restart_in_progress = True

    # This request OWNS the claim from here on: any failure before a restart
    # is under way releases it (_restart_request_failed), or every later
    # restart would be refused.  Solo success hands the claim to the delayed
    # restart worker.
    try:
        username = session.username
        logger.info(f"Server restart requested by {username}")

        # Story #1200 AC7: branch on cluster vs solo mode.
        #
        # Cluster (PG pool present): bump launch_restart_generation only.
        # The bumping node does NOT synchronously write restart.signal here —
        # its own check_pending_launch_restart() poll (running every interval)
        # detects target > applied and signals itself (MAJOR-M3 / FIX-2).  All
        # other nodes are signalled by their own poll loops independently.
        #
        # Solo: retain the existing single-node restart path (materialize +
        # signal / os.execv).  Do NOT bump the generation in solo mode (FIX-5).
        from code_indexer.server.services.server_restart_audited import (
            request_server_restart,
        )

        config_svc = get_config_service()
        if config_svc._pool is not None:
            # Cluster mode: bump generation, let per-poll check handle
            # restart.signal.  The audit row is written durably BEFORE the bump.
            request_server_restart(
                config_svc.bump_launch_restart_generation,
                actor=username,
                scope="cluster",
            )
            with _restart_lock:
                _restart_in_progress = False
            return JSONResponse(
                status_code=202,
                content={
                    "message": (
                        "Cluster restart requested: generation bumped. "
                        "All nodes will restart via the auto-updater."
                    )
                },
            )

        # Solo mode: materialize launch config then schedule single-node
        # restart.  The audit row is written durably BEFORE it is scheduled.
        def _restart_this_node() -> None:
            config_svc.materialize_launch_config()
            _schedule_delayed_restart(delay=2)

        request_server_restart(_restart_this_node, actor=username, scope="node")
        # Return 202 Accepted immediately
        return JSONResponse(
            status_code=202, content={"message": "Server is restarting in 2 seconds..."}
        )
    except Exception as exc:
        return _restart_request_failed(exc)
