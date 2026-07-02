"""
Unit tests for Bug #998: load_config() atomic merge fix.

The race condition: load_config() previously set self._config = config
(bootstrap defaults) BEFORE merging runtime from DB. Any concurrent
get_config() call during the merge window would see wrong defaults
(e.g. elevation_enforcement_enabled = False even when DB says True).

Fix: load_config() must NOT publish self._config until after the
full DB merge completes.

These tests verify:
1. SQLite path: get_config() never transiently sees bootstrap defaults
   while _merge_runtime_config is executing.
2. _merge_runtime_config accepts optional base_config kwarg and uses it
   instead of calling get_config() when provided.
3. _load_runtime_from_pg accepts optional base_config kwarg and passes
   it through to _merge_runtime_config.
4. load_config() falls back correctly for no-DB and no-runtime cases.
"""

import sqlite3
import threading
import time
from unittest.mock import MagicMock, patch

from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.utils.config_manager import ServerConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ELEVATION_RUNTIME_KEY = "elevation_enforcement_enabled"


def _make_sqlite_with_elevation(db_path: str, elevation_value: bool) -> None:
    """Create a minimal server_config SQLite table with elevation set to True."""
    import json

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS server_config "
            "(config_key TEXT PRIMARY KEY, config_json TEXT NOT NULL, "
            "version INTEGER NOT NULL DEFAULT 1, "
            "updated_at TEXT, updated_by TEXT)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO server_config (config_key, config_json, version) "
            "VALUES (?, ?, 1)",
            ("runtime", json.dumps({ELEVATION_RUNTIME_KEY: elevation_value})),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test 1: atomicity — SQLite path never exposes bootstrap default
# ---------------------------------------------------------------------------


def test_load_config_sqlite_never_exposes_bootstrap_default(tmp_path):
    """
    get_config() must never return elevation_enforcement_enabled=False
    while load_config() is merging from SQLite (which has it True).

    Strategy:
    - Patch _merge_runtime_config to inject a brief sleep before delegating
      to the real implementation, simulating a slow DB merge.
    - Call load_config() in a background thread.
    - Poll get_config() in the main thread during the merge.
    - Assert elevation_enforcement_enabled is never False during polling.
    """
    db_path = str(tmp_path / "server.db")
    _make_sqlite_with_elevation(db_path, True)

    svc = ConfigService(server_dir_path=str(tmp_path))
    svc._sqlite_db_path = db_path

    # Capture transient values seen by polling thread
    observed_values: list[bool] = []

    # Wrap _merge_runtime_config to sleep briefly so the polling thread
    # has a chance to call get_config() mid-merge.
    original_merge = svc._merge_runtime_config

    def slow_merge(runtime_dict: dict, base_config=None) -> None:
        time.sleep(0.02)  # 20ms window — enough for polling thread to sample
        original_merge(runtime_dict, base_config=base_config)

    poll_stop_event = threading.Event()

    def poll_thread_fn() -> None:
        """Poll get_config() until told to stop; record elevation values."""
        while not poll_stop_event.wait(timeout=0.002):
            try:
                cfg = svc.get_config()
                observed_values.append(cfg.elevation_enforcement_enabled)
            except Exception:
                pass  # _config still None is expected before load starts

    poll_t = threading.Thread(target=poll_thread_fn, daemon=True)

    with patch.object(svc, "_merge_runtime_config", side_effect=slow_merge):
        poll_t.start()
        svc.load_config()

    poll_stop_event.set()
    poll_t.join(timeout=2.0)

    # The final config must have elevation = True (DB value)
    assert svc.get_config().elevation_enforcement_enabled is True

    # During the merge window, no poll should have seen False
    # (i.e. bootstrap default leaking through before merge completed).
    # Note: before load_config() runs, get_config() may raise or return
    # a value from a previous load — we only care that after the service
    # first becomes readable it shows the correct value.
    #
    # After the fix: self._config is set ONLY after merge, so all polled
    # values should be True (or there were no samples at all).
    false_observations = [v for v in observed_values if v is False]
    assert false_observations == [], (
        f"Observed {len(false_observations)} transient False values for "
        f"elevation_enforcement_enabled — bootstrap defaults leaked during merge"
    )


# ---------------------------------------------------------------------------
# Test 2: _merge_runtime_config base_config kwarg
# ---------------------------------------------------------------------------


