"""
Pre-invocation guard for pace-maker configuration enforcement (Story #997).

Called before every Claude CLI invocation to idempotently enforce pace-maker
configuration based on the runtime mode setting.

Three modes:
  "disabled" - no-op, never touch pace-maker (safe for dev machines)
  "on"        - enforce pacing-only mode (5h + weekly limits active)
  "off"       - actively disable pace-maker master switch
"""

from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

_PACE_MAKER_STATUS_TIMEOUT = 5
_PACE_MAKER_CMD_TIMEOUT = 5

# Expected status output lines when pacing-only mode is correctly configured
_PACING_ONLY_EXPECTED = {
    "Pace Maker: ACTIVE",
    "5-Hour Limit: ENABLED",
    "Weekly Limit: ENABLED",
    "Tempo Mode: OFF",
    "Intent Validation: DISABLED",
    "TDD Enforcement: DISABLED",
    "Subagent Reminder: DISABLED",
    "Langfuse: DISABLED",
    "Danger Bash: DISABLED",
    "Memory Localization: DISABLED",
}

_PACING_ONLY_COMMANDS = [
    ["pace-maker", "on"],
    ["pace-maker", "5-hour-limit", "on"],
    ["pace-maker", "weekly-limit", "on"],
    ["pace-maker", "tempo", "off"],
    ["pace-maker", "intent-validation", "off"],
    ["pace-maker", "tdd", "off"],
    ["pace-maker", "reminder", "off"],
    ["pace-maker", "langfuse", "off"],
    ["pace-maker", "memory-localization", "off"],
    ["pace-maker", "danger-bash", "off"],
]


def _get_pace_maker_mode() -> str:
    """Read pace_maker_mode from runtime config. Returns 'disabled' on any error."""
    from code_indexer.server.services.config_service import get_config_service

    config = get_config_service().get_config()
    return str(getattr(config, "pace_maker_mode", "disabled"))


def _run_pace_maker_status() -> str | None:
    """Run 'pace-maker status' and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["pace-maker", "status"],
            capture_output=True,
            text=True,
            timeout=_PACE_MAKER_STATUS_TIMEOUT,
        )
        if result.returncode != 0:
            logger.debug(
                "pace-maker status returned non-zero exit %d", result.returncode
            )
            return None
        return result.stdout
    except Exception as exc:
        logger.debug("pace-maker status failed: %s", exc)
        return None


def _run_pace_maker_command(cmd: list) -> bool:
    """Run a pace-maker CLI command. Returns True on success."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_PACE_MAKER_CMD_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning(
                "pace-maker command %s exited with code %d", cmd, result.returncode
            )
            return False
        return True
    except Exception as exc:
        logger.warning("pace-maker command %s failed: %s", cmd, exc)
        return False


def _check_pacing_only_status(status_output: str) -> bool:
    """Check if status output matches expected pacing-only configuration.

    Uses substring matching so that lines with parenthetical suffixes
    (e.g. "Weekly Limit: ENABLED (5x:0.001 20x:0.0003)") still match
    expected prefixes like "Weekly Limit: ENABLED".
    """
    lines = [line.strip() for line in status_output.splitlines()]
    return all(
        any(expected in line for line in lines) for expected in _PACING_ONLY_EXPECTED
    )


def enforce_pace_maker_config() -> None:
    """Enforce pace-maker configuration before a Claude CLI invocation.

    Mode-based dispatch:
      "disabled" -> return immediately, never touch pace-maker
      "on"       -> ensure pacing-only mode is active; correct drift if needed
      "off"      -> ensure pace-maker master switch is OFF

    This function NEVER raises -- all failures are logged and silently ignored.
    """
    try:
        mode = _get_pace_maker_mode()

        if mode == "disabled":
            return

        # Check CLI availability before any subprocess work
        if shutil.which("pace-maker") is None:
            return

        if mode == "on":
            status_output = _run_pace_maker_status()
            if status_output is None:
                return
            if not _check_pacing_only_status(status_output):
                for cmd in _PACING_ONLY_COMMANDS:
                    _run_pace_maker_command(cmd)
                logger.warning("pace-maker config drift corrected to pacing-only")
        elif mode == "off":
            status_output = _run_pace_maker_status()
            if status_output is None:
                return
            if "Pace Maker: ACTIVE" in status_output:
                _run_pace_maker_command(["pace-maker", "off"])
                logger.info("pace-maker master switch set to OFF (pace_maker_mode=off)")
    except Exception as exc:
        logger.debug("enforce_pace_maker_config failed: %s", exc)
