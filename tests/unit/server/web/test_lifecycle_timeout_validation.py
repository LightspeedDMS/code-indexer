"""
Unit tests for Story #885 Phase 5b — Lifecycle Analysis Timeout validation (AC-V4-8)
and auto-migration visibility (AC-V4-17).

AC-V4-8: Invalid timeout combination (outer < shell + 30) is rejected at save time
         with a validation error. Persisted config is unchanged.

AC-V4-17: Upgraded server surfaces lifecycle_analysis_config in Web UI without
          operator action -- defaults {shell: 360, outer: 420} auto-populated
          in runtime config storage.
"""

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from code_indexer.server.web.routes import _validate_config_section


# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

DEFAULT_SHELL_TIMEOUT = 360
DEFAULT_OUTER_TIMEOUT = 420
MINIMUM_MARGIN = 30  # outer must be >= shell + MINIMUM_MARGIN

SHELL_600 = 600
OUTER_INVALID_610 = 610    # 610 < 600 + 30 = 630  -> rejected
OUTER_BOUNDARY_630 = 630   # 630 == 600 + 30        -> accepted (boundary)
OUTER_VALID_700 = 700      # 700 > 600 + 30         -> accepted


# ---------------------------------------------------------------------------
# Shared legacy-DB helper
# ---------------------------------------------------------------------------

_SERVER_CONFIG_DDL = """
    CREATE TABLE IF NOT EXISTS server_config (
        config_key   TEXT PRIMARY KEY,
        config_json  TEXT NOT NULL,
        version      INTEGER NOT NULL DEFAULT 1,
        updated_at   DATETIME DEFAULT (datetime('now')),
        updated_by   TEXT
    )
"""

# Minimal legacy runtime that intentionally lacks lifecycle_analysis_config
_LEGACY_RUNTIME_WITHOUT_LIFECYCLE = {
    "password_security_config": {
        "min_length": 8,
        "max_length": 128,
        "required_char_classes": 2,
    },
}


def _make_legacy_db(server_dir: Path) -> Path:
    """
    Create a minimal bootstrap config.json and a SQLite DB whose runtime
    config deliberately omits lifecycle_analysis_config, simulating a server
    that was installed before Story #885.

    Returns the path to the SQLite DB file.
    """
    (server_dir / "config.json").write_text(json.dumps({
        "server_dir": str(server_dir),
        "host": "localhost",
        "port": 8000,
    }))

    db_path = server_dir / "runtime.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_SERVER_CONFIG_DDL)
        conn.execute(
            "INSERT INTO server_config "
            "(config_key, config_json, version, updated_by) VALUES (?, ?, 1, ?)",
            ("runtime", json.dumps(_LEGACY_RUNTIME_WITHOUT_LIFECYCLE), "test"),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


# ---------------------------------------------------------------------------
# AC-V4-8: _validate_config_section("lifecycle_analysis", ...) cross-field rule
# ---------------------------------------------------------------------------


class TestLifecycleTimeoutValidation:
    """_validate_config_section enforces outer >= shell + 30 cross-field rule."""

    def test_save_rejects_outer_less_than_shell_plus_30(self):
        """outer=610 with shell=600: 610 < 600+30, must return an error string."""
        error = _validate_config_section(
            "lifecycle_analysis",
            {
                "shell_timeout_seconds": str(SHELL_600),
                "outer_timeout_seconds": str(OUTER_INVALID_610),
            },
        )
        assert error is not None
        assert "outer" in error.lower() or "timeout" in error.lower()

    def test_save_accepts_valid_outer_at_minimum_boundary(self):
        """outer=630 with shell=600: exactly at boundary 600+30, must accept (None)."""
        error = _validate_config_section(
            "lifecycle_analysis",
            {
                "shell_timeout_seconds": str(SHELL_600),
                "outer_timeout_seconds": str(OUTER_BOUNDARY_630),
            },
        )
        assert error is None

    def test_save_accepts_valid_outer_above_minimum(self):
        """outer=700 with shell=600: 700 > 600+30, above minimum, must accept (None)."""
        error = _validate_config_section(
            "lifecycle_analysis",
            {
                "shell_timeout_seconds": str(SHELL_600),
                "outer_timeout_seconds": str(OUTER_VALID_700),
            },
        )
        assert error is None


# ---------------------------------------------------------------------------
# AC-V4-17: auto-migration — first boot with missing section gets defaults
# ---------------------------------------------------------------------------


@pytest.fixture()
def legacy_config_service(tmp_path):
    """
    ConfigService initialised against a SQLite DB that has no
    lifecycle_analysis_config, simulating a pre-885 upgraded server.
    """
    from code_indexer.server.services.config_service import ConfigService

    server_dir = tmp_path / "cidx-server"
    server_dir.mkdir()
    db_path = _make_legacy_db(server_dir)

    service = ConfigService(server_dir_path=str(server_dir))
    service.initialize_runtime_db(str(db_path))
    return service


class TestLifecycleConfigAutoMigration:
    """AC-V4-17: Upgraded server gets lifecycle defaults without operator action."""

    def test_first_boot_with_missing_section_populates_defaults(
        self, legacy_config_service
    ):
        """
        After initialize_runtime_db() on a legacy DB without lifecycle_analysis_config,
        get_all_settings()["lifecycle_analysis"] must return defaults {shell:360, outer:420}.
        """
        settings = legacy_config_service.get_all_settings()

        assert "lifecycle_analysis" in settings
        section = settings["lifecycle_analysis"]
        assert section["shell_timeout_seconds"] == DEFAULT_SHELL_TIMEOUT
        assert section["outer_timeout_seconds"] == DEFAULT_OUTER_TIMEOUT

    def test_first_boot_logs_migration_event(self, tmp_path, caplog):
        """
        initialize_runtime_db() on a legacy DB without lifecycle_analysis_config
        must emit an INFO log event mentioning 'lifecycle_analysis_config'.
        """
        from code_indexer.server.services.config_service import ConfigService

        server_dir = tmp_path / "cidx-server2"
        server_dir.mkdir()
        db_path = _make_legacy_db(server_dir)

        service = ConfigService(server_dir_path=str(server_dir))
        with caplog.at_level(
            logging.INFO,
            logger="code_indexer.server.services.config_service",
        ):
            service.initialize_runtime_db(str(db_path))

        lifecycle_logs = [
            r for r in caplog.records
            if r.levelno == logging.INFO
            and "lifecycle_analysis" in r.getMessage().lower()
        ]
        assert len(lifecycle_logs) >= 1, (
            "Expected INFO log mentioning 'lifecycle_analysis' during "
            f"initialize_runtime_db; INFO records: "
            f"{[r.getMessage() for r in caplog.records if r.levelno == logging.INFO]}"
        )
