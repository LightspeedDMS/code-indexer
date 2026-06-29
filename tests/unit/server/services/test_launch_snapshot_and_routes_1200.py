"""Story #1200 AC1/AC3/AC5/AC7: snapshot helper, no-boot-infer, route wiring.

RED -> GREEN -> REFACTOR.

AC1/AC3 -- _read_raw_launch_snapshot() returns generation + host/port/workers/log_level.
AC5     -- initialize_runtime_db must NOT write applied_launch.json on startup.
AC7     -- routes.py restart_server must branch: cluster bumps generation only;
           solo materializes + single-node-restart without bump.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Named constants for source-scan windows
# ---------------------------------------------------------------------------

_ROUTE_FN_SCAN_WINDOW = 3500

# ---------------------------------------------------------------------------
# Source paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONFIG_SERVICE_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "services" / "config_service.py"
)
_ROUTES_PATH = _REPO_ROOT / "src" / "code_indexer" / "server" / "web" / "routes.py"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sqlite_db(db_path: str) -> None:
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


def _seed_runtime_row(db_path: str, data: dict, version: int = 1) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO server_config (config_key, config_json, version, updated_by) "
            "VALUES ('runtime', ?, ?, 'test') "
            "ON CONFLICT(config_key) DO UPDATE SET "
            "config_json = excluded.config_json, "
            "version = excluded.version",
            (json.dumps(data), version),
        )
        conn.commit()


def _make_config_service(tmp_path: Path, db_path: str):
    from code_indexer.server.services.config_service import ConfigService

    svc = ConfigService(server_dir_path=str(tmp_path))
    svc.load_config()
    svc._sqlite_db_path = db_path
    return svc


# ===========================================================================
# AC1/AC3: _read_raw_launch_snapshot() helper
# ===========================================================================


class TestReadRawLaunchSnapshotMethodExists:
    """Source guard: method must be defined."""

    def test_method_defined(self) -> None:
        source = _CONFIG_SERVICE_PATH.read_text()
        assert "def _read_raw_launch_snapshot" in source, (
            "AC1/AC3: ConfigService must define _read_raw_launch_snapshot()"
        )


@pytest.mark.slow
class TestReadRawLaunchSnapshotBehavioral:
    """AC1/AC3 behavioral: real SQLite, snapshot returns all five fields."""

    def test_returns_all_five_fields(self, tmp_path: Path) -> None:
        """Helper must return dict with generation + 4 launch keys."""
        db_path = str(tmp_path / "cidx.db")
        _make_sqlite_db(db_path)
        _seed_runtime_row(
            db_path,
            {
                "workers": 4,
                "log_level": "DEBUG",
                "host": "0.0.0.0",
                "port": 9000,
                "launch_restart_generation": 2,
            },
        )

        svc = _make_config_service(tmp_path, db_path)
        snapshot = svc._read_raw_launch_snapshot()

        assert snapshot is not None
        assert snapshot.get("launch_restart_generation") == 2
        assert snapshot.get("workers") == 4
        assert snapshot.get("log_level") == "DEBUG"
        assert snapshot.get("host") == "0.0.0.0"
        assert snapshot.get("port") == 9000

    def test_coalesces_absent_generation_to_zero(self, tmp_path: Path) -> None:
        """Absent launch_restart_generation -> COALESCE 0."""
        db_path = str(tmp_path / "cidx.db")
        _make_sqlite_db(db_path)
        _seed_runtime_row(db_path, {"workers": 1})

        svc = _make_config_service(tmp_path, db_path)
        snapshot = svc._read_raw_launch_snapshot()

        assert snapshot is not None
        assert snapshot.get("launch_restart_generation") == 0, (
            "AC1/AC3: absent generation must COALESCE to 0"
        )

    def test_returns_none_when_no_db(self, tmp_path: Path) -> None:
        """No DB configured -> helper returns None gracefully."""
        from code_indexer.server.services.config_service import ConfigService

        svc = ConfigService(server_dir_path=str(tmp_path))
        svc.load_config()
        # No _sqlite_db_path set, no _pool
        snapshot = svc._read_raw_launch_snapshot()
        assert snapshot is None, (
            "AC1/AC3: _read_raw_launch_snapshot must return None when no DB configured"
        )


# ===========================================================================
# AC5: initialize_runtime_db must NOT write applied_launch.json on startup
# ===========================================================================


@pytest.mark.slow
class TestAC5NoBootInfer:
    """AC5: startup does NOT boot-infer applied generation."""

    def test_initialize_runtime_db_does_not_write_applied_launch(
        self, tmp_path: Path
    ) -> None:
        """initialize_runtime_db must NOT create applied_launch.json."""
        from code_indexer.server.services.config_service import ConfigService

        db_path = str(tmp_path / "cidx.db")
        _make_sqlite_db(db_path)
        _seed_runtime_row(
            db_path,
            {"workers": 1, "launch_restart_generation": 5},
        )

        applied_path = tmp_path / "applied_launch.json"
        svc = ConfigService(server_dir_path=str(tmp_path))
        svc.load_config()

        with patch(
            "code_indexer.server.services.config_service.APPLIED_LAUNCH_CONFIG_PATH",
            applied_path,
        ):
            svc.initialize_runtime_db(db_path)

        assert not applied_path.exists(), (
            "AC5: initialize_runtime_db must NOT write applied_launch.json; "
            "only the auto-updater (Story #1199) writes it"
        )

    def test_load_config_does_not_write_applied_launch(self, tmp_path: Path) -> None:
        """load_config() must NOT write applied_launch.json."""
        from code_indexer.server.services.config_service import ConfigService

        db_path = str(tmp_path / "cidx.db")
        _make_sqlite_db(db_path)
        _seed_runtime_row(
            db_path,
            {"workers": 1, "launch_restart_generation": 3},
        )

        applied_path = tmp_path / "applied_launch.json"
        svc = ConfigService(server_dir_path=str(tmp_path))

        with patch(
            "code_indexer.server.services.config_service.APPLIED_LAUNCH_CONFIG_PATH",
            applied_path,
        ):
            svc.load_config()
            svc._sqlite_db_path = db_path
            svc._load_runtime_from_sqlite()

        assert not applied_path.exists(), (
            "AC5: load_config must NOT write applied_launch.json"
        )


# ===========================================================================
# AC7: routes.py restart_server cluster/solo branch guards
# ===========================================================================


class TestAC7RoutesClusterSoloBranch:
    """AC7: routes.py restart_server must branch cluster vs solo."""

    def _restart_fn_body(self) -> str:
        source = _ROUTES_PATH.read_text()
        fn_start = source.find("def restart_server(")
        assert fn_start != -1, "restart_server function not found in routes.py"
        return source[fn_start : fn_start + _ROUTE_FN_SCAN_WINDOW]

    def test_restart_server_defined(self) -> None:
        """routes.py must still define restart_server."""
        source = _ROUTES_PATH.read_text()
        assert "def restart_server" in source

    def test_cluster_path_calls_bump(self) -> None:
        """AC7: cluster branch must call bump_launch_restart_generation()."""
        source = _ROUTES_PATH.read_text()
        assert "bump_launch_restart_generation" in source, (
            "AC7: routes.py cluster branch must call bump_launch_restart_generation()"
        )

    def test_bump_inside_conditional_guard(self) -> None:
        """AC7/FIX-5: bump must be inside a cluster/solo conditional, not unconditional."""
        fn_body = self._restart_fn_body()
        bump_pos = fn_body.find("bump_launch_restart_generation")
        assert bump_pos != -1, "bump must appear in restart_server body"
        pre_bump = fn_body[:bump_pos]
        assert "if " in pre_bump or "else" in pre_bump, (
            "AC7: bump_launch_restart_generation must be inside a conditional guard "
            "(cluster/solo branch), not called unconditionally"
        )

    def test_solo_path_retains_single_node_restart(self) -> None:
        """AC7/FIX-2: solo branch must retain _schedule_delayed_restart or _delayed_restart."""
        source = _ROUTES_PATH.read_text()
        assert "_schedule_delayed_restart" in source or "_delayed_restart" in source, (
            "AC7: routes.py must retain the solo single-node restart path "
            "(_schedule_delayed_restart or _delayed_restart)"
        )

    def test_cluster_detection_uses_pool_attribute(self) -> None:
        """AC7: cluster detection must check for PG pool (_pool attribute)."""
        fn_body = self._restart_fn_body()
        assert "_pool" in fn_body or "pool" in fn_body.lower(), (
            "AC7: restart_server must detect cluster mode via the PG pool attribute"
        )
