"""Auto-updater self-heal: deployed uvicorn units gain --no-server-header.

`sudo tee` / `systemctl daemon-reload` are replaced by a fake whose tee
really writes the unit file, so idempotency is checked against the file.
"""

import subprocess
from pathlib import Path
from typing import Any, List
from unittest.mock import patch

import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor

_MODULE = "code_indexer.server.auto_update.deployment_executor"
_FLAG = "--no-server-header"

_UVICORN_EXEC = (
    "ExecStart=/opt/venv/bin/python3 -m uvicorn code_indexer.server.app:app "
    "--host 0.0.0.0 --port 8000 --log-level info --workers 2"
)
_MAIN_EXEC = (
    "ExecStart=/usr/bin/python3 -m code_indexer.server.main --host 0.0.0.0 --port 8000"
)


def _unit(execstart: str) -> str:
    return (
        "[Unit]\nDescription=CIDX Server\n\n[Service]\n"
        'Environment="CIDX_SERVER_MODE=1"\n'
        f"{execstart}\nRestart=always\n\n[Install]\nWantedBy=multi-user.target\n"
    )


class _FakeSystemctl:
    """Stands in for `sudo tee <path>` (writes the file) and daemon-reload."""

    def __init__(self, tee_rc: int = 0) -> None:
        self.calls: List[List[str]] = []
        self.tee_rc = tee_rc

    def __call__(self, cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        if cmd[:2] == ["sudo", "tee"]:
            if self.tee_rc == 0:
                Path(cmd[2]).write_text(kwargs["input"])
            return subprocess.CompletedProcess(cmd, self.tee_rc, "", "tee failed")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def tee_calls(self) -> List[List[str]]:
        return [c for c in self.calls if c[:2] == ["sudo", "tee"]]


def _execstart(unit_file: Path) -> str:
    return next(
        ln for ln in unit_file.read_text().splitlines() if ln.startswith("ExecStart=")
    )


def _run(tmp_path: Path, fake: _FakeSystemctl) -> bool:
    executor = DeploymentExecutor(repo_path=tmp_path, service_name="cidx-server")
    with patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", tmp_path):
        with patch(f"{_MODULE}.subprocess.run", fake):
            return executor._ensure_no_server_header()


@pytest.fixture
def unit_file(tmp_path: Path) -> Path:
    path = tmp_path / "cidx-server.service"
    path.write_text(_unit(_UVICORN_EXEC))
    return path


def test_adds_flag_and_preserves_other_arguments(tmp_path, unit_file):
    fake = _FakeSystemctl()
    assert _run(tmp_path, fake) is True
    assert _execstart(unit_file) == f"{_UVICORN_EXEC} {_FLAG}"
    assert 'Environment="CIDX_SERVER_MODE=1"' in unit_file.read_text()
    assert ["sudo", "systemctl", "daemon-reload"] in fake.calls


def test_second_run_is_a_no_op(tmp_path, unit_file):
    _run(tmp_path, _FakeSystemctl())
    second = _FakeSystemctl()
    assert _run(tmp_path, second) is True
    assert second.calls == []
    assert _execstart(unit_file).split().count(_FLAG) == 1


def test_unit_already_carrying_flag_is_not_rewritten(tmp_path, unit_file):
    unit_file.write_text(_unit(f"{_UVICORN_EXEC} {_FLAG}"))
    fake = _FakeSystemctl()
    assert _run(tmp_path, fake) is True
    assert fake.calls == []


def test_server_main_unit_is_left_untouched(tmp_path, unit_file):
    unit_file.write_text(_unit(_MAIN_EXEC))
    fake = _FakeSystemctl()
    assert _run(tmp_path, fake) is True
    assert fake.calls == []
    assert _execstart(unit_file) == _MAIN_EXEC


def test_missing_unit_reports_failure_without_writing(tmp_path):
    fake = _FakeSystemctl()
    assert _run(tmp_path, fake) is False
    assert fake.calls == []


def test_failed_tee_reports_failure(tmp_path, unit_file):
    fake = _FakeSystemctl(tee_rc=1)
    assert _run(tmp_path, fake) is False
    assert len(fake.tee_calls()) == 1
    assert _execstart(unit_file) == _UVICORN_EXEC


_CONTINUED_WITHOUT_FLAG = (
    "ExecStart=/opt/venv/bin/python3 -m uvicorn code_indexer.server.app:app \\\n"
    "    --host 0.0.0.0 --port 8000 \\\n"
    "    --workers 2"
)


def test_continued_execstart_without_flag_is_refused_untouched(
    tmp_path, unit_file, caplog
):
    unit_file.write_text(_unit(_CONTINUED_WITHOUT_FLAG))
    before = unit_file.read_bytes()
    caplog.set_level("WARNING")
    for _ in range(2):
        fake = _FakeSystemctl()
        assert _run(tmp_path, fake) is False
        assert fake.calls == []
        assert unit_file.read_bytes() == before
    assert "DEPLOY-GENERAL-229" in caplog.text


def test_continued_execstart_with_flag_on_continuation_is_a_no_op(tmp_path, unit_file):
    unit_file.write_text(_unit(f"{_CONTINUED_WITHOUT_FLAG} {_FLAG}"))
    fake = _FakeSystemctl()
    assert _run(tmp_path, fake) is True
    assert fake.calls == []


def test_flag_and_uvicorn_only_on_continuation_lines_is_a_no_op(tmp_path, unit_file):
    unit_file.write_text(
        _unit(
            "ExecStart=/opt/venv/bin/python3 \\\n"
            "    -m uvicorn code_indexer.server.app:app \\\n"
            f"    --workers 2 {_FLAG}"
        )
    )
    fake = _FakeSystemctl()
    assert _run(tmp_path, fake) is True
    assert fake.calls == []


_REPO = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize(
    "source",
    [
        _REPO / "scripts/install-cidx-server.sh",
        _REPO / "deployment/cidx-server.service",
    ],
    ids=lambda p: p.name,
)
def test_shipped_execstart_shape_is_restored_exactly(tmp_path, unit_file, source):
    shipped = next(
        ln for ln in source.read_text().splitlines() if ln.startswith("ExecStart=")
    )
    unit_file.write_text(_unit(shipped.replace(f" {_FLAG}", "")))
    assert _run(tmp_path, _FakeSystemctl()) is True
    assert _execstart(unit_file) == shipped


def test_execute_runs_the_self_heal(tmp_path):
    from tests.unit.server.auto_update.test_ensure_codex_cli_installed_845 import (
        _patch_execute_siblings,
    )

    executor = DeploymentExecutor(repo_path=tmp_path, service_name="cidx-server")
    with _patch_execute_siblings(executor):
        with patch.object(executor, "_ensure_codex_cli_installed", return_value=True):
            with patch.object(
                executor, "_ensure_no_server_header", return_value=True
            ) as heal:
                executor.execute()
    heal.assert_called_once_with()
