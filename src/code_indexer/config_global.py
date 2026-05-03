"""Global CLI configuration loader (Story #690 — Epic #689).

Provides a user-scoped, hand-editable configuration store for CLI reranker
settings at ~/.config/cidx/global.json (XDG-compliant).

Path resolution order (first match wins):
  1. CIDX_GLOBAL_CONFIG_PATH env var (for test isolation and explicit overrides)
  2. $XDG_CONFIG_HOME/cidx/global.json
  3. ~/.config/cidx/global.json  (XDG fallback when XDG_CONFIG_HOME unset)

The file is auto-seeded with defaults on first access.  On parse errors the
loader raises ValueError with the file path included — no silent fallback to
defaults (Messi Rule 02 Anti-Fallback).  The per-project .code-indexer/
config.json is never read or written by this module.
"""

import json
import os
from pathlib import Path
from typing import List, cast

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class RerankSettings(BaseModel):
    """Reranker preferences stored in the global CLI config."""

    auto_populate_rerank_query: bool = True
    cohere_reranker_model: str = "rerank-v3.5"
    overfetch_multiplier: int = 5
    preferred_vendor_order: List[str] = ["voyage", "cohere"]
    voyage_reranker_model: str = "rerank-2.5"


class GlobalCliConfig(BaseModel):
    """Root schema for ~/.config/cidx/global.json."""

    rerank: RerankSettings = RerankSettings()


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _resolve_config_path() -> Path:
    """Return the resolved path to the global config file.

    Checks CIDX_GLOBAL_CONFIG_PATH first (test isolation), then
    XDG_CONFIG_HOME, then falls back to ~/.config/cidx/global.json.
    """
    explicit = os.environ.get("CIDX_GLOBAL_CONFIG_PATH")
    if explicit:
        return Path(explicit)

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        base = Path(xdg_config_home)
    else:
        base = Path.home() / ".config"

    return base / "cidx" / "global.json"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_global_config() -> GlobalCliConfig:
    """Load and return the global CLI config.

    Behavior:
      - If the file does not exist: seed it with defaults and return them.
      - If the file exists: parse and validate it.
      - On JSON parse error: raise ValueError naming the file path and the
        parse error (no silent fallback — Messi Rule 02).
      - On schema validation error: propagate the pydantic ValidationError.

    Returns:
        GlobalCliConfig instance with validated settings.

    Raises:
        ValueError: If the file contains malformed JSON.
        pydantic.ValidationError: If the JSON does not match the schema.
    """
    path = _resolve_config_path()

    if not path.exists():
        cfg = GlobalCliConfig()
        _write_sorted_json(path, cfg.model_dump())
        return cfg

    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Global CLI config at {path} contains malformed JSON: {exc}"
        ) from exc

    # Pydantic's model_validate returns Self at runtime (always GlobalCliConfig
    # here), but mypy interprets the Self return type as Any. cast() narrows
    # without changing runtime behavior.
    return cast(GlobalCliConfig, GlobalCliConfig.model_validate(data))


def save_global_config(cfg: GlobalCliConfig) -> None:
    """Persist cfg to the global config file with sorted, indented JSON.

    Args:
        cfg: The GlobalCliConfig instance to persist.
    """
    path = _resolve_config_path()
    _write_sorted_json(path, cfg.model_dump())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write_sorted_json(path: Path, data: object) -> None:
    """Write data as indented, sorted JSON to path, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
