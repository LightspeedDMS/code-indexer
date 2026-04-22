#!/usr/bin/env python3
"""Auto-update service entry point - executes one polling iteration."""

from code_indexer.server.middleware.correlation import get_correlation_id
import sys
import os
import logging
from pathlib import Path

from code_indexer.server.auto_update.service import AutoUpdateService
from code_indexer.server.auto_update.change_detector import ChangeDetector
from code_indexer.server.auto_update.deployment_lock import DeploymentLock
from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor
from code_indexer.server.utils.config_manager import ServerConfigManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


def _resolve_server_url() -> str:
    """Resolve CIDX server URL from ~/.cidx-server/config.json (Bug #882).

    The auto-updater runs as a systemd unit on the same host as cidx-server.
    The operator-configured host/port live in config.json (bootstrap settings);
    if that file is missing, cidx-server itself cannot have started, so the
    auto-updater has nothing to manage. Fail loud instead of guessing a URL.

    Returns:
        Fully-qualified server URL, e.g. "http://0.0.0.0:8080".

    Raises:
        RuntimeError: if config.json cannot be loaded — the operator must run
            the CIDX installer or write a valid config.json before the
            auto-updater can function.
    """
    cfg = ServerConfigManager().load_config()
    if cfg is None:
        raise RuntimeError(
            "Bug #882: cannot resolve cidx-server URL — no config.json found. "
            "The auto-updater requires ~/.cidx-server/config.json (or the path "
            "referenced by CIDX_SERVER_DIR) to discover the operator-configured "
            "host/port. Run the CIDX installer or create a valid config.json "
            "before re-running the auto-updater."
        )
    return f"http://{cfg.host}:{cfg.port}"


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
        lock_file = Path("/tmp/cidx-auto-update.lock")
        check_interval = 60  # seconds (not used in oneshot mode)

        # Bug #882: Resolve real server URL from config.json so maintenance-mode
        # calls hit the operator-configured port, not the hardcoded default.
        server_url = _resolve_server_url()

        # Initialize components
        change_detector = ChangeDetector(repo_path=repo_path, branch=branch)
        deployment_lock = DeploymentLock(lock_file=lock_file)
        deployment_executor = DeploymentExecutor(
            repo_path=repo_path,
            branch=branch,
            service_name="cidx-server",
            server_url=server_url,
        )

        # Check if we need to retry deployment from previous run
        if deployment_executor._should_retry_on_startup():
            logger.info(
                "Pending deployment detected, retrying",
                extra={"correlation_id": get_correlation_id()},
            )
            deployment_executor._write_status_file(
                "in_progress", "Retrying deployment after restart"
            )

            # Execute full deployment
            success = deployment_executor.execute()

            if success:
                deployment_executor._write_status_file(
                    "success", "Deployment completed"
                )
                # Restart CIDX server after successful deployment
                deployment_executor.restart_server()
                logger.info(
                    "Retry deployment completed successfully",
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
