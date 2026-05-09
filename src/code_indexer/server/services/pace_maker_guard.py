"""
Pre-invocation guard for pace-maker configuration enforcement (Story #997).

Called before every Claude CLI invocation to idempotently enforce pace-maker
configuration based on the runtime toggle. Dev environments are protected
via location awareness (clone path check).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

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
    ["pace-maker", "cross-session-awareness", "off"],
    ["pace-maker", "memory-localization", "off"],
    ["pace-maker", "danger-bash", "off"],
]


def _get_clone_path_from_bootstrap() -> Optional[str]:
    """Read pace_maker_clone_path from bootstrap config."""
    data_dir = Path(os.environ.get("CIDX_DATA_DIR", str(Path.home() / ".cidx-server")))
    config_path = data_dir / "config.json"
    if not config_path.exists():
        return None
    import json

    with open(config_path) as f:
        config = json.load(f)
    value = config.get("pace_maker_clone_path")
    return str(value) if value is not None else None


def _get_enforce_toggle() -> bool:
    """Read enforce_pace_maker_pacing_only from runtime config."""
    from code_indexer.server.services.config_service import get_config_service

    config = get_config_service().get_config()
    return bool(getattr(config, "enforce_pace_maker_pacing_only", False))


def _run_pace_maker_status() -> Optional[str]:
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
        any(expected in line for line in lines)
        for expected in _PACING_ONLY_EXPECTED
    )


def enforce_pace_maker_config() -> None:
    """Enforce pace-maker configuration before a Claude CLI invocation.

    Three-layer safety model:
    1. Location awareness: clone path from bootstrap config must exist
    2. Runtime toggle: read from Web UI config
    3. Idempotent CLI enforcement: check status, correct if drifted

    This function NEVER raises -- all failures are logged and silently ignored.
    """
    try:
        # Step 1: Location awareness -- dev environment protection
        clone_path = _get_clone_path_from_bootstrap()
        if clone_path is None or not Path(clone_path).exists():
            return

        # Step 2: Read runtime config toggle
        enforce = _get_enforce_toggle()

        # Step 3: Check CLI availability
        if shutil.which("pace-maker") is None:
            return

        # Step 4: Enforce based on toggle
        if enforce:
            status_output = _run_pace_maker_status()
            if status_output is None:
                return
            if not _check_pacing_only_status(status_output):
                for cmd in _PACING_ONLY_COMMANDS:
                    _run_pace_maker_command(cmd)
                logger.warning("pace-maker config drift corrected to pacing-only")
        else:
            status_output = _run_pace_maker_status()
            if status_output is None:
                return
            if "Pace Maker: ACTIVE" in status_output:
                _run_pace_maker_command(["pace-maker", "off"])
                logger.info(
                    "pace-maker master switch set to OFF "
                    "(enforce_pace_maker_pacing_only=false)"
                )
    except Exception as exc:
        logger.debug("enforce_pace_maker_config failed: %s", exc)
