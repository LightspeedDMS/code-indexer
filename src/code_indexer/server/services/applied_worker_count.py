"""Applied-worker-count resolver (Story #1197 AC5 / Bug #1239).
Story #1196 (next-release cleanup, FIX-1b): drops the config.json rung.

Reads the worker count the running uvicorn unit was ACTUALLY launched with
(APPLIED), never a saved-but-unapplied TARGET from the runtime DB.

Priority (post Story #1196 cleanup):
  1. Live systemd ExecStart --workers    — ground truth of the running process
       ExecStart found, has --workers N  -> return N
       ExecStart found, no --workers     -> return 1 (uvicorn default; this is the
                                           Bug #1239 first-deploy case)
       ExecStart unreadable / not found  -> fall through to Priority 2
  2. applied_launch.json["workers"]      — auto-updater-owned APPLIED file (Story 3)
  3. ServerConfig default: 1

Story #1197 originally added a config.json["workers"] rung between Priority 2
and the default (bootstrap fallback for pre-Story-3 / new nodes, while
config.json still carried the four launch keys via TRANSITION_PRESERVE_KEYS).
Story #1196 removes that rung in lockstep with config_service.py's removal of
the config.json launch-key copies: a node missing applied_launch.json now
falls straight to the ServerConfig default 1, never config.json.

Consumers: ProviderConcurrencyGovernor._read_config_workers()
           startup/service_init.py cache-init worker-count read

The resolver is:
  - DB-free  (reads only local files — safe before DB pool is wired)
  - Fail-soft (any error → falls through / returns 1; never raises)
  - Side-effect-free (pure reader, never writes)
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from code_indexer.server.auto_update.deployment_executor import (
    APPLIED_LAUNCH_CONFIG_PATH,
    DeploymentExecutor,
)

logger = logging.getLogger(__name__)

# MAJOR-M2 (Story #1198): derive the filename from the shared constant declared
# in deployment_executor.py so the auto-updater writer (Story 4) and this reader
# cannot diverge by independently hardcoding the same string in two places.
# The data_dir parameter is kept for test injection (callers can override the
# directory); the filename is always sourced from the shared constant.
_APPLIED_LAUNCH_FILENAME = APPLIED_LAUNCH_CONFIG_PATH.name


def _default_data_dir() -> Path:
    """Return the per-node data directory (mirrors deployment_executor._cidx_data_dir)."""
    return Path(os.environ.get("CIDX_DATA_DIR", str(Path.home() / ".cidx-server")))


def _default_unit_file() -> Path:
    """Return the systemd unit file path for cidx-server (evaluated at call time).

    Reads SYSTEMD_UNIT_DIR env var at call time (not import time) so the value
    is always fresh and test injection via the unit_file parameter is the
    preferred seam rather than module reload.
    """
    unit_dir = Path(os.environ.get("SYSTEMD_UNIT_DIR", "/etc/systemd/system"))
    return unit_dir / "cidx-server.service"


def _read_workers_from_execstart(unit_file: Path) -> Optional[int]:
    """Read the applied worker count from the live systemd ExecStart line.

    Reuses DeploymentExecutor._is_cidx_execstart (detection predicate) and
    DeploymentExecutor._read_flag (bounded-token extraction) — exactly one
    ExecStart parser in the codebase (Bug #1239 fix).

    Returns:
        None  — fall through: file missing, read error, or no cidx ExecStart found.
        1     — ExecStart found but no --workers token; uvicorn default = 1 worker.
                This is the Bug #1239 first-deploy case: the unit has no --workers
                flag (10.141.0 -> v11 first deploy), so uvicorn runs ONE worker even
                though config.json may say workers=4.
        N     — ExecStart found with --workers N; N coerced to int.

    Never raises (fail-soft).
    """
    try:
        if not unit_file.exists():
            return None
        lines = unit_file.read_text().split("\n")
    except Exception as exc:
        logger.debug(
            "applied_worker_count: could not read %s (%s); skipping ExecStart priority",
            unit_file,
            exc,
        )
        return None

    for line in lines:
        if not DeploymentExecutor._is_cidx_execstart(line):
            continue
        # Found the cidx ExecStart line.
        workers_str = DeploymentExecutor._read_flag(line, "--workers")
        if workers_str is None:
            # ExecStart exists but carries no --workers token.
            # Uvicorn launched with its default of 1 worker — this IS the ground truth.
            logger.debug(
                "applied_worker_count: ExecStart found but no --workers token; "
                "uvicorn default = 1 worker (Bug #1239 first-deploy case)"
            )
            return 1
        try:
            return int(workers_str)
        except ValueError:
            logger.debug(
                "applied_worker_count: --workers value %r is not an int; "
                "treating as 1 (uvicorn default)",
                workers_str,
            )
            return 1

    # File was readable but contained no cidx ExecStart line — fall through.
    return None


def _read_workers_from_applied_launch(data_dir: Path) -> Optional[int]:
    """Read workers from applied_launch.json; returns None on any problem."""
    path = data_dir / _APPLIED_LAUNCH_FILENAME
    try:
        if not path.exists():
            return None
        with open(path) as f:
            data = json.load(f)
        value = data.get("workers")
        if not isinstance(value, int):
            logger.debug(
                "applied_worker_count: applied_launch.json workers is not an int (%r); "
                "falling back to the ServerConfig default",
                value,
            )
            return None
        return value
    except Exception as exc:
        logger.debug(
            "applied_worker_count: could not read %s (%s); falling back to the "
            "ServerConfig default",
            path,
            exc,
        )
        return None


def get_applied_worker_count(
    data_dir: Optional[str] = None,
    unit_file: Optional[Path] = None,
) -> int:
    """Return the APPLIED worker count for this node.

    The APPLIED count is the count the running uvicorn process was actually
    launched with — this differs from get_config().workers (the TARGET, which
    may have been saved but not yet restarted into effect).

    Story #1196 (FIX-1b): the Story #1197 config.json rung has been removed
    now that config.json no longer carries a launch-key copy of 'workers'
    (config_service.py AC1). A node missing applied_launch.json falls
    straight to the ServerConfig default 1.

    Args:
        data_dir:   Path to the cidx data directory (default: CIDX_DATA_DIR env
                    or ~/.cidx-server). Must contain applied_launch.json when
                    authored by the auto-updater (Story 3).
        unit_file:  Path to the systemd unit file (default: SYSTEMD_UNIT_DIR /
                    "cidx-server.service"). Override in tests to inject a fake
                    unit file without touching the filesystem at /etc/systemd/.

    Returns:
        Applied worker count >= 1. Never 0, never negative, never raises.
    """
    _data_dir = Path(data_dir) if data_dir is not None else _default_data_dir()
    _unit_file = unit_file if unit_file is not None else _default_unit_file()

    # Priority 1: live systemd ExecStart --workers (Bug #1239 fix).
    # Ground truth of the actually-running process:
    #   - ExecStart found with --workers N -> return N
    #   - ExecStart found, no --workers   -> return 1 (uvicorn default; first-deploy case)
    #   - ExecStart unreadable / absent   -> fall through
    value = _read_workers_from_execstart(_unit_file)
    if value is not None:
        return max(1, value)

    # Priority 2: applied_launch.json (APPLIED — auto-updater-owned, Story 3)
    value = _read_workers_from_applied_launch(_data_dir)
    if value is not None:
        return max(1, value)

    # Priority 3: default (Story #1196 removed the config.json rung)
    logger.debug("applied_worker_count: no source found; using default worker_count=1")
    return 1
