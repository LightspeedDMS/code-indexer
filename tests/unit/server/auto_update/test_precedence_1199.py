"""
TDD tests for Story #1199 AC4: source precedence chain and ServerConfig defaults.

APPLY:  launch.json → config.json → ServerConfig defaults.
DEPLOY: applied_launch.json → config.json → defaults (NEVER launch.json).

Behavioral: real temp files, patched subprocess only.  DB-free: no ServerConfigManager.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_MAIN_PY_UNIT = """\
[Unit]
Description=CIDX Server

[Service]
ExecStart=/usr/bin/python3 -m code_indexer.server.main --host 0.0.0.0 --port 8000

[Install]
WantedBy=multi-user.target
"""


@pytest.fixture
def unit_dir(tmp_path: Path) -> Path:
    d = tmp_path / "systemd"
    d.mkdir()
    return d


@pytest.fixture
def executor(tmp_path: Path) -> Any:
    from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor

    return DeploymentExecutor(repo_path=tmp_path, service_name="cidx-server")


def _run_mode(
    executor: Any,
    unit_dir: Path,
    *,
    mode: str,
    launch_path: Path,
    applied_path: Path,
    cidx_data_dir: Path,
) -> tuple:
    """Run _ensure_launch_config(mode); return (result, written_or_None)."""
    tee_written: list = []

    def fake_run(cmd: list, **kwargs: Any) -> MagicMock:
        r = MagicMock()
        r.returncode = 0
        r.stderr = ""
        r.stdout = ""
        if "tee" in cmd:
            tee_written.append(kwargs.get("input", ""))
        return r

    with patch(
        "code_indexer.server.auto_update.deployment_executor.subprocess.run",
        side_effect=fake_run,
    ):
        with patch(
            "code_indexer.server.auto_update.deployment_executor.LAUNCH_CONFIG_PATH",
            launch_path,
        ):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.APPLIED_LAUNCH_CONFIG_PATH",
                applied_path,
            ):
                with patch(
                    "code_indexer.server.auto_update.deployment_executor._cidx_data_dir",
                    cidx_data_dir,
                ):
                    with patch(
                        "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                        unit_dir,
                    ):
                        result = executor._ensure_launch_config(mode)

    return result, (tee_written[0] if tee_written else None)


class TestAC4Precedence:
    """AC4: source precedence chain is correct for APPLY and DEPLOY modes."""

    def test_apply_launch_json_overrides_config_json(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC4: APPLY uses launch.json values when both launch.json and config.json present."""
        (unit_dir / "cidx-server.service").write_text(_MAIN_PY_UNIT)
        launch = tmp_path / "launch.json"
        launch.write_text(json.dumps({"host": "10.0.0.1", "port": 9001, "workers": 6}))
        applied = tmp_path / "applied_launch.json"  # not created
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"host": "1.2.3.4", "port": 5555, "workers": 1}))

        result, written = _run_mode(
            executor,
            unit_dir,
            mode="APPLY",
            launch_path=launch,
            applied_path=applied,
            cidx_data_dir=tmp_path,
        )

        assert written is not None
        assert "--port 9001" in written, f"launch.json port must win; got: {written!r}"
        assert "--workers 6" in written, (
            f"launch.json workers must win; got: {written!r}"
        )
        assert result is not None and result.get("port") == 9001

    def test_apply_falls_back_to_config_json_when_launch_absent(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC4: APPLY uses config.json values when launch.json absent."""
        (unit_dir / "cidx-server.service").write_text(_MAIN_PY_UNIT)
        launch = tmp_path / "launch.json"  # not created
        applied = tmp_path / "applied_launch.json"  # not created
        config = tmp_path / "config.json"
        config.write_text(
            json.dumps({"host": "192.168.1.1", "port": 7777, "workers": 3})
        )

        result, written = _run_mode(
            executor,
            unit_dir,
            mode="APPLY",
            launch_path=launch,
            applied_path=applied,
            cidx_data_dir=tmp_path,
        )

        assert written is not None
        assert "--port 7777" in written, (
            f"config.json port must be used; got: {written!r}"
        )
        assert result is not None and result.get("workers") == 3

    def test_apply_uses_serverconfig_defaults_when_both_absent(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC4: APPLY uses ServerConfig defaults (port=8000 NOT 8090) when all sources absent."""
        # Unit has port=9999 so the default rewrite is visible
        unit = """\
[Unit]
Description=CIDX Server

[Service]
ExecStart=/usr/bin/python3 -m code_indexer.server.main --host 9.9.9.9 --port 9999

[Install]
WantedBy=multi-user.target
"""
        (unit_dir / "cidx-server.service").write_text(unit)
        launch = tmp_path / "launch.json"  # not created
        applied = tmp_path / "applied_launch.json"  # not created
        # config.json absent too

        result, written = _run_mode(
            executor,
            unit_dir,
            mode="APPLY",
            launch_path=launch,
            applied_path=applied,
            cidx_data_dir=tmp_path,
        )

        assert result is not None, "Must return snapshot even from defaults"
        assert result.get("port") == 8000, (
            f"Default port MUST be 8000 (ServerConfig), NOT 8090 (main.py argparse). "
            f"Got: {result.get('port')!r}"
        )
        assert result.get("workers") == 1, (
            f"Default workers must be 1; got: {result.get('workers')!r}"
        )

    def test_apply_workers_floor_is_one(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC4: workers value is floored at 1 even if source has 0 or negative."""
        (unit_dir / "cidx-server.service").write_text(_MAIN_PY_UNIT)
        launch = tmp_path / "launch.json"
        launch.write_text(json.dumps({"host": "127.0.0.1", "port": 8000, "workers": 0}))
        applied = tmp_path / "applied_launch.json"

        result, written = _run_mode(
            executor,
            unit_dir,
            mode="APPLY",
            launch_path=launch,
            applied_path=applied,
            cidx_data_dir=tmp_path,
        )

        assert result is not None
        assert result.get("workers") >= 1, (
            f"workers must be floored at 1; got: {result.get('workers')!r}"
        )

    def test_deploy_uses_applied_not_config_json(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC4: DEPLOY uses applied_launch.json values, not config.json."""
        (unit_dir / "cidx-server.service").write_text(_MAIN_PY_UNIT)
        applied = tmp_path / "applied_launch.json"
        applied.write_text(
            json.dumps({"host": "127.0.0.1", "port": 6666, "workers": 2})
        )
        launch = tmp_path / "launch.json"  # not created — DEPLOY must never read it
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"host": "1.2.3.4", "port": 5555, "workers": 9}))

        _, written = _run_mode(
            executor,
            unit_dir,
            mode="DEPLOY",
            launch_path=launch,
            applied_path=applied,
            cidx_data_dir=tmp_path,
        )

        assert written is not None
        assert "--port 6666" in written, (
            f"DEPLOY must use applied_launch.json port (6666), not config.json (5555). "
            f"Got: {written!r}"
        )

    def test_apply_db_free_no_serverconfig_manager_called(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """AC4: APPLY resolution is DB-free — ServerConfigManager must NOT be instantiated."""
        (unit_dir / "cidx-server.service").write_text(_MAIN_PY_UNIT)
        launch = tmp_path / "launch.json"
        launch.write_text(json.dumps({"host": "127.0.0.1", "port": 9001, "workers": 2}))

        with patch(
            "code_indexer.server.auto_update.deployment_executor.subprocess.run",
            side_effect=lambda cmd, **kw: MagicMock(returncode=0, stderr="", stdout=""),
        ):
            with patch(
                "code_indexer.server.utils.config_manager.ServerConfigManager.__init__",
                side_effect=AssertionError(
                    "DB-free violation: ServerConfigManager must NOT be called in _ensure_launch_config"
                ),
            ):
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.LAUNCH_CONFIG_PATH",
                    launch,
                ):
                    with patch(
                        "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                        unit_dir,
                    ):
                        # Must complete without raising AssertionError
                        result = executor._ensure_launch_config("APPLY")

        assert result is not None, "APPLY must succeed without ServerConfigManager"
