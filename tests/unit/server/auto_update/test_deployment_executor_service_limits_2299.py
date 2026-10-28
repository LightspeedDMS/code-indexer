# ^ why subprocess.run strips sudo and runs real commands, not mocks; only systemctl is recorded; #2299

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List

import pytest

from code_indexer.server.auto_update import deployment_executor as de
from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor
from code_indexer.server.auto_update.service_unit_limits import (
    COREDUMP_DROPIN_NAME,
    COREDUMP_TEMPLATE,
    SERVER_LIMITS_DROPIN_NAME,
    SERVER_LIMITS_TEMPLATE,
    TEMPLATE_SUBDIR,
)
from tests.unit.server.auto_update.test_deploy_optional_steps_nonfatal_1254 import (
    _patched_execute,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
_real_run = subprocess.run


class _SudoStripper:
    def __init__(self) -> None:
        self.systemctl_calls: List[List[str]] = []
        self.sudo_prefixed: List[bool] = []

    def __call__(self, cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.sudo_prefixed.append(cmd[0] == "sudo")
        argv = cmd[1:] if cmd[0] == "sudo" else cmd
        if argv[0] == "systemctl":
            self.systemctl_calls.append(list(argv))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return _real_run(argv, **kwargs)


@pytest.fixture
def unit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "system"
    path.mkdir()
    monkeypatch.setattr(de, "SYSTEMD_UNIT_DIR", path)
    return path


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> _SudoStripper:
    stripper = _SudoStripper()
    monkeypatch.setattr(de.subprocess, "run", stripper)
    return stripper


def test_ensure_service_limits_writes_drop_ins_with_sudo_and_reloads(
    unit_dir: Path, runner: _SudoStripper
) -> None:
    executor = DeploymentExecutor(repo_path=REPO_ROOT, service_name="cidx-server")

    assert executor._ensure_service_limits() is True

    limits = unit_dir / "cidx-server.service.d" / SERVER_LIMITS_DROPIN_NAME
    coredump = unit_dir.parent / "coredump.conf.d" / COREDUMP_DROPIN_NAME
    assert "LimitNOFILE=65536:524288" in limits.read_text().splitlines()
    assert "Storage=external" in coredump.read_text().splitlines()
    assert runner.sudo_prefixed and all(runner.sudo_prefixed)
    assert runner.systemctl_calls == [["systemctl", "daemon-reload"]]

    runner.systemctl_calls.clear()
    assert executor._ensure_service_limits() is True
    assert runner.systemctl_calls == []


def test_ensure_service_limits_without_templates_is_logged_not_raised(
    unit_dir: Path,
    runner: _SudoStripper,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = DeploymentExecutor(repo_path=tmp_path / "no-repo")

    with caplog.at_level("WARNING"):
        assert executor._ensure_service_limits() is False

    assert "DEPLOY-GENERAL-230" in caplog.text
    assert runner.sudo_prefixed == []


def test_ensure_service_limits_with_undecodable_template_is_logged_not_raised(
    unit_dir: Path,
    runner: _SudoStripper,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = tmp_path / "repo"
    templates = repo / TEMPLATE_SUBDIR
    templates.mkdir(parents=True)
    (templates / SERVER_LIMITS_TEMPLATE).write_bytes(b"[Service]\n\xff\xfe\n")
    (templates / COREDUMP_TEMPLATE).write_text("[Coredump]\n")
    executor = DeploymentExecutor(repo_path=repo)

    with caplog.at_level("WARNING"):
        assert executor._ensure_service_limits() is False

    assert "DEPLOY-GENERAL-230" in caplog.text
    assert runner.sudo_prefixed == []


def test_execute_runs_service_limits_step_and_survives_its_failure() -> None:
    executor = DeploymentExecutor(repo_path=Path("/test/repo"))
    with _patched_execute(executor, {"_ensure_service_limits": False}):
        assert executor.execute() is True
        executor._ensure_service_limits.assert_called_once()  # type: ignore[attr-defined]
        executor._ensure_rust_toolchain.assert_called_once()  # type: ignore[attr-defined]
