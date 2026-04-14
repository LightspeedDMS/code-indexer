"""Config seeding helper (Bug #678).

Overlays server-side provider config onto CLI subprocess config.json
before each cidx index launch. Server values always win.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Surface 1 keys: seeded to CLI config.json. NO sinbin, NO orchestration keys.
SEEDED_KEYS: List[str] = [
    "voyage_ai.timeout",
    "voyage_ai.connect_timeout",
    "voyage_ai.max_retries",
    "voyage_ai.retry_delay",
    "voyage_ai.exponential_backoff",
    "voyage_ai.parallel_requests",
    "voyage_ai.reranker_timeout",
    "voyage_ai.reranker_connect_timeout",
    "voyage_ai.health_monitor.rolling_window_minutes",
    "voyage_ai.health_monitor.down_consecutive_failures",
    "voyage_ai.health_monitor.down_error_rate",
    "voyage_ai.health_monitor.degraded_error_rate",
    "voyage_ai.health_monitor.latency_p95_threshold_ms",
    "voyage_ai.health_monitor.availability_threshold",
    "cohere.timeout",
    "cohere.connect_timeout",
    "cohere.max_retries",
    "cohere.retry_delay",
    "cohere.exponential_backoff",
    "cohere.parallel_requests",
    "cohere.reranker_timeout",
    "cohere.reranker_connect_timeout",
    "cohere.health_monitor.rolling_window_minutes",
    "cohere.health_monitor.down_consecutive_failures",
    "cohere.health_monitor.down_error_rate",
    "cohere.health_monitor.degraded_error_rate",
    "cohere.health_monitor.latency_p95_threshold_ms",
    "cohere.health_monitor.availability_threshold",
]


def seed_provider_config(repo_path: str) -> None:
    """Overlay server provider config onto CLI config.json. No-op if file absent."""
    config_file = Path(repo_path) / ".code-indexer" / "config.json"
    if not config_file.exists():
        return

    try:
        server_values = _get_server_provider_values()
    except Exception as exc:
        logger.debug("Config seeding: could not read server config: %s", exc)
        return

    try:
        with open(config_file, "r") as f:
            disk_config = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Config seeding: could not read %s: %s", config_file, exc)
        return

    for dot_path in SEEDED_KEYS:
        value = _resolve_dot_path(server_values, dot_path)
        if value is not None:
            _set_dot_path(disk_config, dot_path, value)

    _atomic_write(config_file, disk_config)


def _atomic_write(config_file: Path, data: Dict[str, Any]) -> None:
    """Write data to config_file atomically via a temp file + rename."""
    parent_dir = config_file.parent
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(parent_dir), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, str(config_file))
    except Exception as exc:
        logger.warning("Config seeding: atomic write failed: %s", exc)
        try:
            os.unlink(tmp_name)
        except Exception as cleanup_exc:
            logger.debug("Config seeding: cleanup of temp file failed: %s", cleanup_exc)


def _get_server_provider_values() -> Dict[str, Any]:
    """Get provider config values from server runtime config or Pydantic defaults.

    Starts with Pydantic model defaults (which are the canonical baseline), then
    attempts to overlay any provider-specific overrides from the live server config.
    """
    from code_indexer.config import CohereConfig, VoyageAIConfig

    voyage = VoyageAIConfig()
    cohere_cfg = CohereConfig()
    result: Dict[str, Any] = {
        "voyage_ai": json.loads(voyage.model_dump_json()),
        "cohere": json.loads(cohere_cfg.model_dump_json()),
    }

    # Try to overlay with live server config if available
    try:
        from code_indexer.server.services.config_service import get_config_service

        server_cfg = get_config_service().get_config()
        indexing = getattr(server_cfg, "indexing_config", None)
        if indexing is not None:
            for attr in ("voyage_ai_timeout", "cohere_timeout"):
                val = getattr(indexing, attr, None)
                if val is not None:
                    provider = "voyage_ai" if "voyage" in attr else "cohere"
                    result[provider]["timeout"] = val
    except Exception as exc:
        logger.debug(
            "Config seeding: server config unavailable, using defaults: %s", exc
        )

    return result


def _resolve_dot_path(data: Dict[str, Any], dot_path: str) -> Any:
    """Resolve a dot-separated path like 'voyage_ai.timeout' in nested dict."""
    keys = dot_path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current


def _set_dot_path(data: Dict[str, Any], dot_path: str, value: Any) -> None:
    """Set a value at a dot-separated path, creating intermediate dicts as needed."""
    keys = dot_path.split(".")
    current = data
    for key in keys[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value
