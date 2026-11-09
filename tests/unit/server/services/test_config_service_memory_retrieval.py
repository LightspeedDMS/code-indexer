"""Tests for Phase B (Story #883) — 5 memory retrieval runtime config keys.

Component 13 of 14:
  5 runtime config keys accessible via ConfigService:
    - memory_retrieval_enabled (bool, default True)
    - memory_voyage_min_score (float, default 0.5)
    - memory_cohere_min_score (float, default 0.4)
    - memory_retrieval_k_multiplier (int, default 5)
    - memory_retrieval_max_body_chars (int, default 2000)

  All runtime (DB-backed, Story #578 bootstrap-vs-runtime rule).
  NOT added to config.json bootstrap. Since modular phase 4f they are the
  cidx-memory feature's config section (``feature_settings["memory"]``),
  listed and updated through the feature config door, as every feature
  setting is.

TDD: these tests are written BEFORE the implementation.
"""

from typing import Any, Dict

import pytest

from cidx_memory.config import MEMORY_CONFIG_SECTION
from code_indexer.server.features import config as feature_config


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def config_service(tmp_path):
    """Build a ConfigService backed by a temp dir (no real DB/disk entanglement)."""
    from code_indexer.server.services.config_service import ConfigService

    server_dir = tmp_path / "cidx-server"
    server_dir.mkdir()
    return ConfigService(server_dir_path=str(server_dir))


def _listed(config_service: Any) -> Dict[str, Any]:
    """The memory section's values as the config API and screen list them."""
    (section,) = feature_config.describe_sections(
        [("memory", MEMORY_CONFIG_SECTION)], config_service.get_config()
    )
    return {key["name"]: key["value"] for key in section["keys"]}


def _set(config_service: Any, key: str, value: Any) -> None:
    feature_config.commit_runtime_update(
        config_service, "memory", MEMORY_CONFIG_SECTION, {key: value}, "admin"
    )


# ---------------------------------------------------------------------------
# Parametrized test data shared across tests
# ---------------------------------------------------------------------------

# (key, default_value, updated_value, expected_type)
_KEY_SPECS = [
    ("memory_retrieval_enabled", True, False, bool),
    ("memory_voyage_min_score", 0.5, 0.7, float),
    ("memory_cohere_min_score", 0.4, 0.6, float),
    ("memory_retrieval_k_multiplier", 5, 10, int),
    ("memory_retrieval_max_body_chars", 2000, 5000, int),
]

_IDS = [s[0] for s in _KEY_SPECS]


# ---------------------------------------------------------------------------
# Tests (exactly 5 test functions matching the declared 5 areas)
# ---------------------------------------------------------------------------


def test_memory_retrieval_section_exists(config_service):
    """The config API / screen lists the five keys."""
    assert set(_listed(config_service)) == {spec[0] for spec in _KEY_SPECS}


@pytest.mark.parametrize("key,default,_updated,_type", _KEY_SPECS, ids=_IDS)
def test_memory_retrieval_default(config_service, key, default, _updated, _type):
    """Each key's default value is listed."""
    result = _listed(config_service)[key]
    if isinstance(default, float):
        assert result == pytest.approx(default)
    else:
        assert result == default


@pytest.mark.parametrize("key,_default,updated,_type", _KEY_SPECS, ids=_IDS)
def test_memory_retrieval_update_roundtrip(
    config_service, key, _default, updated, _type
):
    """Each key can be set and immediately read back."""
    _set(config_service, key, updated)
    result = _listed(config_service)[key]
    if isinstance(updated, float):
        assert result == pytest.approx(updated)
    else:
        assert result == updated


def test_memory_retrieval_unknown_key_raises(config_service):
    """An unknown key is refused."""
    with pytest.raises(feature_config.FeatureConfigError, match="no setting"):
        _set(config_service, "nonexistent_key", "val")


@pytest.mark.parametrize("key,default,_updated,expected_type", _KEY_SPECS, ids=_IDS)
def test_memory_retrieval_config_field_type(
    config_service, key, default, _updated, expected_type
):
    """Each key of the feature's section on get_config() is the declared type
    once updated."""
    _set(config_service, key, _updated)
    config = config_service.get_config()
    value = config.feature_settings["memory"][key]
    assert isinstance(value, expected_type), (
        f"Expected {key} to be {expected_type.__name__}, got {type(value).__name__}"
    )
