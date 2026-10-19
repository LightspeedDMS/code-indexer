"""An X-Ray evaluator compile never outlives the request that started it.

Real processes only: the real xray-cli release binary runs with a stand-in
``rustc`` first on PATH. The stand-in answers ``--version`` like the real
compiler and otherwise starts a child (standing in for the linker) and
sleeps. Every process of the job inherits a unique environment marker, so
"nothing from the job remains" is checked by scanning /proc for it.

Two ways a compile is stopped from the server side:
- the server's own xray-cli timeout (``_run_xray_cli_process``);
- a job cancel (``BackgroundJobManager._terminate_child_processes``).
In both, when the X-Ray call returns, no process of the job is alive and
the compile slot it held is free again.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple
from unittest.mock import patch

import pytest

from code_indexer.xray import rust_backend
from code_indexer.xray.rust_backend import RustNativeBackend, _XRAY_CLI_DEFAULT

_MARKER_ENV = "CIDX_TEST_XRAY_JOB_MARKER"
_WAIT_FOR_RUSTC_SECONDS = 30.0
_POLL_SECONDS = 0.02

_FAKE_RUSTC = """#!/bin/bash
if [ "$1" = "--version" ] || [ "$1" = "-vV" ]; then
  echo "rustc 1.98.0 (stand-in 2026-01-01)"
  echo "release: 1.98.0"
  exit 0
fi
sleep 300 &
echo "$$ $!" > "$FAKE_RUSTC_LOG.tmp"
mv "$FAKE_RUSTC_LOG.tmp" "$FAKE_RUSTC_LOG"
wait
"""


def _alive(pid: int) -> bool:
    """Present in /proc and not a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def _alive_with_marker(marker: str) -> List[int]:
    needle = f"{_MARKER_ENV}={marker}".encode()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle in environ.split(b"\0") and _alive(int(entry.name)):
            found.append(int(entry.name))
    return found


