"""Story #1007 - Health watchdog for cidx-server.

Monitors server health via HTTP, tracks consecutive failures,
and triggers a systemctl restart after reaching the failure threshold
(subject to a cooldown window to avoid restart storms).

Entry point: python3 -m code_indexer.server.auto_update.health_watchdog
Intended use: invoked by a systemd timer every N seconds.
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants (documented defaults; all overridable via config)
# ---------------------------------------------------------------------------

DEFAULT_FAILURES_THRESHOLD: int = 3
DEFAULT_COOLDOWN_SECONDS: int = 300
DEFAULT_CHECK_TIMEOUT_SECONDS: int = 10
# Default service name — resolved from config first; this constant is the fallback.
HEALTH_WATCHDOG_SERVICE_NAME: str = "cidx-server"
STATE_FILE_NAME: str = "health_watchdog_state.json"

# Timeout for the systemctl restart subprocess call
_RESTART_SUBPROCESS_TIMEOUT: int = 30


# ---------------------------------------------------------------------------
# HealthWatchdog
# ---------------------------------------------------------------------------


class HealthWatchdog:
    """Monitors cidx-server health and restarts it after consecutive failures.

    State is persisted to a JSON file so failure counts survive process restarts
    (this process runs as a short-lived systemd oneshot on a timer).

    Args:
        server_url: Base URL of the cidx-server (e.g. "http://localhost:8000").
            Must be a non-empty string.
        state_file: Path to the persistent JSON state file. Must be a Path instance.
        failures_threshold: Number of consecutive failures before restart is triggered.
            Must be an integer >= 1.
        cooldown_seconds: Minimum seconds between two consecutive restarts.
            Must be an integer >= 0.
        check_timeout_seconds: HTTP request timeout for the /health endpoint.
            Must be an integer > 0.
        service_name: systemd service name to restart. Must be a non-empty,
            non-whitespace-only string. Defaults to HEALTH_WATCHDOG_SERVICE_NAME.

    Raises:
        ValueError: if any argument fails its validation constraint.
    """

    def __init__(
        self,
        server_url: str,
        state_file: Path,
        failures_threshold: int = DEFAULT_FAILURES_THRESHOLD,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        check_timeout_seconds: int = DEFAULT_CHECK_TIMEOUT_SECONDS,
        service_name: str = HEALTH_WATCHDOG_SERVICE_NAME,
    ) -> None:
        if not isinstance(server_url, str) or not server_url:
            raise ValueError("server_url must be a non-empty string")
        if not isinstance(state_file, Path):
            raise ValueError("state_file must be a pathlib.Path instance")
        if not isinstance(failures_threshold, int) or failures_threshold < 1:
            raise ValueError("failures_threshold must be an integer >= 1")
        if not isinstance(cooldown_seconds, int) or cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be an integer >= 0")
        if not isinstance(check_timeout_seconds, int) or check_timeout_seconds <= 0:
            raise ValueError("check_timeout_seconds must be an integer > 0")
        if not isinstance(service_name, str) or not service_name.strip():
            raise ValueError(
                "service_name must be a non-empty, non-whitespace-only string"
            )

        self.server_url = server_url
        self.state_file = state_file
        self.failures_threshold = failures_threshold
        self.cooldown_seconds = cooldown_seconds
        self.check_timeout_seconds = check_timeout_seconds
        self.service_name = service_name

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_once(self) -> None:
        """Execute one health-check iteration.

        Loads state, probes /health, updates consecutive_failures, and
        triggers a restart when the threshold is reached and cooldown has passed.
        """
        state = self._load_state()
        healthy = self._is_healthy()

        if healthy:
            if state["consecutive_failures"] > 0:
                logger.info(
                    "Server recovered after %d consecutive failures",
                    state["consecutive_failures"],
                )
            state["consecutive_failures"] = 0
            self._save_state(state)
            return

        state["consecutive_failures"] += 1
        logger.warning(
            "Health check failed (consecutive failures: %d)",
            state["consecutive_failures"],
        )

        if state["consecutive_failures"] >= self.failures_threshold:
            if self._is_cooldown_active(state):
                logger.warning(
                    "Restart suppressed: cooldown active (last restart: %s)",
                    state["last_restart_ts"],
                )
                self._save_state(state)
                return

            logger.error(
                "Failure threshold reached (%d), restarting %s",
                self.failures_threshold,
                self.service_name,
            )
            if self._restart_server():
                state["last_restart_ts"] = datetime.now(timezone.utc).isoformat()
                state["consecutive_failures"] = 0

        self._save_state(state)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_healthy(self) -> bool:
        """Return True if GET {server_url}/health responds with HTTP 200."""
        try:
            response = requests.get(
                f"{self.server_url}/health",
                timeout=self.check_timeout_seconds,
            )
            return response.status_code == 200
        except (requests.ConnectionError, requests.Timeout, requests.RequestException):
            return False

    def _is_cooldown_active(self, state: dict) -> bool:
        """Return True if a restart happened recently within the cooldown window.

        Logs a warning and returns False (no cooldown enforced) if the stored
        timestamp is present but cannot be parsed, so the operator is informed.
        """
        last_ts: Optional[str] = state.get("last_restart_ts")
        if last_ts is None:
            return False
        try:
            last_restart = datetime.fromisoformat(last_ts)
            if last_restart.tzinfo is None:
                last_restart = last_restart.replace(tzinfo=timezone.utc)
            elapsed = datetime.now(timezone.utc) - last_restart
            return elapsed < timedelta(seconds=self.cooldown_seconds)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "Cannot parse last_restart_ts %r; treating cooldown as inactive: %s",
                last_ts,
                exc,
            )
            return False

    def _restart_server(self) -> bool:
        """Invoke systemctl restart on the server service.

        Returns True if the command exited with code 0, False otherwise.
        """
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "restart", self.service_name],
                capture_output=True,
                text=True,
                timeout=_RESTART_SUBPROCESS_TIMEOUT,
            )
            if result.returncode == 0:
                logger.info("systemctl restart %s succeeded", self.service_name)
                return True
            logger.error(
                "systemctl restart %s failed (rc=%d): %s",
                self.service_name,
                result.returncode,
                result.stderr[:200],
            )
            return False
        except subprocess.TimeoutExpired:
            logger.error(
                "systemctl restart %s timed out after %ds",
                self.service_name,
                _RESTART_SUBPROCESS_TIMEOUT,
            )
            return False

    def _load_state(self) -> dict:
        """Load state from disk, returning defaults on any error."""
        defaults: dict = {"consecutive_failures": 0, "last_restart_ts": None}
        if not self.state_file.exists():
            return dict(defaults)
        try:
            raw = json.loads(self.state_file.read_text())
            failures = raw["consecutive_failures"]
            last_ts = raw["last_restart_ts"]
            if not isinstance(failures, int):
                raise TypeError("consecutive_failures must be int")
            if last_ts is not None and not isinstance(last_ts, str):
                raise TypeError("last_restart_ts must be str or null")
            return {"consecutive_failures": failures, "last_restart_ts": last_ts}
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning(
                "State file %s is corrupted; using defaults", self.state_file
            )
            return dict(defaults)

    def _save_state(self, state: dict) -> None:
        """Persist state dict to disk as JSON."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state))


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def _resolve_config(
    data_dir: Optional[str] = None,
) -> Tuple[str, Path, int, int, int, object]:
    """Resolve watchdog configuration from config.json.

    The service name to restart is read from the config attribute
    health_watchdog_service_name, falling back to HEALTH_WATCHDOG_SERVICE_NAME.
    The raw value is returned without coercion so the caller can narrow the type
    and let HealthWatchdog.__init__ enforce its validation contract.

    Returns:
        Tuple of (server_url, state_file, failures_threshold, cooldown_seconds,
        check_timeout_seconds, service_name). The service_name element is typed
        as object because it is returned raw from config; callers must narrow
        before passing to HealthWatchdog.

    Raises:
        RuntimeError: if config.json cannot be loaded.
    """
    from code_indexer.server.utils.config_manager import ServerConfigManager

    effective_dir = data_dir or os.environ.get(
        "CIDX_DATA_DIR", str(Path.home() / ".cidx-server")
    )
    cfg = ServerConfigManager(effective_dir).load_config()
    if cfg is None:
        raise RuntimeError(
            "health_watchdog: cannot resolve cidx-server URL — "
            "no config.json found at " + effective_dir
        )

    server_url = f"http://{cfg.host}:{cfg.port}"
    server_dir = Path(cfg.server_dir)
    state_file = server_dir / STATE_FILE_NAME

    failures_threshold = int(
        getattr(cfg, "health_watchdog_failures_threshold", DEFAULT_FAILURES_THRESHOLD)
    )
    cooldown_seconds = int(
        getattr(cfg, "health_watchdog_cooldown", DEFAULT_COOLDOWN_SECONDS)
    )
    check_timeout_seconds = int(
        getattr(cfg, "health_watchdog_check_timeout", DEFAULT_CHECK_TIMEOUT_SECONDS)
    )
    # Raw — no str() coercion — so the caller can narrow and HealthWatchdog
    # __init__ can validate type and content independently.
    service_name: object = getattr(
        cfg, "health_watchdog_service_name", HEALTH_WATCHDOG_SERVICE_NAME
    )

    return (
        server_url,
        state_file,
        failures_threshold,
        cooldown_seconds,
        check_timeout_seconds,
        service_name,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Execute one health-check iteration.

    Reads configuration from config.json, constructs a HealthWatchdog,
    and calls check_once(). Intended to be invoked by a systemd oneshot timer.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    try:
        (
            server_url,
            state_file,
            failures_threshold,
            cooldown_seconds,
            check_timeout,
            raw_service_name,
        ) = _resolve_config()
        # Narrow raw_service_name from object to str before construction.
        # HealthWatchdog.__init__ will reject empty/whitespace values; passing
        # the narrowed str lets __init__ enforce the full validation contract.
        if not isinstance(raw_service_name, str):
            raise ValueError(
                f"health_watchdog_service_name must be a string, "
                f"got {type(raw_service_name).__name__!r}"
            )
        wdog = HealthWatchdog(
            server_url=server_url,
            state_file=state_file,
            failures_threshold=failures_threshold,
            cooldown_seconds=cooldown_seconds,
            check_timeout_seconds=check_timeout,
            service_name=raw_service_name,
        )
    except (RuntimeError, ValueError) as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    wdog.check_once()


if __name__ == "__main__":
    main()
