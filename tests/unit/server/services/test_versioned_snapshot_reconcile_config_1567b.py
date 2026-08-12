"""Bug #1567 Gap 2: expose the versioned-snapshot orphan sweep's `mode`
("report" vs "delete") as a runtime Web UI Config Screen setting.

As shipped, `mode="delete"` was only a function parameter to
`reconcile_versioned_snapshots` -- `lifespan.py` hardcoded `mode="report"`
with a comment, so the sweep could never promote to "delete" without a
code change. That makes the fix inert for its primary healing purpose
(the ~229-per-repo backlog stays exactly as-is forever).

This mirrors the EXACT established pattern for FleetMigrationConfig
(Story #1458) and HNSWOrphanRepairSweepConfig (Story #1397):
  - tests/unit/server/services/test_fleet_migration_config_service_1458.py
    (config_manager.py dataclass + dict<->dataclass round trip through
    ServerConfigManager, real SQLite runtime table)
  - tests/unit/server/services/test_fleet_migration_web_config_1458.py
    (ConfigService.get_all_settings()/update_setting() round trip)
  - tests/unit/server/web/test_config_section_fleet_migration_1458.py
    (config_section.html structural source-text checks)

DEFAULT MUST BE "report" (fail-closed) -- promoting to "delete" is a
deliberate, explicit operator action via the Web UI, never a code-level
default flip.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest


def _make_sqlite_runtime_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS server_config ("
            "config_key TEXT PRIMARY KEY DEFAULT 'runtime', "
            "config_json TEXT NOT NULL, "
            "version INTEGER NOT NULL DEFAULT 1, "
            "updated_at TEXT DEFAULT (datetime('now')), "
            "updated_by TEXT)"
        )
        conn.commit()


class TestConfigManagerDataclassDefault:
    def test_default_mode_is_report(self):
        from code_indexer.server.utils.config_manager import (
            VersionedSnapshotReconcileConfig,
        )

        assert VersionedSnapshotReconcileConfig().mode == "report"


class TestConfigServiceSqliteRoundTrip:
    def test_fresh_server_first_boot_seed_defaults_to_report(self, tmp_path):
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        db_path = str(tmp_path / "runtime.db")
        _make_sqlite_runtime_table(db_path)
        service.initialize_runtime_db(db_path)

        cfg = service.get_config().versioned_snapshot_reconcile_config
        assert cfg.mode == "report"

    def test_server_restart_reload_from_sqlite_produces_typed_config(self, tmp_path):
        """Bug #1368-class regression guard: before the dict->dataclass
        conversion block exists, `cfg.mode` raises AttributeError after a
        restart reload (the persisted value round-trips as a raw dict)."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import (
            VersionedSnapshotReconcileConfig,
        )

        db_path = str(tmp_path / "runtime.db")
        _make_sqlite_runtime_table(db_path)

        boot_service = ConfigService(server_dir_path=str(tmp_path))
        boot_service.initialize_runtime_db(db_path)

        restarted_service = ConfigService(server_dir_path=str(tmp_path))
        restarted_service.initialize_runtime_db(db_path)

        cfg = restarted_service.get_config().versioned_snapshot_reconcile_config

        assert isinstance(cfg, VersionedSnapshotReconcileConfig), (
            f"Expected VersionedSnapshotReconcileConfig, got "
            f"{type(cfg).__name__} ({cfg!r}) -- Bug #1368-class regression"
        )
        assert cfg.mode == "report"

    def test_custom_web_ui_value_survives_restart_reload(self, tmp_path):
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import (
            VersionedSnapshotReconcileConfig,
        )

        db_path = str(tmp_path / "runtime.db")
        _make_sqlite_runtime_table(db_path)

        boot_service = ConfigService(server_dir_path=str(tmp_path))
        boot_service.initialize_runtime_db(db_path)

        # Simulate an operator explicitly promoting to "delete" via the Web
        # UI -- immediately persisted via the REAL save_config() -> SQLite
        # write, read back via a SEPARATE, freshly-restarted instance.
        config = boot_service.get_config()
        config.versioned_snapshot_reconcile_config = VersionedSnapshotReconcileConfig(
            mode="delete"
        )
        boot_service.save_config(config)

        restarted_service = ConfigService(server_dir_path=str(tmp_path))
        restarted_service.initialize_runtime_db(db_path)
        cfg = restarted_service.get_config().versioned_snapshot_reconcile_config

        assert isinstance(cfg, VersionedSnapshotReconcileConfig)
        assert cfg.mode == "delete"
        assert asdict(cfg) == {"mode": "delete"}


def _make_config_service(tmp_dir: str):
    from code_indexer.server.services.config_service import ConfigService
    from code_indexer.server.utils.config_manager import ServerConfigManager

    mgr = ServerConfigManager(server_dir_path=tmp_dir)
    mgr.save_config(mgr.create_default_config())
    return ConfigService(config_manager=mgr)


class TestGetAllSettingsVersionedSnapshotReconcileSection:
    def test_section_present(self, tmp_path) -> None:
        svc = _make_config_service(str(tmp_path))
        settings = svc.get_all_settings()
        assert "versioned_snapshot_reconcile" in settings

    def test_section_has_mode_key(self, tmp_path) -> None:
        svc = _make_config_service(str(tmp_path))
        section = svc.get_all_settings()["versioned_snapshot_reconcile"]
        assert "mode" in section

    def test_section_default_value_is_report(self, tmp_path) -> None:
        svc = _make_config_service(str(tmp_path))
        section = svc.get_all_settings()["versioned_snapshot_reconcile"]
        assert section["mode"] == "report"


