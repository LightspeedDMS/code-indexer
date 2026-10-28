# ^ why real mkdir/tee/chmod/mv in temp dirs minus sudo, not mocks; only daemon-reload is recorded; #2299

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from code_indexer.server.auto_update.service_unit_limits import (
    COREDUMP_DROPIN_NAME,
    SERVER_LIMITS_DROPIN_NAME,
    ManagedConfFile,
    ensure_conf_files,
    managed_conf_files,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
SERVICE_NAME = "cidx-server"


class _Recorder:
    def __init__(self, reload_ok: bool = True) -> None:
        self.reloads = 0
        self.commands: List[List[str]] = []
        self.reload_ok = reload_ok

    def run(self, argv: List[str], stdin: Optional[str]) -> subprocess.CompletedProcess:
        self.commands.append(list(argv))
        return subprocess.run(argv, input=stdin, capture_output=True, text=True)

    def reload(self) -> bool:
        self.reloads += 1
        return self.reload_ok


Dirs = Tuple[Path, Path]


@pytest.fixture
def dirs(tmp_path: Path) -> Dirs:
    unit_dir = tmp_path / "system"
    coredump_dir = tmp_path / "coredump.conf.d"
    unit_dir.mkdir()
    return unit_dir, coredump_dir


def _files(dirs: Dirs) -> Tuple[ManagedConfFile, ...]:
    return managed_conf_files(REPO_ROOT, dirs[0], SERVICE_NAME)


def _limits_path(dirs: Dirs) -> Path:
    return dirs[0] / f"{SERVICE_NAME}.service.d" / SERVER_LIMITS_DROPIN_NAME


def _coredump_path(dirs: Dirs) -> Path:
    return dirs[1] / COREDUMP_DROPIN_NAME


def test_absent_files_are_written_and_daemon_reloaded(dirs) -> None:
    rec = _Recorder()

    assert ensure_conf_files(_files(dirs), rec.run, rec.reload) is True

    limits = _limits_path(dirs).read_text()
    assert "LimitNOFILE=65536:524288" in limits.splitlines()
    assert "LimitCORE=infinity" in limits.splitlines()
    coredump = _coredump_path(dirs).read_text().splitlines()
    assert "Storage=external" in coredump
    assert "ProcessSizeMax=16G" in coredump
    assert "ExternalSizeMax=16G" in coredump
    assert _limits_path(dirs).stat().st_mode & 0o777 == 0o644
    assert _coredump_path(dirs).stat().st_mode & 0o777 == 0o644
    assert rec.reloads == 1
    leftovers = [p.name for p in _limits_path(dirs).parent.iterdir()]
    assert leftovers == [SERVER_LIMITS_DROPIN_NAME]


def test_current_files_are_a_no_op(dirs) -> None:
    assert ensure_conf_files(_files(dirs), _Recorder().run, _Recorder().reload)
    rec = _Recorder()

    assert ensure_conf_files(_files(dirs), rec.run, rec.reload) is True

    assert rec.commands == []
    assert rec.reloads == 0


def test_drifted_limits_drop_in_is_rewritten_and_reloaded(dirs) -> None:
    files = _files(dirs)
    _limits_path(dirs).parent.mkdir(parents=True)
    _limits_path(dirs).write_text("[Service]\nLimitNOFILE=1024\n")
    _coredump_path(dirs).parent.mkdir(parents=True)
    _coredump_path(dirs).write_text(files[1].content)
    rec = _Recorder()

    assert ensure_conf_files(files, rec.run, rec.reload) is True

    assert _limits_path(dirs).read_text() == files[0].content
    assert rec.reloads == 1
    assert all(str(_coredump_path(dirs)) not in cmd for cmd in rec.commands)


def test_drifted_coredump_drop_in_is_rewritten_without_reload(dirs) -> None:
    files = _files(dirs)
    _limits_path(dirs).parent.mkdir(parents=True)
    _limits_path(dirs).write_text(files[0].content)
    _coredump_path(dirs).parent.mkdir(parents=True)
    _coredump_path(dirs).write_text("[Coredump]\nProcessSizeMax=1G\n")
    rec = _Recorder()

    assert ensure_conf_files(files, rec.run, rec.reload) is True

    assert _coredump_path(dirs).read_text() == files[1].content
    assert rec.reloads == 0


def test_write_failure_is_logged_and_other_file_still_written(
    dirs, caplog: pytest.LogCaptureFixture
) -> None:
    unit_dir, coredump_dir = dirs
    coredump_dir.write_text("not a directory")  # mkdir -p of the drop-in dir fails
    rec = _Recorder()

    with caplog.at_level("WARNING"):
        ok = ensure_conf_files(_files(dirs), rec.run, rec.reload)

    assert ok is False
    assert "DEPLOY-GENERAL-230" in caplog.text
    assert str(_coredump_path(dirs)) in caplog.text
    assert _limits_path(dirs).read_text() == _files(dirs)[0].content
    assert rec.reloads == 1


def test_undecodable_installed_drop_in_is_rewritten(dirs: Dirs) -> None:
    files = _files(dirs)
    _limits_path(dirs).parent.mkdir(parents=True)
    _limits_path(dirs).write_bytes(b"[Service]\n\xff\xfe\n")
    rec = _Recorder()

    assert ensure_conf_files(files, rec.run, rec.reload) is True

    assert _limits_path(dirs).read_text() == files[0].content
    assert rec.reloads == 1


def test_reload_failure_reports_false(dirs) -> None:
    rec = _Recorder(reload_ok=False)

    assert ensure_conf_files(_files(dirs), rec.run, rec.reload) is False
    assert rec.reloads == 1
