"""
TDD tests for Story #1199 production-safety defect:
DEPLOY mode + applied_launch.json MISSING must preserve the live ExecStart,
NOT rewrite it from config.json / ServerConfig defaults.

Context:
  - Real cluster deployments have live ExecStart with --host 0.0.0.0
    (hardcoded by the installer so HAProxy on another host can reach the node).
  - config.json has host=127.0.0.1 (ServerConfig default, never overwritten
    by the installer).
  - A routine DEPLOY with applied_launch.json absent would previously rewrite
    --host 0.0.0.0 -> 127.0.0.1, binding loopback-only and dropping the node
    off the load balancer (production outage).

Fix:
  - DEPLOY + applied_launch.json MISSING -> preserve live ExecStart (same as
    CORRUPT path), do NOT fall through to config.json / defaults.
  - DEPLOY + applied_launch.json PRESENT (valid) -> still use applied values
    (unchanged regression guard).
  - DEPLOY + applied_launch.json CORRUPT -> still preserve live ExecStart
    (unchanged regression guard).
  - APPLY mode UNCHANGED: still applies the TARGET from launch.json.

Behavioral: real temp files, subprocess patched.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# A realistic cluster unit ExecStart — binds 0.0.0.0 so HAProxy can reach it.
_CLUSTER_UNIT = """\
[Unit]
Description=CIDX Multi-User Server with MCP Integration
After=network.target

[Service]
Type=simple
User=code-indexer
WorkingDirectory=/opt/code-indexer
Environment="PATH=/home/code-indexer/.local/bin:/usr/local/bin:/usr/bin"
Environment="PYTHONUNBUFFERED=1"
ExecStart=/opt/code-indexer/venv/bin/python3 -m code_indexer.server.main \
--host 0.0.0.0 --port 8000 --workers 4
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=cidx-server