class TestUpdateSettingModeField:
    def test_update_mode_to_delete_persists(self, tmp_path) -> None:
        svc = _make_config_service(str(tmp_path))
        svc.update_setting("versioned_snapshot_reconcile", "mode", "delete")
        assert svc.get_config().versioned_snapshot_reconcile_config.mode == "delete"

    def test_update_mode_back_to_report_persists(self, tmp_path) -> None:
        svc = _make_config_service(str(tmp_path))
        svc.update_setting("versioned_snapshot_reconcile", "mode", "delete")
        svc.update_setting("versioned_snapshot_reconcile", "mode", "report")
        assert svc.get_config().versioned_snapshot_reconcile_config.mode == "report"

    def test_invalid_mode_value_rejected(self, tmp_path) -> None:
        svc = _make_config_service(str(tmp_path))
        with pytest.raises(ValueError):
            svc.update_setting(
                "versioned_snapshot_reconcile", "mode", "delete-everything"
            )
        # Rejected value must NEVER take effect (Issue #1554 discipline --
        # a rejected write must not silently apply).
        assert svc.get_config().versioned_snapshot_reconcile_config.mode == "report"

    def test_unknown_key_raises_value_error(self, tmp_path) -> None:
        svc = _make_config_service(str(tmp_path))
        with pytest.raises(ValueError):
            svc.update_setting("versioned_snapshot_reconcile", "not_a_real_key", "x")


class TestWebRoutesValidateConfigSection:
    def test_valid_delete_mode_accepted(self):
        from code_indexer.server.web.routes import _validate_config_section

        assert (
            _validate_config_section("versioned_snapshot_reconcile", {"mode": "delete"})
            is None
        )

    def test_valid_report_mode_accepted(self):
        from code_indexer.server.web.routes import _validate_config_section

        assert (
            _validate_config_section("versioned_snapshot_reconcile", {"mode": "report"})
            is None
        )

    def test_invalid_mode_rejected_with_message(self):
        from code_indexer.server.web.routes import _validate_config_section

        error = _validate_config_section(
            "versioned_snapshot_reconcile", {"mode": "delete-everything"}
        )
        assert error is not None
        assert "mode" in error.lower()

    def test_section_is_registered_in_valid_sections(self):
        from code_indexer.server.web.routes import _VALID_CONFIG_SECTIONS

        assert "versioned_snapshot_reconcile" in _VALID_CONFIG_SECTIONS


def _read_config_section_template() -> str:
    template_path = (
        Path(__file__).resolve().parents[4]
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
        / "partials"
        / "config_section.html"
    )
    return template_path.read_text()


def _extract_section(html: str, section_id: str) -> str:
    start = html.find(f'id="{section_id}"')
    assert start != -1, f"Missing <details> section id={section_id!r}"
    section_start = html.rfind("<details", 0, start)
    assert section_start != -1
    end = html.find("</details>", start)
    assert end != -1
    return html[section_start : end + len("</details>")]


class TestConfigSectionTemplate:
    def test_template_contains_section(self):
        section = _extract_section(
            _read_config_section_template(), "section-versioned-snapshot-reconcile"
        )
        assert "versioned snapshot" in section.lower()

    def test_template_contains_mode_input(self):
        section = _extract_section(
            _read_config_section_template(), "section-versioned-snapshot-reconcile"
        )
        assert 'name="mode"' in section

    def test_template_posts_to_admin_config_endpoint(self):
        section = _extract_section(
            _read_config_section_template(), "section-versioned-snapshot-reconcile"
        )
        assert 'action="/admin/config/versioned_snapshot_reconcile"' in section


class TestLifespanReadsModeFromConfig:
    """The Bug #1567 Gap 2 wiring guard: lifespan.py's reconcile call MUST
    read `mode` from the config service, never a hardcoded string literal.
    Mirrors this project's own AST/source-text wiring-guard convention
    (e.g. test_temporal_write_side_sister_path_1529.py,
    test_lifespan_clone_backend_wiring_bug1044.py) -- a full lifespan()
    integration test would require standing up the entire FastAPI app
    lifecycle, which is disproportionate to verifying one call-site wire.
    """

    def _read_lifespan_source(self) -> str:
        lifespan_path = (
            Path(__file__).resolve().parents[4]
            / "src"
            / "code_indexer"
            / "server"
            / "startup"
            / "lifespan.py"
        )
        return lifespan_path.read_text()

    def test_reconcile_call_site_does_not_hardcode_report_string_literal(self):
        source = self._read_lifespan_source()
        start = source.find("reconcile_versioned_snapshots(")
        assert start != -1, "reconcile_versioned_snapshots(...) call site not found"
        # Scan forward for the matching closing paren of the call (handles
        # nested parens defensively via a depth count).
        end = start
        depth = 0
        idx = start + len("reconcile_versioned_snapshots(") - 1
        for i in range(idx, len(source)):
            if source[i] == "(":
                depth += 1
            elif source[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        call_text = source[start : end + 1]
        assert 'mode="report"' not in call_text, (
            "reconcile_versioned_snapshots(...) still hardcodes "
            'mode="report" -- Gap 2 requires reading it from the config '
            "service so an operator can promote to delete mode"
        )
        assert "mode=" in call_text, "call site must still pass mode=..."

    def test_versioned_snapshot_reconcile_config_is_referenced(self):
        source = self._read_lifespan_source()
        assert "versioned_snapshot_reconcile_config" in source
