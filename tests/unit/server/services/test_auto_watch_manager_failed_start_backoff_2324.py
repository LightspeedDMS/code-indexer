"""#2324: a watch whose start failed (here: no embedding API key) is retried
at most once per watch idle-timeout window, logging its ERROR once per
attempt, never once per MCP write."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, List

import pytest

from code_indexer.server.services.auto_watch_manager import AutoWatchManager
from code_indexer.services.local_project_init import initialize_local_project

WATCH_LOGGER = "code_indexer.daemon.watch_manager"
TIMEOUT_SECONDS = 300


def _wait_until(condition: Callable[[], bool], seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.05)
    return condition()


def _thread_errors(caplog: pytest.LogCaptureFixture) -> List[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == WATCH_LOGGER and "Watch thread error" in r.getMessage()
    ]


def _start_and_settle(manager: AutoWatchManager, repo: str) -> None:
    manager.start_watch(repo)
    entry = manager._watch_state.get(repo)
    if entry is not None:
        _wait_until(lambda: not entry["watch_instance"].is_running())


def test_failed_watch_start_is_retried_once_per_idle_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace = (tmp_path / "example-repo").resolve()
    workspace.mkdir()
    initialize_local_project(workspace, no_override_file=True)
    repo = str(workspace)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CO_API_KEY", raising=False)
    now = [1000.0]
    manager = AutoWatchManager(default_timeout=TIMEOUT_SECONDS)
    manager._clock = lambda: now[0]
    caplog.set_level(logging.INFO)
    try:
        _start_and_settle(manager, repo)
        assert len(_thread_errors(caplog)) == 1

        for _ in range(5):
            now[0] += 10
            _start_and_settle(manager, repo)
        assert len(_thread_errors(caplog)) == 1

        now[0] += TIMEOUT_SECONDS
        _start_and_settle(manager, repo)
        assert len(_thread_errors(caplog)) == 2

        monkeypatch.setenv("VOYAGE_API_KEY", "sentinel-test-key-2324")
        now[0] += TIMEOUT_SECONDS + 1
        result = manager.start_watch(repo)
        assert result["status"] == "success"
        instance = manager._watch_state[repo]["watch_instance"]
        assert _wait_until(
            lambda: type(instance.watch_handler).__name__ == "GitAwareWatchHandler"
        )
        assert manager.is_watching(repo)
        assert len(_thread_errors(caplog)) == 2
    finally:
        manager.shutdown()


def test_synchronous_start_failure_logs_once_per_window(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = str((tmp_path / "example-repo").resolve())
    Path(repo).mkdir()
    now = [1000.0]
    manager = AutoWatchManager(default_timeout=TIMEOUT_SECONDS)
    manager._clock = lambda: now[0]
    caplog.set_level(logging.INFO)

    def refusals() -> int:
        return len(
            [r for r in caplog.records if "Refusing to start watch" in r.getMessage()]
        )

    try:
        for _ in range(4):
            assert manager.start_watch(repo)["status"] == "error"
            now[0] += 10
        assert refusals() == 1

        now[0] += TIMEOUT_SECONDS
        assert manager.start_watch(repo)["status"] == "error"
        assert refusals() == 2
    finally:
        manager.shutdown()
