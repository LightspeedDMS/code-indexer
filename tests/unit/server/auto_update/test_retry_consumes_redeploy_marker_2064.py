"""The startup-retry deploy consumes the pending-redeploy marker (public Bug #2064).

After an auto-updater self-restart the status file says ``pending_restart``
AND the ``pending-redeploy`` marker exists.  The next run takes run_once's
retry branch, deploys and restarts cidx-server.  It must consume the marker
once that retry deploy AND the cidx-server restart succeeded, or the
following ``poll_once`` sees the marker and forces a SECOND deploy + restart.
A failed deploy, or a failed or interrupted restart, keeps the marker so the
next run retries.  A marker created anew DURING the retry deploy (a fresh
redeploy request) is not consumed.

The marker is a real file in a temp directory and the follow-up poll runs the
real AutoUpdateService.poll_once(); only the deploy executor (pip, systemctl)
is a recording test double.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, List, Optional, Union

import pytest

from code_indexer.server.auto_update import run_once
from code_indexer.server.auto_update import service as service_module
from code_indexer.server.auto_update.service import AutoUpdateService


class _RecordingExecutor:
    """Stands in for DeploymentExecutor: records deploys, restarts, statuses.

    *restart_result* is what restart_server() returns, or an exception it
    raises (a process killed during the drain wait never returns).
    """

    def __init__(
        self,
        marker: Path,
        deploy_ok: bool,
        during_execute: Optional[Callable[[], None]] = None,
        restart_result: Union[bool, BaseException] = True,
        after_restart: Optional[Callable[[], None]] = None,
    ) -> None:
        self.marker = marker
        self.deploy_ok = deploy_ok
        self.during_execute = during_execute
        self.restart_result = restart_result
        self.after_restart = after_restart
        self.events: List[str] = []
        self.server_url: Optional[str] = None

    def _should_retry_on_startup(self) -> bool:
        return True

    def _write_status_file(self, status: str, details: str = "") -> None:
        self.events.append(f"status:{status}")

    def execute(self) -> bool:
        self.events.append("execute")
        if self.during_execute is not None:
            self.during_execute()
        return self.deploy_ok

    def restart_server(self) -> bool:
        self.events.append(f"restart(marker_exists={self.marker.exists()})")
        if self.after_restart is not None:
            self.after_restart()
        if isinstance(self.restart_result, BaseException):
            raise self.restart_result
        return self.restart_result


class _NoChanges:
    def has_changes(self) -> bool:
        return False


class _UnusedLock:
    def acquire(self) -> bool:
        raise AssertionError("no deploy expected, so no lock")


@pytest.fixture()
def marker(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "pending-redeploy"
    path.touch()
    monkeypatch.setattr(run_once, "PENDING_REDEPLOY_MARKER", path, raising=False)
    monkeypatch.setattr(service_module, "PENDING_REDEPLOY_MARKER", path)
    monkeypatch.setattr(
        service_module, "LEGACY_REDEPLOY_MARKER", tmp_path / "legacy-redeploy"
    )
    monkeypatch.setattr(
        service_module, "RESTART_SIGNAL_PATH", tmp_path / "restart.signal"
    )
    return path


def _run_retry(monkeypatch, executor: _RecordingExecutor) -> int:
    monkeypatch.setattr(run_once, "DeploymentExecutor", lambda **_kw: executor)
    monkeypatch.setattr(
        run_once, "_resolve_server_url", lambda _e: "http://127.0.0.1:8000"
    )
    with pytest.raises(SystemExit) as exit_info:
        run_once.main()
    code = exit_info.value.code
    assert isinstance(code, int), code
    return code


def _next_poll(executor: _RecordingExecutor, tmp_path: Path) -> None:
    service = AutoUpdateService(
        repo_path=tmp_path, check_interval=60, lock_file=tmp_path / "lock"
    )
    service.change_detector = _NoChanges()  # type: ignore[assignment]
    service.deployment_lock = _UnusedLock()  # type: ignore[assignment]
    service.deployment_executor = executor  # type: ignore[assignment]
    service.poll_once()


def _restarts(executor: _RecordingExecutor) -> int:
    return sum(e.startswith("restart") for e in executor.events)


def test_successful_retry_consumes_marker_and_next_poll_does_not_redeploy(
    marker: Path, monkeypatch, tmp_path: Path
) -> None:
    executor = _RecordingExecutor(marker, deploy_ok=True)

    assert _run_retry(monkeypatch, executor) == 0
    assert not marker.exists()
    assert executor.events.count("execute") == 1
    # Consumed only once the restart has succeeded.
    assert "restart(marker_exists=True)" in executor.events
    assert executor.events[-1] == "status:success"

    _next_poll(executor, tmp_path)
    assert executor.events.count("execute") == 1
    assert _restarts(executor) == 1


def test_failed_restart_keeps_marker_and_records_failure(
    marker: Path, monkeypatch
) -> None:
    executor = _RecordingExecutor(marker, deploy_ok=True, restart_result=False)

    assert _run_retry(monkeypatch, executor) == 1
    assert marker.exists()  # the next run re-drives the restart
    assert executor.events[-1] == "status:failed"
    assert "status:success" not in executor.events


def test_interrupted_restart_keeps_marker(marker: Path, monkeypatch) -> None:
    executor = _RecordingExecutor(
        marker, deploy_ok=True, restart_result=RuntimeError("drain interrupted")
    )

    assert _run_retry(monkeypatch, executor) == 1
    assert marker.exists()


# type(Path()) is the runtime-selected concrete class (PosixPath here), which
# mypy cannot accept as a base class expression.
class _UnreadableAfterRestart(type(Path())):  # type: ignore[misc]
    """A marker path whose stat() fails once ``restarted`` is set."""

    restarted = False

    def stat(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.restarted:
            raise PermissionError("marker unreadable")
        return super().stat(*args, **kwargs)


def test_marker_check_error_does_not_fail_a_completed_restart(
    tmp_path: Path, monkeypatch
) -> None:
    unreadable = _UnreadableAfterRestart(str(tmp_path / "pending-redeploy"))
    plain = Path(str(unreadable))  # a plain Path: no stat() override
    plain.touch()
    monkeypatch.setattr(run_once, "PENDING_REDEPLOY_MARKER", unreadable)

    def _mark_restarted() -> None:
        unreadable.restarted = True

    executor = _RecordingExecutor(plain, deploy_ok=True, after_restart=_mark_restarted)

    assert _run_retry(monkeypatch, executor) == 0
    assert _restarts(executor) == 1
    assert executor.events[-1] == "status:success"
    assert plain.exists()  # could not be checked: kept, logged


def test_failed_retry_keeps_marker(marker: Path, monkeypatch) -> None:
    executor = _RecordingExecutor(marker, deploy_ok=False)

    assert _run_retry(monkeypatch, executor) == 1
    assert marker.exists()
    assert _restarts(executor) == 0


def test_marker_recreated_during_retry_deploy_is_kept(
    marker: Path, monkeypatch
) -> None:
    """A redeploy requested during the retry deploy itself must survive."""
    before = marker.stat().st_mtime_ns

    def _request_redeploy() -> None:
        marker.unlink()
        marker.touch()
        os.utime(marker, ns=(before + 10**9, before + 10**9))

    executor = _RecordingExecutor(
        marker, deploy_ok=True, during_execute=_request_redeploy
    )

    assert _run_retry(monkeypatch, executor) == 0
    assert marker.exists()
