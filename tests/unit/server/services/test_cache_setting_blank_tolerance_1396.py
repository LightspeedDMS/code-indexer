"""Bug #1396 -- cache-settings blank-tolerance audit.

The Web UI "Cache Settings" form (POST /admin/config/cache) must tolerate a
blank value for EVERY numeric field by falling back to its documented
default, exactly like the pre-existing size-cap fields
(index_cache_max_size_mb / fts_cache_max_size_mb, Bug #880) and the
swap-threshold field (memory_governor_swap_pswpin_red_threshold, Bug #1225 /
#1396). Before this fix, `_update_cache_setting` (config_service.py) called
`int(value)`/`float(value)` unconditionally on several field groups, raising
ValueError on an empty string and rejecting the ENTIRE cache form even when
the admin only changed an unrelated field.

These are Layer-2 (service-level) tests: they call ConfigService's private
`_update_cache_setting` directly on a real CacheConfig, bypassing the HTTP
layer entirely (mirrors the `_CacheSettingUpdater` helper already used in
test_governor_swap_threshold_1225.py and test_memory_governor_config_story2.py).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helper (mirrors the pattern in test_governor_swap_threshold_1225.py)
# ---------------------------------------------------------------------------

_TEST_SERVER_DIR = str(Path(tempfile.gettempdir()) / "cidx_cache_blank_tolerance_test")


class _CacheSettingUpdater:
    """Minimal stub that calls _update_cache_setting on a real CacheConfig."""

    def __init__(self):
        from code_indexer.server.utils.config_manager import ServerConfig

        self._config = ServerConfig(server_dir=_TEST_SERVER_DIR)
        assert self._config.cache_config is not None

    def update(self, key: str, value: Any) -> None:
        from code_indexer.server.services.config_service import ConfigService

        svc = object.__new__(ConfigService)
        svc._update_cache_setting(self._config, key, value)

    @property
    def cache(self):
        return self._config.cache_config


# ---------------------------------------------------------------------------
# Group A: TTL fields (float) -- documented default 10.0
# ---------------------------------------------------------------------------

_TTL_FIELDS = [
    pytest.param("index_cache_ttl_minutes", 10.0, id="index_ttl"),
    pytest.param("fts_cache_ttl_minutes", 10.0, id="fts_ttl"),
]


@pytest.mark.parametrize("field_name, expected_default", _TTL_FIELDS)
class TestTtlFieldBlankTolerance:
    def test_blank_falls_back_to_default(self, field_name, expected_default):
        f = _CacheSettingUpdater()
        f.update(field_name, "")
        assert getattr(f.cache, field_name) == expected_default

    def test_non_numeric_still_raises(self, field_name, expected_default):
        f = _CacheSettingUpdater()
        with pytest.raises((ValueError, TypeError)):
            f.update(field_name, "abc")


# ---------------------------------------------------------------------------
# Group B: cleanup interval fields (int) -- documented default 60
# ---------------------------------------------------------------------------

_CLEANUP_INTERVAL_FIELDS = [
    pytest.param("index_cache_cleanup_interval", 60, id="index_cleanup"),
    pytest.param("fts_cache_cleanup_interval", 60, id="fts_cleanup"),
]


@pytest.mark.parametrize("field_name, expected_default", _CLEANUP_INTERVAL_FIELDS)
class TestCleanupIntervalFieldBlankTolerance:
    def test_blank_falls_back_to_default(self, field_name, expected_default):
        f = _CacheSettingUpdater()
        f.update(field_name, "")
        assert getattr(f.cache, field_name) == expected_default

    def test_non_numeric_still_raises(self, field_name, expected_default):
        f = _CacheSettingUpdater()
        with pytest.raises((ValueError, TypeError)):
            f.update(field_name, "abc")


# ---------------------------------------------------------------------------
# Group C: payload cache fields (int) -- documented defaults
# ---------------------------------------------------------------------------

_PAYLOAD_FIELDS = [
    pytest.param("payload_preview_size_chars", 2000, id="preview"),
    pytest.param("payload_max_fetch_size_chars", 5000, id="max_fetch"),
    pytest.param("payload_cache_ttl_seconds", 900, id="ttl"),
    pytest.param("payload_cleanup_interval_seconds", 60, id="cleanup"),
]


@pytest.mark.parametrize("field_name, expected_default", _PAYLOAD_FIELDS)
class TestPayloadFieldBlankTolerance:
    def test_blank_falls_back_to_default(self, field_name, expected_default):
        f = _CacheSettingUpdater()
        f.update(field_name, "")
        assert getattr(f.cache, field_name) == expected_default

    def test_non_numeric_still_raises(self, field_name, expected_default):
        f = _CacheSettingUpdater()
        with pytest.raises((ValueError, TypeError)):
            f.update(field_name, "abc")