[Install]
WantedBy=multi-user.target
"""

# config.json as written by the installer — host defaults to 127.0.0.1
_CONFIG_JSON_WITH_127 = json.dumps({"host": "127.0.0.1", "port": 8000, "workers": 4})


@pytest.fixture
def unit_dir(tmp_path: Path) -> Path:
    d = tmp_path / "systemd"
    d.mkdir()
    return d


@pytest.fixture
def executor(tmp_path: Path) -> Any:
    from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor

    return DeploymentExecutor(repo_path=tmp_path, service_name="cidx-server")


def _write_cluster_unit(unit_dir: Path, content: str = _CLUSTER_UNIT) -> None:
    (unit_dir / "cidx-server.service").write_text(content)


def _run_deploy(
    executor: Any,
    unit_dir: Path,
    applied_path: Path,
    launch_path: Path,
    config_json_path: Path,
) -> tuple:
    """Run DEPLOY mode; patch subprocess + all config paths.

    Returns (result, tee_written_or_None).
    """
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
            "code_indexer.server.auto_update.deployment_executor.APPLIED_LAUNCH_CONFIG_PATH",
            applied_path,
        ):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.LAUNCH_CONFIG_PATH",
                launch_path,
            ):
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                    unit_dir,
                ):
                    with patch(
                        "code_indexer.server.auto_update.deployment_executor._cidx_data_dir",
                        config_json_path.parent,
                    ):
                        result = executor._ensure_launch_config("DEPLOY")

    return result, (tee_written[0] if tee_written else None)


class TestDeployMissingAppliedPreservesLiveHost:
    """CORE REGRESSION: DEPLOY + missing applied_launch.json preserves live ExecStart."""

    def test_deploy_missing_applied_config127_live_0000_stays_0000(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """THE regression test: DEPLOY + absent applied + config.json host=127.0.0.1
        + live ExecStart --host 0.0.0.0 -> --host MUST STAY 0.0.0.0.

        Before the fix: ExecStart is rewritten to --host 127.0.0.1, dropping the node
        off HAProxy (production outage).
        After the fix: ExecStart is preserved unchanged.
        """
        _write_cluster_unit(unit_dir)

        applied = tmp_path / "applied_launch.json"  # deliberately NOT created
        launch = tmp_path / "launch.json"  # also absent — DEPLOY ignores it anyway

        # config.json has host=127.0.0.1 (the installer default that was never aligned)
        config_json = tmp_path / "config.json"
        config_json.write_text(_CONFIG_JSON_WITH_127)

        result, written = _run_deploy(executor, unit_dir, applied, launch, config_json)

        assert result is None, f"DEPLOY must always return None; got: {result!r}"
        assert written is None, (
            "DEPLOY + missing applied_launch.json MUST preserve live ExecStart "
            "(no tee rewrite). The live unit binds --host 0.0.0.0 for HAProxy; "
            "rewriting to 127.0.0.1 (config.json default) drops the node off the "
            f"load balancer. Got written: {written!r}"
        )

    def test_deploy_missing_applied_live_workers4_stays_4(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """DEPLOY + missing applied: --workers is also preserved (whole ExecStart preserved)."""
        _write_cluster_unit(unit_dir)

        applied = tmp_path / "applied_launch.json"  # absent
        launch = tmp_path / "launch.json"

        # config.json has workers=1 (default) while live ExecStart has --workers 4
        config_json = tmp_path / "config.json"
        config_json.write_text(
            json.dumps({"host": "127.0.0.1", "port": 8000, "workers": 1})
        )

        result, written = _run_deploy(executor, unit_dir, applied, launch, config_json)

        assert result is None
        assert written is None, (
            "DEPLOY + missing applied must preserve live ExecStart (including --workers 4). "
            f"Got written: {written!r}"
        )


class TestDeployMissingVsCorruptAreEquivalent:
    """MISSING and CORRUPT applied_launch.json both preserve the live ExecStart."""

    def test_corrupt_preserves_live_unchanged(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """Regression guard: CORRUPT applied still preserves live ExecStart (unchanged)."""
        _write_cluster_unit(unit_dir)

        applied = tmp_path / "applied_launch.json"
        applied.write_text("not valid json {{{")

        config_json = tmp_path / "config.json"
        config_json.write_text(_CONFIG_JSON_WITH_127)
        launch = tmp_path / "launch.json"

        result, written = _run_deploy(executor, unit_dir, applied, launch, config_json)

        assert result is None
        assert written is None, (
            f"Corrupt applied must preserve live ExecStart. Got written: {written!r}"
        )

    def test_missing_preserves_live_same_as_corrupt(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """After fix: MISSING applied behaves identically to CORRUPT (both preserve live unit)."""
        _write_cluster_unit(unit_dir)

        applied = tmp_path / "applied_launch.json"  # absent
        config_json = tmp_path / "config.json"
        config_json.write_text(_CONFIG_JSON_WITH_127)
        launch = tmp_path / "launch.json"

        result, written = _run_deploy(executor, unit_dir, applied, launch, config_json)

        assert result is None, f"DEPLOY must always return None; got: {result!r}"
        assert written is None, (
            "After fix: DEPLOY + missing applied must preserve live ExecStart, "
            f"same as CORRUPT. Got written: {written!r}"
        )


class TestDeployAppliedPresentStillUsed:
    """Regression guard: DEPLOY + valid applied_launch.json still uses applied values."""

    def test_deploy_applied_present_uses_applied_values(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """REGRESSION GUARD: When applied_launch.json EXISTS and is valid, DEPLOY uses it."""
        _write_cluster_unit(unit_dir)

        applied = tmp_path / "applied_launch.json"
        applied.write_text(json.dumps({"host": "0.0.0.0", "port": 8000, "workers": 8}))

        config_json = tmp_path / "config.json"
        config_json.write_text(_CONFIG_JSON_WITH_127)
        launch = tmp_path / "launch.json"

        result, written = _run_deploy(executor, unit_dir, applied, launch, config_json)

        assert result is None, f"DEPLOY must always return None; got: {result!r}"
        # applied_launch.json says workers=8; live unit says workers=4 → must rewrite
        assert written is not None, (
            "DEPLOY + valid applied must rewrite ExecStart when values differ"
        )
        assert "--workers 8" in written, (
            f"DEPLOY must use applied_launch.json workers (8). Got: {written!r}"
        )

    def test_deploy_applied_present_host_from_applied(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """REGRESSION GUARD: applied_launch.json host is used (not config.json host)."""
        _write_cluster_unit(unit_dir)

        applied = tmp_path / "applied_launch.json"
        # applied says 127.0.0.1 (e.g. dev node where this was intentional)
        applied.write_text(
            json.dumps({"host": "127.0.0.1", "port": 8000, "workers": 4})
        )

        config_json = tmp_path / "config.json"
        config_json.write_text(
            json.dumps({"host": "192.168.1.10", "port": 8000, "workers": 4})
        )
        launch = tmp_path / "launch.json"

        result, written = _run_deploy(executor, unit_dir, applied, launch, config_json)

        assert result is None
        # live unit has 0.0.0.0; applied says 127.0.0.1 → rewrite to applied value
        assert written is not None, (
            "DEPLOY + applied (host=127.0.0.1) vs live (host=0.0.0.0) must rewrite"
        )
        assert "--host 127.0.0.1" in written, (
            f"Must use applied_launch.json host (127.0.0.1). Got: {written!r}"
        )
        # Must NOT use config.json host
        assert "--host 192.168.1.10" not in written, (
            f"Must NOT use config.json host (192.168.1.10). Got: {written!r}"
        )


class TestApplyModeUnchanged:
    """REGRESSION: APPLY mode behavior is NOT affected by the DEPLOY fix."""

    def test_apply_mode_applies_launch_json_target(
        self, executor: Any, unit_dir: Path, tmp_path: Path
    ) -> None:
        """APPLY reads launch.json and applies it — unchanged by the DEPLOY fix."""
        _write_cluster_unit(unit_dir)

        # launch.json has the target config to apply
        launch = tmp_path / "launch.json"
        launch.write_text(
            json.dumps(
                {
                    "host": "0.0.0.0",
                    "port": 9001,
                    "workers": 6,
                    "target_restart_generation": 3,
                }
            )
        )
        applied = tmp_path / "applied_launch.json"  # absent (APPLY reads launch.json)
        config_json = tmp_path / "config.json"
        config_json.write_text(_CONFIG_JSON_WITH_127)

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
                "code_indexer.server.auto_update.deployment_executor.APPLIED_LAUNCH_CONFIG_PATH",
                applied,
            ):
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.LAUNCH_CONFIG_PATH",
                    launch,
                ):
                    with patch(
                        "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                        unit_dir,
                    ):
                        with patch(
                            "code_indexer.server.auto_update.deployment_executor._cidx_data_dir",
                            tmp_path,
                        ):
                            result = executor._ensure_launch_config("APPLY")

        # APPLY returns a snapshot (not None) on success
        assert result is not None, (
            f"APPLY must return snapshot dict on success; got: {result!r}"
        )
        assert result.get("host") == "0.0.0.0", (
            f"Applied host must be 0.0.0.0; got: {result}"
        )
        assert result.get("port") == 9001, f"Applied port must be 9001; got: {result}"
        assert result.get("workers") == 6, f"Applied workers must be 6; got: {result}"
        # APPLY rewrites the ExecStart (live was port 8000 / workers 4; target is 9001 / 6)
        assert len(tee_written) == 1, (
            f"APPLY must rewrite ExecStart via tee; tee_written={tee_written}"
        )
        written = tee_written[0]
        assert "--port 9001" in written, (
            f"APPLY must write --port 9001; got: {written!r}"
        )
        assert "--workers 6" in written, (
            f"APPLY must write --workers 6; got: {written!r}"
        )
