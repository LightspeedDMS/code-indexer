"""
TDD tests for Story #1199 AC5: service.py wiring.

- service.py restart-signal handler calls _ensure_launch_config("APPLY"), not _ensure_workers_config
- ensure failure → skip restart, no applied_launch.json
- ensure success + restart success → applied_launch.json written
- ensure success + restart failure → applied_launch.json NOT written
- Idempotent rewrite: APPLY returns truthy snapshot on no-op

Behavioral: real DeploymentExecutor + real AutoUpdateService logic, subprocess patched.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _fresh_signal(signal_path: Path) -> None:
    """Write a valid, fresh restart signal JSON to signal_path."""
    signal_path.write_text(
        json.dumps({"timestamp": datetime.now().isoformat(), "generation": 1})
    )


def _make_service(tmp_path: Path) -> Any:
    """Return a minimal AutoUpdateService stub wired for poll_once restart-signal path."""
    from code_indexer.server.auto_update.service import AutoUpdateService

    service = AutoUpdateService.__new__(AutoUpdateService)
    service.deployment_executor = MagicMock()
    service.change_detector = MagicMock()
    service.deployment_lock = MagicMock()
    service.current_state = MagicMock()
    service.last_deployment = None
    service.last_error = None
    return service


@pytest.fixture
def executor(tmp_path: Path) -> Any:
    from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor

    return DeploymentExecutor(repo_path=tmp_path, service_name="cidx-server")


class TestAC5ServicePy:
    """AC5: service.py must check _ensure_launch_config return and guard applied_launch.json."""

    def test_service_calls_ensure_launch_config_not_workers(
        self, tmp_path: Path
    ) -> None:
        """AC5: service.py poll_once calls _ensure_launch_config('APPLY'), not _ensure_workers_config.

        The restart-signal handler must use APPLY mode (reads launch.json / TARGET).
        DEPLOY always returns None, which would cause the 'if not launch_snapshot' guard
        to fire unconditionally and skip restart_server() entirely — bricking admin restarts.
        """
        signal_path = tmp_path / "restart.signal"
        _fresh_signal(signal_path)
        applied_path = tmp_path / "applied_launch.json"

        ensure_calls: list = []
        workers_calls: list = []

        service = _make_service(tmp_path)

        def _fake_ensure(mode: str) -> dict:
            ensure_calls.append(mode)
            return {"host": "127.0.0.1", "port": 8000, "workers": 1}

        def _fake_workers() -> bool:
            workers_calls.append(True)
            return True

        service.deployment_executor._ensure_launch_config.side_effect = _fake_ensure
        service.deployment_executor._ensure_workers_config = _fake_workers
        service.deployment_executor.restart_server.return_value = True

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_path
        ):
            with patch(
                "code_indexer.server.auto_update.service.APPLIED_LAUNCH_CONFIG_PATH",
                applied_path,
            ):
                service.poll_once()

        assert ensure_calls == ["APPLY"], (
            f"service.py restart-signal handler must call _ensure_launch_config('APPLY'), "
            f"not 'DEPLOY'; got: {ensure_calls}. "
            f"DEPLOY always returns None so restart would be unconditionally skipped."
        )
        assert not workers_calls, (
            f"service.py must NOT call _ensure_workers_config; got: {workers_calls}"
        )

    def test_ensure_failure_skips_restart(self, tmp_path: Path) -> None:
        """AC5: When _ensure_launch_config returns None, restart_server must NOT be called."""
        signal_path = tmp_path / "restart.signal"
        _fresh_signal(signal_path)
        applied_path = tmp_path / "applied_launch.json"

        service = _make_service(tmp_path)
        service.deployment_executor._ensure_launch_config.return_value = None

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_path
        ):
            with patch(
                "code_indexer.server.auto_update.service.APPLIED_LAUNCH_CONFIG_PATH",
                applied_path,
            ):
                service.poll_once()

        service.deployment_executor.restart_server.assert_not_called()
        assert not applied_path.exists(), (
            "applied_launch.json must NOT be written when ensure fails"
        )

    def test_ensure_success_restart_success_writes_applied(
        self, tmp_path: Path
    ) -> None:
        """AC5: ensure success + restart success → applied_launch.json written with snapshot."""
        signal_path = tmp_path / "restart.signal"
        _fresh_signal(signal_path)
        applied_path = tmp_path / "applied_launch.json"

        snapshot = {"host": "127.0.0.1", "port": 9001, "workers": 4}
        service = _make_service(tmp_path)
        service.deployment_executor._ensure_launch_config.return_value = snapshot
        service.deployment_executor.restart_server.return_value = True

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_path
        ):
            with patch(
                "code_indexer.server.auto_update.service.APPLIED_LAUNCH_CONFIG_PATH",
                applied_path,
            ):
                service.poll_once()

        assert applied_path.exists(), "applied_launch.json must be written on success"
        written = json.loads(applied_path.read_text())
        assert written.get("workers") == 4, f"Snapshot written incorrectly: {written}"
        assert written.get("port") == 9001

    def test_ensure_success_restart_failure_no_applied(self, tmp_path: Path) -> None:
        """AC5: ensure success + restart failure → applied_launch.json NOT written."""
        signal_path = tmp_path / "restart.signal"
        _fresh_signal(signal_path)
        applied_path = tmp_path / "applied_launch.json"

        snapshot = {"host": "127.0.0.1", "port": 8000, "workers": 2}
        service = _make_service(tmp_path)
        service.deployment_executor._ensure_launch_config.return_value = snapshot
        service.deployment_executor.restart_server.return_value = False

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_path
        ):
            with patch(
                "code_indexer.server.auto_update.service.APPLIED_LAUNCH_CONFIG_PATH",
                applied_path,
            ):
                service.poll_once()

        assert not applied_path.exists(), (
            "applied_launch.json must NOT be written when restart fails"
        )

    def test_apply_idempotent_rewrite_returns_truthy(
        self, executor: Any, tmp_path: Path
    ) -> None:
        """AC5: APPLY returns truthy snapshot even when ExecStart already matches (no-op)."""
        unit_dir = tmp_path / "systemd"
        unit_dir.mkdir()
        unit = """\
[Unit]
Description=CIDX Server

[Service]
ExecStart=/usr/bin/python3 -m code_indexer.server.main --host 127.0.0.1 --port 9001 --workers 3

[Install]
WantedBy=multi-user.target
"""
        (unit_dir / "cidx-server.service").write_text(unit)
        launch = tmp_path / "launch.json"
        launch.write_text(json.dumps({"host": "127.0.0.1", "port": 9001, "workers": 3}))

        with patch(
            "code_indexer.server.auto_update.deployment_executor.subprocess.run",
            side_effect=lambda cmd, **kw: MagicMock(returncode=0, stderr="", stdout=""),
        ):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.LAUNCH_CONFIG_PATH",
                launch,
            ):
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
                    unit_dir,
                ):
                    result = executor._ensure_launch_config("APPLY")

        assert result, f"APPLY no-op must return truthy snapshot; got: {result!r}"
        assert isinstance(result, dict)
        assert result.get("port") == 9001
