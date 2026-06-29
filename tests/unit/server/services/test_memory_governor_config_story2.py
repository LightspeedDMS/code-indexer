"""Tests for Story #1213 Story 2: CacheConfig fields, _update_cache_setting mapping,
validation, live hot-reload, kill-switch, config_section.html rows,
get_all_settings cache keys, and source-text guards.

RED phase: all tests fail before implementation.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT_PARENT_INDEX = 4
_REPO_ROOT = Path(__file__).resolve().parents[_REPO_ROOT_PARENT_INDEX]

_CONFIG_SECTION_HTML = (
    _REPO_ROOT
    / "src"
    / "code_indexer"
    / "server"
    / "web"
    / "templates"
    / "partials"
    / "config_section.html"
)
_GOVERNOR_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "services" / "memory_governor.py"
)
_SERVICE_INIT_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "service_init.py"
)

# ---------------------------------------------------------------------------
# Expected fields: name -> design default
# ---------------------------------------------------------------------------

_EXPECTED_FIELDS = [
    ("memory_governor_enabled", True),
    ("memory_governor_yellow_pct", 70.0),
    ("memory_governor_red_pct", 85.0),
    ("memory_governor_hysteresis_pct", 10.0),
    ("memory_governor_red_min_dwell_seconds", 30),
    ("memory_governor_sample_interval_seconds", 2.0),
    ("memory_governor_swap_forces_red", True),
    ("memory_governor_rss_inflation_factor", 2.0),
]

_FIELD_NAMES = [name for name, _ in _EXPECTED_FIELDS]


# ---------------------------------------------------------------------------
# 1. CacheConfig fields and defaults
# ---------------------------------------------------------------------------


class TestCacheConfigFields:
    """CacheConfig has 8 new governor fields with correct design defaults."""

    @pytest.mark.parametrize("field_name,expected_default", _EXPECTED_FIELDS)
    def test_field_exists_with_correct_default(self, field_name, expected_default):
        from code_indexer.server.utils.config_manager import CacheConfig

        field_names = {f.name for f in fields(CacheConfig)}
        assert field_name in field_names, f"CacheConfig missing field: {field_name}"
        c = CacheConfig()
        assert getattr(c, field_name) == expected_default, (
            f"{field_name}: expected {expected_default}, got {getattr(c, field_name)}"
        )

    def test_backward_compat_old_dict_without_governor_fields_uses_defaults(self):
        """An old persisted CacheConfig dict without the 8 new fields loads fine."""
        from code_indexer.server.utils.config_manager import CacheConfig

        old_dict = {
            "index_cache_ttl_minutes": 10.0,
            "index_cache_cleanup_interval": 60,
            "index_cache_max_size_mb": None,
            "fts_cache_ttl_minutes": 10.0,
            "fts_cache_cleanup_interval": 60,
            "fts_cache_max_size_mb": None,
            "fts_cache_reload_on_access": True,
            "payload_preview_size_chars": 2000,
            "payload_max_fetch_size_chars": 5000,
            "payload_cache_ttl_seconds": 900,
            "payload_cleanup_interval_seconds": 60,
            "query_path_cache_enabled": True,
            "repo_config_cache_ttl_seconds": 30,
            "repo_config_cache_max_entries": 2048,
        }
        c = CacheConfig(**old_dict)
        assert c.memory_governor_enabled is True
        assert c.memory_governor_yellow_pct == 70.0
        assert c.memory_governor_red_pct == 85.0


# ---------------------------------------------------------------------------
# Shared helper for _update_cache_setting tests
# ---------------------------------------------------------------------------


class _CacheSettingUpdater:
    """Minimal stub that applies _update_cache_setting on a real CacheConfig."""

    def __init__(self):
        from code_indexer.server.utils.config_manager import ServerConfig

        self._config = ServerConfig(server_dir="/tmp/test")
        assert self._config.cache_config is not None

    def update(self, key: str, value: Any) -> None:
        from code_indexer.server.services.config_service import ConfigService

        svc = object.__new__(ConfigService)
        svc._update_cache_setting(self._config, key, value)

    @property
    def cache(self):
        return self._config.cache_config


# ---------------------------------------------------------------------------
# 2. _update_cache_setting mapping
# ---------------------------------------------------------------------------

_MAPPING_CASES = [
    ("memory_governor_enabled", "true", True),
    ("memory_governor_enabled", "false", False),
    ("memory_governor_yellow_pct", "60.0", 60.0),
    ("memory_governor_red_pct", "80.0", 80.0),
    ("memory_governor_hysteresis_pct", "5.0", 5.0),
    ("memory_governor_red_min_dwell_seconds", "45", 45),
    ("memory_governor_sample_interval_seconds", "3.0", 3.0),
    ("memory_governor_swap_forces_red", "false", False),
    ("memory_governor_rss_inflation_factor", "1.5", 1.5),
]


class TestUpdateCacheSettingMapping:
    """_update_cache_setting correctly maps all 8 governor keys."""

    @pytest.mark.parametrize("key,input_val,expected", _MAPPING_CASES)
    def test_setting_mapped_correctly(self, key, input_val, expected):
        f = _CacheSettingUpdater()
        f.update(key, input_val)
        assert getattr(f.cache, key) == expected, (
            f"cache.{key}: expected {expected}, got {getattr(f.cache, key)}"
        )


# ---------------------------------------------------------------------------
# 3. Validation — loudly reject bad watermarks
# ---------------------------------------------------------------------------

_REJECT_CASES = [
    ("memory_governor_yellow_pct", "0.0", "yellow"),
    ("memory_governor_yellow_pct", "-1.0", "yellow"),
    ("memory_governor_red_pct", "101.0", "red"),
    ("memory_governor_hysteresis_pct", "70.0", "hysteresis"),  # >= yellow(70)
    ("memory_governor_hysteresis_pct", "15.0", "hysteresis"),  # >= 100-red(15)
]


class TestGovernorWatermarkValidation:
    """Validation rejects bad watermark combinations with a clear error."""

    @pytest.mark.parametrize("key,bad_value,match", _REJECT_CASES)
    def test_invalid_value_rejected(self, key, bad_value, match):
        with pytest.raises(ValueError, match=match):
            _CacheSettingUpdater().update(key, bad_value)

    def test_yellow_ge_red_rejected(self):
        """yellow >= current red must be rejected."""
        f = _CacheSettingUpdater()
        # default red=85; yellow=85 is invalid (>= red)
        with pytest.raises(ValueError, match="yellow"):
            f.update("memory_governor_yellow_pct", "85.0")

    def test_yellow_greater_than_red_rejected(self):
        """yellow > current red must be rejected."""
        f = _CacheSettingUpdater()
        f._config.cache_config.memory_governor_red_pct = 60.0
        with pytest.raises(ValueError, match="yellow"):
            f.update("memory_governor_yellow_pct", "70.0")

    def test_red_equal_100_accepted(self):
        """red=100 is the upper bound; it must be accepted."""
        f = _CacheSettingUpdater()
        f._config.cache_config.memory_governor_yellow_pct = 50.0
        f._config.cache_config.memory_governor_hysteresis_pct = 5.0
        f.update("memory_governor_red_pct", "100.0")
        assert f.cache.memory_governor_red_pct == 100.0

    def test_valid_combination_accepted(self):
        """yellow=60, red=80, hysteresis=5 — hysteresis < min(60, 20) — is valid."""
        f = _CacheSettingUpdater()
        f.update("memory_governor_yellow_pct", "60.0")
        f.update("memory_governor_red_pct", "80.0")
        f.update("memory_governor_hysteresis_pct", "5.0")
        assert f.cache.memory_governor_yellow_pct == 60.0
        assert f.cache.memory_governor_red_pct == 80.0
        assert f.cache.memory_governor_hysteresis_pct == 5.0


# ---------------------------------------------------------------------------
# Shared fake helpers for live-reload tests
# ---------------------------------------------------------------------------


class _FakeReaders:
    """Fake memory readers: 10% RSS, no swap, host basis."""

    def read_cgroup_v2_max(self) -> str:
        raise FileNotFoundError("no cgroup v2")

    def read_cgroup_v1_limit(self) -> int:
        raise FileNotFoundError("no cgroup v1")

    def read_host_memory(self):
        m = MagicMock()
        m.total = 16 * 1024 * 1024 * 1024  # 16 GB
        m.used = int(m.total * 0.10)  # 10% used
        return m

    def read_pswpin(self) -> int:
        return 0


class _FakeConfigService:
    """Minimal config-service stub with mutable CacheConfig."""

    def __init__(self, yellow: float = 70.0, red: float = 85.0):
        from code_indexer.server.utils.config_manager import CacheConfig, ServerConfig

        cache = CacheConfig(
            memory_governor_enabled=True,
            memory_governor_yellow_pct=yellow,
            memory_governor_red_pct=red,
            memory_governor_hysteresis_pct=10.0,
            memory_governor_red_min_dwell_seconds=0,
            memory_governor_sample_interval_seconds=2.0,
            memory_governor_swap_forces_red=False,
            memory_governor_rss_inflation_factor=2.0,
        )
        self._config = ServerConfig(server_dir="/tmp/test", cache_config=cache)

    def get_config(self):
        return self._config

    @property
    def cache(self):
        return self._config.cache_config


# ---------------------------------------------------------------------------
# 4. Live hot-reload: governor reads config each decision
# ---------------------------------------------------------------------------


class TestGovernorLiveReload:
    """Governor reads watermarks live from config_service on each _tick()."""

    def test_band_changes_when_watermarks_tightened_live(self):
        """
        10% usage is GREEN at yellow=70/red=85.
        After tightening to yellow=5/red=8 (live), the same 10% is RED.
        No governor rebuild required.
        """
        from code_indexer.server.services.memory_governor import (
            MemoryBand,
            MemoryGovernor,
        )

        cfg = _FakeConfigService(yellow=70.0, red=85.0)
        gov = MemoryGovernor(
            readers=_FakeReaders(),
            enabled=True,
            start_sampler=False,
            config_service=cfg,
        )

        gov._tick()
        assert gov.band == MemoryBand.GREEN, (
            f"Expected GREEN at 10% with yellow=70; got {gov.band}"
        )

        cfg.cache.memory_governor_yellow_pct = 5.0
        cfg.cache.memory_governor_red_pct = 8.0

        gov._tick()
        assert gov.band == MemoryBand.RED, (
            f"Expected RED after watermark tightening; got {gov.band}"
        )

    def test_config_read_failure_forces_fail_safe_red(self):
        """config_service.get_config() raising must revert band to RED."""
        from code_indexer.server.services.memory_governor import (
            MemoryBand,
            MemoryGovernor,
        )

        class _BrokenCfg:
            def get_config(self):
                raise RuntimeError("DB unavailable")

        gov = MemoryGovernor(
            readers=_FakeReaders(),
            enabled=True,
            start_sampler=False,
            config_service=_BrokenCfg(),
        )

        gov._tick()
        assert gov.band == MemoryBand.RED, (
            "Fail-safe: band must be RED when config_service.get_config() raises"
        )


# ---------------------------------------------------------------------------
# 5. Kill-switch: enabled=False read live from config
# ---------------------------------------------------------------------------


class TestKillSwitchLiveRead:
    """should_evict_after_shard() reads enabled live from config_service."""

    def test_live_disabled_returns_evict_true(self):
        from code_indexer.server.services.memory_governor import MemoryGovernor

        cfg = _FakeConfigService(yellow=70.0, red=85.0)
        cfg.cache.memory_governor_enabled = False

        gov = MemoryGovernor(
            readers=_FakeReaders(),
            enabled=True,  # constructor param irrelevant when config_service supplied
            start_sampler=False,
            config_service=cfg,
        )

        assert gov.should_evict_after_shard() is True, (
            "should_evict_after_shard() must return True when live config disabled"
        )

    def test_live_enabled_green_band_returns_evict_false(self):
        from code_indexer.server.services.memory_governor import (
            MemoryBand,
            MemoryGovernor,
        )

        cfg = _FakeConfigService(yellow=70.0, red=85.0)
        gov = MemoryGovernor(
            readers=_FakeReaders(),
            enabled=True,
            start_sampler=False,
            config_service=cfg,
        )

        gov._tick()
        assert gov.band == MemoryBand.GREEN
        assert gov.should_evict_after_shard() is False

    def test_enabled_toggle_mid_session(self):
        """Flip enabled=False live; next call returns True without rebuild."""
        from code_indexer.server.services.memory_governor import (
            MemoryBand,
            MemoryGovernor,
        )

        cfg = _FakeConfigService(yellow=70.0, red=85.0)
        gov = MemoryGovernor(
            readers=_FakeReaders(),
            enabled=True,
            start_sampler=False,
            config_service=cfg,
        )

        gov._tick()
        assert gov.band == MemoryBand.GREEN
        assert gov.should_evict_after_shard() is False

        cfg.cache.memory_governor_enabled = False
        assert gov.should_evict_after_shard() is True


# ---------------------------------------------------------------------------
# 6. config_section.html — 8 display rows and 8 form inputs
# ---------------------------------------------------------------------------


class TestConfigSectionHtml:
    """config_section.html has display rows and edit form inputs for all 8 governor settings."""

    @pytest.fixture(autouse=True)
    def html(self):
        self._html = _CONFIG_SECTION_HTML.read_text()

    def test_all_eight_governor_entries_present(self):
        """All 8 governor field names appear in the HTML."""
        missing = [name for name in _FIELD_NAMES if name not in self._html]
        assert not missing, f"config_section.html missing governor fields: {missing}"

    def test_edit_form_has_inputs_for_all_governor_fields(self):
        """All 8 governor fields must appear as named form inputs."""
        missing = [name for name in _FIELD_NAMES if f'name="{name}"' not in self._html]
        assert not missing, (
            f"config_section.html edit form missing name= inputs: {missing}"
        )

    def test_display_table_rows_contain_governor_labels(self):
        """Display table must have at least 8 rows referencing governor settings.
        Each row is identified by config.cache.<field_name> in Jinja template syntax.
        """
        row_count = sum(
            1 for name in _FIELD_NAMES if f"config.cache.{name}" in self._html
        )
        assert row_count == len(_FIELD_NAMES), (
            f"Expected {len(_FIELD_NAMES)} display rows with config.cache.<name>; "
            f"found {row_count}"
        )


# ---------------------------------------------------------------------------
# 7. get_all_settings() returns all 8 new cache keys
# ---------------------------------------------------------------------------


class TestGetAllSettingsCacheKeys:
    """get_all_settings() includes all 8 governor fields in the 'cache' dict."""

    def test_all_eight_governor_keys_in_cache_settings(self, tmp_path):
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        mgr = ServerConfigManager(server_dir_path=str(tmp_path))
        svc = ConfigService(config_manager=mgr)
        settings = svc.get_all_settings()

        cache_keys = set(settings.get("cache", {}).keys())
        missing = [name for name in _FIELD_NAMES if name not in cache_keys]
        assert not missing, (
            f"get_all_settings()['cache'] missing governor keys: {missing}"
        )


# ---------------------------------------------------------------------------
# 8. Source-text guards
# ---------------------------------------------------------------------------


class TestGovernorLiveReadSourceGuard:
    """Source-text guards: governor reads live config; service_init passes config_service."""

    def test_governor_accepts_config_service_parameter(self):
        source = _GOVERNOR_PATH.read_text()
        assert "config_service" in source, (
            "MemoryGovernor.__init__ must accept config_service for live hot-reload"
        )

    def test_governor_reads_cache_config_in_band_logic(self):
        source = _GOVERNOR_PATH.read_text()
        assert "cache_config" in source or "get_config" in source, (
            "MemoryGovernor must read live cache_config from config_service"
        )

    def test_service_init_passes_config_service_to_governor(self):
        """service_init.py must use config_service for live read, not frozen scalars."""
        source = _SERVICE_INIT_PATH.read_text()
        assert "config_service" in source or "get_config_service" in source, (
            "service_init.py must pass config_service to the governor (live read)"
        )