def _kill_quietly(pids: List[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


class _Job:
    """One X-Ray compile job: evaluator, candidate list, stand-in compiler."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        if not _XRAY_CLI_DEFAULT.exists():
            pytest.fail(f"xray-cli release binary missing at {_XRAY_CLI_DEFAULT}")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        rustc = bin_dir / "rustc"
        rustc.write_text(_FAKE_RUSTC)
        rustc.chmod(0o755)
        sample = root / "sample.py"
        sample.write_text("x = 1\n")
        self.files = root / "files.txt"
        self.files.write_text(f"{sample}\n")
        self.marker = uuid.uuid4().hex
        self.evaluator = root / "ev.rs"
        self.evaluator.write_text(
            "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> "
            f'{{ let _ = "{self.marker}"; Vec::new() }}'
        )
        self.log = root / "rustc.pids"
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
        monkeypatch.setenv("CIDX_DATA_DIR", str(root / "data"))
        monkeypatch.setenv("FAKE_RUSTC_LOG", str(self.log))
        monkeypatch.setenv(_MARKER_ENV, self.marker)

    @property
    def cmd(self) -> List[str]:
        return [
            str(_XRAY_CLI_DEFAULT),
            "--dynlib",
            str(self.evaluator),
            "--files-from",
            str(self.files),
            "--json",
        ]

    def wait_for_rustc(self) -> Tuple[int, int]:
        deadline = time.monotonic() + _WAIT_FOR_RUSTC_SECONDS
        while time.monotonic() < deadline:
            if self.log.exists():
                rustc, linker = (int(p) for p in self.log.read_text().split())
                return rustc, linker
            time.sleep(_POLL_SECONDS)
        pytest.fail("stand-in rustc never started")


def _free_compile_slots() -> int:
    return rust_backend._compile_semaphore._value  # type: ignore[attr-defined]


def test_server_timeout_leaves_no_process_of_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _Job(tmp_path, monkeypatch)
    slots_before = _free_compile_slots()
    try:
        _, err = RustNativeBackend()._run_xray_cli_process(
            job.cmd, 3, None, acquire_compile_slot=True
        )
        survivors = _alive_with_marker(job.marker)
    finally:
        _kill_quietly(_alive_with_marker(job.marker))
    assert err is not None and "timed out" in err
    assert job.log.exists(), "the stand-in compiler never ran"
    assert survivors == [], f"processes of the job outlived the timeout: {survivors}"
    assert _free_compile_slots() == slots_before


def _manager(background_job_manager_factory):
    with patch(
        "code_indexer.server.services.maintenance_service.get_maintenance_state"
    ) as maintenance:
        maintenance.return_value.is_maintenance_mode.return_value = False
        return background_job_manager_factory()


def test_job_cancel_kills_every_member_of_the_child_group(
    tmp_path: Path, background_job_manager_factory
) -> None:
    """A registered child's group member that ignores SIGTERM dies too."""
    manager = _manager(background_job_manager_factory)
    pid_file = tmp_path / "member.pid"
    proc = subprocess.Popen(
        [
            "bash",
            "-c",
            f'trap "" TERM; sleep 300 & echo $! > {pid_file}; wait',
        ],
        start_new_session=True,
    )
    deadline = time.monotonic() + _WAIT_FOR_RUSTC_SECONDS
    while not pid_file.exists() or not pid_file.read_text().strip():
        assert time.monotonic() < deadline, "group member never started"
        time.sleep(_POLL_SECONDS)
    member = int(pid_file.read_text())
    manager.register_child_process("group-cancel", proc)
    try:
        manager._terminate_child_processes("group-cancel")
        member_alive = _alive(member)
    finally:
        _kill_quietly([member])
        proc.kill()
        proc.wait()
    assert not member_alive, f"group member {member} outlived the job cancel"


def test_job_cancel_leaves_no_process_of_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, background_job_manager_factory
) -> None:
    job = _Job(tmp_path, monkeypatch)
    with patch(
        "code_indexer.server.services.maintenance_service.get_maintenance_state"
    ) as maintenance:
        maintenance.return_value.is_maintenance_mode.return_value = False
        manager = background_job_manager_factory()
    job_id = "xray-cancel-" + job.marker
    result: List[Optional[str]] = []
    slots_before = _free_compile_slots()

    def run() -> None:
        _, err = RustNativeBackend()._run_xray_cli_process(
            job.cmd,
            120,
            lambda proc: manager.register_child_process(job_id, proc),
            acquire_compile_slot=True,
        )
        result.append(err)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        job.wait_for_rustc()
        manager._terminate_child_processes(job_id)
        worker.join(timeout=30)
        returned = not worker.is_alive()
        survivors = _alive_with_marker(job.marker)
    finally:
        _kill_quietly(_alive_with_marker(job.marker))
    assert returned, "the X-Ray call did not return after the cancel"
    assert result and result[0] is not None
    assert survivors == [], f"processes of the job outlived the cancel: {survivors}"
    assert _free_compile_slots() == slots_before


def test_a_failed_termination_is_recorded_in_the_job(
    background_job_manager_factory, caplog
) -> None:
    from datetime import datetime, timezone

    from code_indexer.server.repositories.background_jobs import (
        BackgroundJob,
        JobStatus,
    )

    manager = _manager(background_job_manager_factory)
    job_id = "xray-unsignalled-" + uuid.uuid4().hex
    with manager._lock:
        manager.jobs[job_id] = BackgroundJob(
            job_id=job_id,
            operation_type="xray_search",
            status=JobStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username="example-user",
        )
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    manager.register_child_process(job_id, proc)
    try:
        with patch("os.killpg", side_effect=PermissionError("not ours")):
            manager._terminate_child_processes(job_id)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    error = manager.jobs[job_id].error or ""
    assert "could not be terminated" in error
    assert any(r.levelname == "ERROR" for r in caplog.records)
