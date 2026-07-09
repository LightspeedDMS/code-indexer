"""Story #1196: next-release transition cleanup for launch-config precedence.

Removes the Story #1199 config.json rung from BOTH _ensure_launch_config modes:
  APPLY:  launch.json -> ServerConfig defaults (NO config.json).
  DEPLOY: applied_launch.json -> parse/preserve the CURRENT live-unit ExecStart
          -> ServerConfig defaults.  NEVER launch.json / TARGET (FIX-1 / MAJOR-M1).

FIX-1 proof test (a) lives here: a saved-but-unconfirmed TARGET is NOT applied
by a DEPLOY after cleanup.  FIX-1 proof test (b) lives in
tests/unit/server/services/test_applied_worker_count_1197.py (resolver
side: a node missing applied_launch.json falls to the ServerConfig default 1,
not to config.json).

Behavioral: real temp files, patched subprocess only.  DB-free.
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


class TestFix1aDeployNeverAppliesSavedTarget:
    """FIX-1 proof test (a): a saved TARGET is NOT applied by DEPLOY after cleanup.

    Story #1196 removes the config.json rung, but must NOT re-introduce
    launch.json/TARGET into the DEPLOY chain (MAJOR-M1) -- otherwise a routine
    code deploy would silently apply a saved-but-unconfirmed launch change.
    """

    def test_deploy_uses_applied_not_saved_target_when_both_present(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """Saved TARGET (launch.json workers=8) alongside applied_launch.json
        workers=4 -- DEPLOY must rewrite from applied (4), never the TARGET (8)."""
        (unit_dir / "cidx-server.service").write_text(_MAIN_PY_UNIT)
        launch = tmp_path / "launch.json"
        launch.write_text(json.dumps({"host": "10.0.0.9", "port": 9999, "workers": 8}))
        applied = tmp_path / "applied_launch.json"
        applied.write_text(json.dumps({"host": "0.0.0.0", "port": 8000, "workers": 4}))

        result, written = _run_mode(
            executor,
            unit_dir,
            mode="DEPLOY",
            launch_path=launch,
            applied_path=applied,
            cidx_data_dir=tmp_path,
        )

        assert result is None, "DEPLOY must always return None"
        assert written is not None
        assert "--workers 4" in written, (
            f"DEPLOY must apply applied_launch.json (workers=4), never the saved "
            f"TARGET (workers=8). Got: {written!r}"
        )
        assert "--workers 8" not in written, (
            f"DEPLOY must NOT apply the saved TARGET workers=8. Got: {written!r}"
        )
        assert "--port 9999" not in written, (
            f"DEPLOY must NOT apply the saved TARGET port=9999. Got: {written!r}"
        )

    def test_deploy_with_applied_absent_preserves_live_not_saved_target(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """applied_launch.json ABSENT + a saved TARGET present -> DEPLOY preserves
        the live ExecStart (no rewrite), never adopts the TARGET."""
        (unit_dir / "cidx-server.service").write_text(_MAIN_PY_UNIT)
        launch = tmp_path / "launch.json"
        launch.write_text(json.dumps({"host": "10.0.0.9", "port": 9999, "workers": 8}))
        applied = tmp_path / "applied_launch.json"  # deliberately absent

        result, written = _run_mode(
            executor,
            unit_dir,
            mode="DEPLOY",
            launch_path=launch,
            applied_path=applied,
            cidx_data_dir=tmp_path,
        )

        assert result is None
        assert written is None, (
            "DEPLOY + missing applied_launch.json must preserve the live "
            f"ExecStart (no rewrite), never adopt the saved TARGET. Got: {written!r}"
        )


class TestAC2ApplyDropsConfigJsonRung:
    """AC2: post-cleanup APPLY chain is launch.json -> ServerConfig defaults."""

    def test_apply_falls_through_to_serverconfig_defaults_when_launch_absent(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """launch.json absent + config.json present -> APPLY uses ServerConfig
        defaults, NOT config.json (the config.json rung is removed)."""
        unit = """\
[Unit]
Description=CIDX Server

[Service]
ExecStart=/usr/bin/python3 -m code_indexer.server.main --host 9.9.9.9 --port 9999

[Install]
WantedBy=multi-user.target
"""
        (unit_dir / "cidx-server.service").write_text(unit)
        launch = tmp_path / "launch.json"  # absent
        applied = tmp_path / "applied_launch.json"
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

        assert result is not None
        assert result.get("port") == 8000, (
            f"Story #1196: config.json rung removed -- APPLY must use ServerConfig "
            f"default port=8000, NOT config.json's 7777. Got: {result.get('port')!r}"
        )
        assert result.get("host") == "127.0.0.1", (
            f"APPLY must use ServerConfig default host, not config.json's "
            f"192.168.1.1. Got: {result.get('host')!r}"
        )
        assert result.get("workers") == 1, (
            f"APPLY must use ServerConfig default workers=1, not config.json's "
            f"3. Got: {result.get('workers')!r}"
        )


class TestAC2DeployFillsFromLiveExecstartNotConfigJson:
    """AC2: DEPLOY's missing-field fill source is the live ExecStart, never config.json."""

    def test_deploy_partial_applied_fills_missing_field_from_live_execstart(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """applied_launch.json has host/workers but no port -- the missing port
        must be filled from the LIVE ExecStart, never config.json."""
        unit = """\
[Unit]
Description=CIDX Server

[Service]
ExecStart=/usr/bin/python3 -m code_indexer.server.main --host 0.0.0.0 --port 5050 --workers 2

[Install]
WantedBy=multi-user.target
"""
        (unit_dir / "cidx-server.service").write_text(unit)
        applied = tmp_path / "applied_launch.json"
        applied.write_text(json.dumps({"host": "0.0.0.0", "workers": 6}))  # no port
        launch = tmp_path / "launch.json"
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"port": 7777}))  # must NOT be used

        result, written = _run_mode(
            executor,
            unit_dir,
            mode="DEPLOY",
            launch_path=launch,
            applied_path=applied,
            cidx_data_dir=tmp_path,
        )

        assert result is None, "DEPLOY always returns None"
        assert written is not None
        assert "--port 5050" in written, (
            f"Missing port must be filled from the LIVE ExecStart (5050), not "
            f"config.json (7777). Got: {written!r}"
        )
        assert "--port 7777" not in written, (
            f"DEPLOY must NOT read config.json for the missing port. Got: {written!r}"
        )
        assert "--workers 6" in written, f"applied workers=6 must win. Got: {written!r}"
