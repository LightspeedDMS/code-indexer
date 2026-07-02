"""
Tests for elevation enforcement settings in ServerConfig (Story #923 AC5, Codex M12).

Tests cover: default values, non-default construction, asdict() serialization,
dict round-trip, exclusion from BOOTSTRAP_KEYS, and runtime dict extraction.

These tests FAIL until Critical Fix 1 is applied to config_manager.py.
"""

from dataclasses import asdict


def _make_config(tmp_path, **overrides):
    """Helper: construct a minimal ServerConfig with optional overrides."""
    from code_indexer.server.utils.config_manager import ServerConfig

    return ServerConfig(server_dir=str(tmp_path), **overrides)


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


def test_elevation_enforcement_enabled_default_is_false(tmp_path):
    """elevation_enforcement_enabled must default to False (kill-switch closed)."""
    config = _make_config(tmp_path)
    assert config.elevation_enforcement_enabled is False


def test_elevation_idle_timeout_seconds_default(tmp_path):
    """elevation_idle_timeout_seconds must default to 300 (5 minutes)."""
    config = _make_config(tmp_path)
    assert config.elevation_idle_timeout_seconds == 300


def test_elevation_max_age_seconds_default(tmp_path):
    """elevation_max_age_seconds must default to 1800 (30 minutes)."""
    config = _make_config(tmp_path)
    assert config.elevation_max_age_seconds == 1800


# ---------------------------------------------------------------------------
# Non-default construction
# ---------------------------------------------------------------------------


def test_elevation_enforcement_enabled_can_be_set_true(tmp_path):
    """elevation_enforcement_enabled can be overridden to True at construction."""
    config = _make_config(tmp_path, elevation_enforcement_enabled=True)
    assert config.elevation_enforcement_enabled is True


def test_elevation_idle_timeout_seconds_can_be_overridden(tmp_path):
    """elevation_idle_timeout_seconds can be set to a custom value at construction."""
    config = _make_config(tmp_path, elevation_idle_timeout_seconds=600)
    assert config.elevation_idle_timeout_seconds == 600


def test_elevation_max_age_seconds_can_be_overridden(tmp_path):
    """elevation_max_age_seconds can be set to a custom value at construction."""
    config = _make_config(tmp_path, elevation_max_age_seconds=3600)
    assert config.elevation_max_age_seconds == 3600


# ---------------------------------------------------------------------------
# Dict serialization and round-trip
# ---------------------------------------------------------------------------


def test_elevation_fields_present_in_asdict(tmp_path):
    """All three elevation fields must appear in asdict() output with correct values."""
    config = _make_config(
        tmp_path,
        elevation_enforcement_enabled=True,
        elevation_idle_timeout_seconds=120,
        elevation_max_age_seconds=900,
    )
    d = asdict(config)
    assert "elevation_enforcement_enabled" in d
    assert d["elevation_enforcement_enabled"] is True
    assert "elevation_idle_timeout_seconds" in d
    assert d["elevation_idle_timeout_seconds"] == 120
    assert "elevation_max_age_seconds" in d
    assert d["elevation_max_age_seconds"] == 900


def test_elevation_fields_round_trip_through_dict(tmp_path):
    """Elevation fields survive a full asdict() -> ServerConfig(**dict) round-trip."""
    from code_indexer.server.utils.config_manager import ServerConfig

    original = _make_config(
        tmp_path,
        elevation_enforcement_enabled=True,
        elevation_idle_timeout_seconds=240,
        elevation_max_age_seconds=1200,
    )
    d = asdict(original)
    rebuilt = ServerConfig(**d)
    assert rebuilt.elevation_enforcement_enabled is True
    assert rebuilt.elevation_idle_timeout_seconds == 240
    assert rebuilt.elevation_max_age_seconds == 1200


# ---------------------------------------------------------------------------
# Runtime classification — must not be bootstrap, must appear in runtime dict
# ---------------------------------------------------------------------------


def test_elevation_fields_not_in_bootstrap_keys():
    """Elevation fields must NOT appear in BOOTSTRAP_KEYS (they are runtime settings)."""
    from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

    assert "elevation_enforcement_enabled" not in BOOTSTRAP_KEYS
    assert "elevation_idle_timeout_seconds" not in BOOTSTRAP_KEYS
    assert "elevation_max_age_seconds" not in BOOTSTRAP_KEYS


def test_elevation_fields_present_in_extract_runtime_dict(tmp_path):
    """Elevation fields must be extracted into the runtime dict by ConfigService."""
    from code_indexer.server.services.config_service import ConfigService

    config = _make_config(
        tmp_path,
        elevation_enforcement_enabled=True,
        elevation_idle_timeout_seconds=180,
        elevation_max_age_seconds=720,
    )
    runtime = ConfigService._extract_runtime_dict(config)
    assert runtime["elevation_enforcement_enabled"] is True
    assert runtime["elevation_idle_timeout_seconds"] == 180
    assert runtime["elevation_max_age_seconds"] == 720
