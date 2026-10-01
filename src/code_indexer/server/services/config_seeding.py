"""Config seeding helper (Bug #678).

Overlays server-side provider config onto CLI subprocess config.json
before each cidx index launch. Server values always win.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from code_indexer.config import write_json_atomic

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
    # Story #1290: per-commit temporal embedder registry (Web UI Config Screen).
    "temporal.embedders",
    "temporal.active_embedder",
    "temporal.aggregation_chunk_chars",
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

    # Story #1158 - AC2: Write temporal keys unconditionally — null must propagate.
    # These are NOT in SEEDED_KEYS because the None-filter loop above would swallow None.
    for dot_path in (
        "voyage_ai.temporal_parallel_requests",
        "cohere.temporal_parallel_requests",
    ):
        temporal_val = _resolve_dot_path(server_values, dot_path)
        _set_dot_path(disk_config, dot_path, temporal_val)

    try:
        write_json_atomic(config_file, disk_config, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Config seeding: atomic write failed: %s", exc)


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
        # Story #1290: per-commit temporal embedder registry defaults
        # (overlaid with live server values below, same as voyage_ai/cohere).
        "temporal": {
            "embedders": ["voyage-context-4"],
            "active_embedder": "voyage-context-4",
            "aggregation_chunk_chars": 4096,
        },
    }

    # Try to overlay with live server config if available
    try:
        from code_indexer.server.services.config_service import get_config_service

        server_cfg = get_config_service().get_config()
        indexing = getattr(server_cfg, "indexing_config", None)
        if indexing is not None:
            # Overlay timeout fields (pre-existing)
            for attr in ("voyage_ai_timeout", "cohere_timeout"):
                val = getattr(indexing, attr, None)
                if val is not None:
                    provider = "voyage_ai" if "voyage" in attr else "cohere"
                    result[provider]["timeout"] = val

            # Story #1158 - AC1: Overlay embedding parallelism when set
            for field_name, provider_key in (
                ("voyage_ai_parallel_requests", "voyage_ai"),
                ("cohere_parallel_requests", "cohere"),
            ):
                val = getattr(indexing, field_name, None)
                if val is not None:
                    result[provider_key]["parallel_requests"] = val

            # Story #1158 - AC2: Propagate temporal parallelism unconditionally
            # None must reach config.json as null — do NOT skip None here
            temporal_val = getattr(indexing, "temporal_parallel_requests", None)
            result["voyage_ai"]["temporal_parallel_requests"] = temporal_val
            result["cohere"]["temporal_parallel_requests"] = temporal_val

            # Story #1290: overlay per-commit temporal embedder registry
            # (embedders/active_embedder/aggregation_chunk_chars) when set.
            embedders_val = getattr(indexing, "temporal_embedders", None)
            if embedders_val:
                result["temporal"]["embedders"] = list(embedders_val)
            active_embedder_val = getattr(indexing, "temporal_active_embedder", None)
            if active_embedder_val:
                result["temporal"]["active_embedder"] = active_embedder_val
            chunk_chars_val = getattr(
                indexing, "temporal_aggregation_chunk_chars", None
            )
            if chunk_chars_val:
                result["temporal"]["aggregation_chunk_chars"] = chunk_chars_val
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