def test_merge_runtime_config_uses_base_config_when_provided(tmp_path):
    """
    _merge_runtime_config(runtime_dict, base_config=X) must use X
    instead of calling get_config() (which would be circular/wrong
    during load_config()).
    """
    svc = ConfigService(server_dir_path=str(tmp_path))

    # Load initial config so get_config() works
    svc.load_config()
    original_cfg = svc.get_config()

    # Create a modified base_config with a distinct marker value
    from dataclasses import replace

    base = replace(original_cfg, elevation_enforcement_enabled=False)

    # Call merge with base_config explicitly set to True in runtime
    svc._merge_runtime_config({ELEVATION_RUNTIME_KEY: True}, base_config=base)

    # Result must use the base_config as base and apply the runtime on top
    result = svc.get_config()
    assert result.elevation_enforcement_enabled is True


def test_merge_runtime_config_falls_back_to_get_config_when_base_config_none(tmp_path):
    """
    _merge_runtime_config(runtime_dict) with no base_config uses self.get_config().
    This is the existing caller path (check_config_update, set_connection_pool).
    """
    svc = ConfigService(server_dir_path=str(tmp_path))
    svc.load_config()

    # Explicitly set elevation to False via direct state
    from dataclasses import replace

    svc._config = replace(svc._config, elevation_enforcement_enabled=False)

    # Now merge runtime with elevation=True, no base_config (should use get_config)
    svc._merge_runtime_config({ELEVATION_RUNTIME_KEY: True})

    assert svc.get_config().elevation_enforcement_enabled is True


# ---------------------------------------------------------------------------
# Test 3: load_config() no-DB path still sets _config
# ---------------------------------------------------------------------------


def test_load_config_no_db_sets_config(tmp_path):
    """
    load_config() with no DB (pure file) must still set self._config.
    """
    svc = ConfigService(server_dir_path=str(tmp_path))
    # No sqlite path, no pg pool
    assert svc._sqlite_db_path is None
    assert svc._pool is None

    result = svc.load_config()

    assert result is not None
    assert svc._config is not None
    assert svc._config is result


# ---------------------------------------------------------------------------
# Test 4: load_config() sqlite path with no runtime row still sets _config
# ---------------------------------------------------------------------------


def test_load_config_sqlite_no_runtime_row_sets_config(tmp_path):
    """
    load_config() with SQLite DB but no runtime row must still publish
    self._config (the bootstrap defaults) rather than leaving it as None.
    """
    db_path = str(tmp_path / "server.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS server_config "
            "(config_key TEXT PRIMARY KEY, config_json TEXT NOT NULL, "
            "version INTEGER NOT NULL DEFAULT 1, "
            "updated_at TEXT, updated_by TEXT)"
        )
        conn.commit()
    finally:
        conn.close()

    svc = ConfigService(server_dir_path=str(tmp_path))
    svc._sqlite_db_path = db_path

    result = svc.load_config()

    assert result is not None
    assert svc._config is not None
    assert svc._config is result


# ---------------------------------------------------------------------------
# Test 5: load_config() default config creation path
# ---------------------------------------------------------------------------


def test_load_config_creates_default_when_file_missing(tmp_path):
    """
    load_config() must create and save a default config when config.json
    is absent, and self._config must be set to that default.
    """
    svc = ConfigService(server_dir_path=str(tmp_path))
    result = svc.load_config()

    assert result is not None
    assert isinstance(result, ServerConfig)
    assert svc._config is not None


# ---------------------------------------------------------------------------
# Test 6: _load_runtime_from_pg passes base_config through to merge
# ---------------------------------------------------------------------------


def test_load_runtime_from_pg_passes_base_config(tmp_path):
    """
    _load_runtime_from_pg(base_config=X) must forward base_config to
    _merge_runtime_config so the atomicity guarantee is preserved.
    """
    import json

    svc = ConfigService(server_dir_path=str(tmp_path))

    # Build a mock pool that returns a runtime row with elevation=True
    mock_row = {"config_json": json.dumps({ELEVATION_RUNTIME_KEY: True}), "version": 1}
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.return_value.fetchone.return_value = mock_row
    mock_pool = MagicMock()
    mock_pool.connection.return_value = mock_conn
    svc._pool = mock_pool

    # Load a base config first so we have a ServerConfig to pass
    svc.load_config()
    from dataclasses import replace

    base = replace(svc._config, elevation_enforcement_enabled=False)

    # Track what base_config was passed to _merge_runtime_config
    captured: list = []
    original_merge = svc._merge_runtime_config

    def capturing_merge(runtime_dict: dict, base_config=None) -> None:
        captured.append(base_config)
        original_merge(runtime_dict, base_config=base_config)

    with patch.object(svc, "_merge_runtime_config", side_effect=capturing_merge):
        svc._load_runtime_from_pg(base_config=base)

    assert len(captured) == 1
    assert captured[0] is base
    assert svc.get_config().elevation_enforcement_enabled is True
