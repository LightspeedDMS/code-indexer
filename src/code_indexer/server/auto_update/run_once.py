#!/usr/bin/env python3
"""Auto-update service entry point - executes one polling iteration."""

from code_indexer.server.middleware.correlation import get_correlation_id
import sys
import os
import logging
from pathlib import Path
from typing import Optional, Tuple

from code_indexer.server.auto_update.service import AutoUpdateService
from code_indexer.server.auto_update.change_detector import ChangeDetector
from code_indexer.server.auto_update.deployment_lock import (
    DeploymentLock,
    get_default_lock_path,
)
from code_indexer.server.auto_update.deployment_executor import (
    PENDING_REDEPLOY_MARKER,
    DeploymentExecutor,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

# Bug #1782: config.json/ServerConfigManager is no longer authoritative for
# host/port (Story #1196) -- its dataclass defaults silently masked a missing
# value. URL resolution now reuses DeploymentExecutor's own launch-config
# mechanism: applied_launch.json, falling back to the live systemd ExecStart.


def _resolve_server_url(deployment_executor: DeploymentExecutor) -> str:
    """Resolve CIDX server URL via the authoritative launch-config mechanism.

    Args:
        deployment_executor: the DeploymentExecutor instance main() already
            constructed for this polling iteration; its service_name/repo_path
            are reused so URL resolution never diverges from the actual managed
            systemd unit.

    Returns:
        Fully-qualified server URL, e.g. "http://0.0.0.0:8080".

    Raises:
        RuntimeError: when NEITHER applied_launch.json NOR the live systemd
            ExecStart can supply a host/port -- genuine total unresolvability.
    """
    raw = deployment_executor._read_launch_source("DEPLOY")
    host = raw.get("host") if raw else None
    port = raw.get("port") if raw else None
    workers = raw.get("workers") if raw else None
    host, port, workers = deployment_executor._fill_from_live_execstart(
        "DEPLOY", host, port, workers
    )
    if host is None or port is None:
        raise RuntimeError(
            "Bug #1782: cannot resolve cidx-server URL — neither applied_launch.json "
            "nor the live systemd ExecStart provided a host/port. The auto-updater "
            "requires either a completed deploy cycle (which writes "
            "applied_launch.json) or an installed cidx-server systemd unit with "
            "--host/--port flags before it can determine where to send "
            "maintenance-mode API calls. Run the CIDX installer or wait for the "
            "next successful deploy."
        )
    return f"http://{host}:{int(port)}"


def _redeploy_marker_identity() -> Optional[Tuple[int, int]]:
    """(inode, mtime_ns) of the pending-redeploy marker, or None if absent."""
    try:
        st = PENDING_REDEPLOY_MARKER.stat()
    except FileNotFoundError:
        return None
    return (st.st_ino, st.st_mtime_ns)


def _consume_redeploy_marker(identity_before: Optional[Tuple[int, int]]) -> None:
    """Remove the marker the completed retry served (Bug #2064); never raises.

    Called only after the retry deploy AND the cidx-server restart
    succeeded.  Without this, the next poll_once() finds the marker and
    forces a second deploy and restart.  A marker that is absent, or was
    re-created during the retry deploy (a fresh redeploy request, e.g. from
    the auto-updater unit's Python being rewritten), is left in place.

    Check-then-unlink is safe: the only other writer runs inside this
    process's own deploy, and cidx-auto-update is a Type=oneshot unit
    started by a timer, which systemd never runs twice at the same time.
    A marker that cannot be checked or removed is kept (WARNING): the cost
    is one extra redeploy, never a skipped one.
    """
    if identity_before is None:
        return
    try:
        if _redeploy_marker_identity() != identity_before:
            logger.info(
                "Pending-redeploy marker was re-created during the retry "
                "deploy; keeping it for the next run",
                extra={"correlation_id": get_correlation_id()},
            )
            return
        PENDING_REDEPLOY_MARKER.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(
            f"Could not consume pending-redeploy marker after retry deploy "
            f"({type(e).__name__}); the next run will redeploy and restart again",
            extra={"correlation_id": get_correlation_id()},
        )


def main():
    """Execute one auto-update polling iteration.

    Self-restart mechanism: Checks for pending_restart/failed status on startup
    and retries deployment if needed (bootstrap problem recovery).
    """
    try:
        # Configuration
        repo_path = Path(
            os.environ.get("CIDX_SERVER_REPO_PATH", "/opt/code-indexer-repo")
        )
        branch = os.environ.get("CIDX_AUTO_UPDATE_BRANCH") or "master"
        lock_file = get_default_lock_path()
        check_interval = 60  # seconds (not used in oneshot mode)

        # Initialize components — construct executor first (Bug #884: must happen
        # before _resolve_server_url so _should_retry_on_startup can run even
        # when the launch config is missing).  server_url is assigned below once known.
        change_detector = ChangeDetector(repo_path=repo_path, branch=branch)
        deployment_lock = DeploymentLock(lock_file=lock_file)
        deployment_executor = DeploymentExecutor(
            repo_path=repo_path,
            branch=branch,
            service_name="cidx-server",
        )

        # Check if we need to retry deployment from previous run — BEFORE
        # resolving the server URL so a missing/incomplete launch config cannot
        # prevent recovery from a pending_restart/failed status (Bug #884).
        if deployment_executor._should_retry_on_startup():
            logger.info(
                "Pending deployment detected, retrying",
                extra={"correlation_id": get_correlation_id()},
            )
            # Bug #884: Resolve URL inside the retry branch; failure here means
            # we cannot reach the server API — write failed status and exit.
            try:
                server_url = _resolve_server_url(deployment_executor)
                deployment_executor.server_url = server_url
            except Exception as e:
                deployment_executor._write_status_file("failed", str(e))
                sys.exit(1)

            deployment_executor._write_status_file(
                "in_progress", "Retrying deployment after restart"
            )

            # Execute full deployment
            marker_before = _redeploy_marker_identity()
            success = deployment_executor.execute()

            if success:
                # Restart CIDX server after successful deployment.  Bug #2064:
                # only a COMPLETED restart consumes the pending-redeploy
                # marker; a failed or interrupted restart keeps it (and a
                # failed status), so the next run re-drives the restart.
                success = deployment_executor.restart_server()
                if success:
                    deployment_executor._write_status_file(
                        "success", "Deployment completed"
                    )
                    _consume_redeploy_marker(marker_before)
                    logger.info(
                        "Retry deployment completed successfully",
                        extra={"correlation_id": get_correlation_id()},
                    )
                else:
                    deployment_executor._write_status_file(
                        "failed",
                        "Deployment succeeded but server restart failed during retry",
                    )
                    logger.error(
                        "Retry deployment: server restart failed; will retry",
                        extra={"correlation_id": get_correlation_id()},
                    )
            else:
                deployment_executor._write_status_file(
                    "failed", "Deployment failed during retry"
                )
                logger.error(
                    "Retry deployment failed",
                    extra={"correlation_id": get_correlation_id()},
                )

            sys.exit(0 if success else 1)

        # Bug #882/#1782: Resolve real server URL via the launch-config
        # mechanism so maintenance-mode calls hit the operator-configured
        # port, not a hardcoded/masked default.
        server_url = _resolve_server_url(deployment_executor)
        deployment_executor.server_url = server_url

        # Initialize service
        service = AutoUpdateService(
            repo_path=repo_path,
            check_interval=check_interval,
            lock_file=lock_file,
        )

        # Inject dependencies
        service.change_detector = change_detector
        service.deployment_lock = deployment_lock
        service.deployment_executor = deployment_executor

        # Execute one polling iteration
        logger.info(
            "Starting auto-update polling iteration",
            extra={"correlation_id": get_correlation_id()},
        )
        service.poll_once()
        logger.info(
            "Auto-update polling iteration completed",
            extra={"correlation_id": get_correlation_id()},
        )

        sys.exit(0)

    except Exception as e:
        logger.exception(
            f"Auto-update polling failed: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
