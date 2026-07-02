"""Tests for Phase B (Story #883) — 5 memory retrieval runtime config keys.

Component 13 of 14:
  5 runtime config keys accessible via ConfigService:
    - memory_retrieval_enabled (bool, default True)
    - memory_voyage_min_score (float, default 0.5)
    - memory_cohere_min_score (float, default 0.4)
    - memory_retrieval_k_multiplier (int, default 5)
    - memory_retrieval_max_body_chars (int, default 2000)

  All runtime (DB-backed, Story #578 bootstrap-vs-runtime rule).
  NOT added to config.json bootstrap.

TDD: these tests are written BEFORE the implementation.
"""

import pytest


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
    """get_all_settings must contain a 'memory_retrieval' section."""
    settings = config_service.get_all_settings()
    assert "memory_retrieval" in settings


@pytest.mark.parametrize("key,default,_updated,_type", _KEY_SPECS, ids=_IDS)
def test_memory_retrieval_default(config_service, key, default, _updated, _type):
    """Each key's default value must be exposed in get_all_settings."""
    settings = config_service.get_all_settings()
    result = settings["memory_retrieval"][key]
    if isinstance(default, float):
        assert result == pytest.approx(default)
    else:
        assert result == default


@pytest.mark.parametrize("key,_default,updated,_type", _KEY_SPECS, ids=_IDS)
def test_memory_retrieval_update_roundtrip(
    config_service, key, _default, updated, _type
):
    """update_setting roundtrip: each key can be set and immediately read back."""
    config_service.update_setting("memory_retrieval", key, updated)
    settings = config_service.get_all_settings()
    result = settings["memory_retrieval"][key]
    if isinstance(updated, float):
        assert result == pytest.approx(updated)
    else:
        assert result == updated


def test_memory_retrieval_unknown_key_raises(config_service):
    """Unknown key in memory_retrieval category raises ValueError."""
    with pytest.raises(ValueError, match="Unknown memory_retrieval setting"):
        config_service.update_setting("memory_retrieval", "nonexistent_key", "val")


@pytest.mark.parametrize("key,default,_updated,expected_type", _KEY_SPECS, ids=_IDS)
def test_memory_retrieval_config_field_type(
    config_service, key, default, _updated, expected_type
):
    """Each memory_retrieval_config field on get_config() must be the declared type."""
    config = config_service.get_config()
    assert config.memory_retrieval_config is not None
    value = getattr(config.memory_retrieval_config, key)
    assert isinstance(value, expected_type), (
        f"Expected {key} to be {expected_type.__name__}, got {type(value).__name__}"
    )
