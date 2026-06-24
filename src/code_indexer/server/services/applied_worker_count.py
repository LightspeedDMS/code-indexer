"""Applied-worker-count resolver (Story #1197 AC5 / CRITICAL-C2).

Reads the worker count the running uvicorn unit was ACTUALLY launched with
(APPLIED), never a saved-but-unapplied TARGET from the runtime DB.

Priority:
  1. applied_launch.json["workers"]  — auto-updater-owned APPLIED file (Story 3)
  2. config.json["workers"]          — bootstrap fallback (pre-Story-3 / new node)
  3. ServerConfig default: 1

Consumers: ProviderConcurrencyGovernor._read_config_workers()
           startup/service_init.py cache-init worker-count read

Pre-Story-3 behaviour: applied_launch.json does NOT exist yet (Story 3 authors
it). The resolver falls back to config.json which is correct — that file still
carries the four launch keys via the TRANSITION_PRESERVE_KEYS mechanism (AC3/AC6).

The resolver is:
  - DB-free  (reads only local files — safe before DB pool is wired)
  - Fail-soft (any error → returns 1 via max(1, value) discipline)
  - Side-effect-free (pure reader, never writes)
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from code_indexer.server.auto_update.deployment_executor import (
    APPLIED_LAUNCH_CONFIG_PATH,
)

logger = logging.getLogger(__name__)

# MAJOR-M2 (Story #1198): derive the filename from the shared constant declared
# in deployment_executor.py so the auto-updater writer (Story 4) and this reader
# cannot diverge by independently hardcoding the same string in two places.
# The data_dir parameter is kept for test injection (callers can override the
# directory); the filename is always sourced from the shared constant.
_APPLIED_LAUNCH_FILENAME = APPLIED_LAUNCH_CONFIG_PATH.name
_CONFIG_FILENAME = "config.json"


def _default_data_dir() -> Path:
    """Return the per-node data directory (mirrors deployment_executor._cidx_data_dir)."""
    return Path(os.environ.get("CIDX_DATA_DIR", str(Path.home() / ".cidx-server")))


def _default_config_dir() -> Path:
    """Return the directory that contains config.json (same as data dir by convention)."""
    return _default_data_dir()


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
                "falling back to config.json",
                value,
            )
            return None
        return value
    except Exception as exc:
        logger.debug(
            "applied_worker_count: could not read %s (%s); falling back to config.json",
            path,
            exc,
        )
        return None


def _read_workers_from_config_json(config_dir: Path) -> Optional[int]:
    """Read workers from config.json; returns None on any problem."""
    path = config_dir / _CONFIG_FILENAME
    try:
        if not path.exists():
            return None
        with open(path) as f:
            data = json.load(f)
        value = data.get("workers")
        if not isinstance(value, int):
            logger.debug(
                "applied_worker_count: config.json workers is not an int (%r); "
                "falling back to default 1",
                value,
            )
            return None
        return value
    except Exception as exc:
        logger.debug(
            "applied_worker_count: could not read %s (%s); falling back to default 1",
            path,
            exc,
        )
        return None


def get_applied_worker_count(
    data_dir: Optional[str] = None,
    config_dir: Optional[str] = None,
) -> int:
    """Return the APPLIED worker count for this node.

    The APPLIED count is the count the running uvicorn process was actually
    launched with — this differs from get_config().workers (the TARGET, which
    may have been saved but not yet restarted into effect).

    Args:
        data_dir:   Path to the cidx data directory (default: CIDX_DATA_DIR env
                    or ~/.cidx-server). Must contain applied_launch.json when
                    authored by the auto-updater (Story 3).
        config_dir: Path to the directory containing config.json (default: same
                    as data_dir). The transition-preserved config.json carries
                    the workers key via TRANSITION_PRESERVE_KEYS (AC3/AC6).

    Returns:
        Applied worker count >= 1. Never 0, never negative, never raises.
    """
    _data_dir = Path(data_dir) if data_dir is not None else _default_data_dir()
    _config_dir = Path(config_dir) if config_dir is not None else _default_config_dir()

    # Priority 1: applied_launch.json (APPLIED — auto-updater-owned, Story 3)
    value = _read_workers_from_applied_launch(_data_dir)
    if value is not None:
        return max(1, value)

    # Priority 2: config.json workers (bootstrap fallback — always present via
    # TRANSITION_PRESERVE_KEYS even after AC1 removes workers from BOOTSTRAP_KEYS)
    value = _read_workers_from_config_json(_config_dir)
    if value is not None:
        return max(1, value)

    # Priority 3: default
    logger.debug("applied_worker_count: no source found; using default worker_count=1")
    return 1
