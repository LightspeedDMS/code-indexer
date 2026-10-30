"""#2323: AutoWatchManager's view of a watch that stopped itself (its
workspace was removed), the removal stop deactivation uses, and (#2324) the
writes of a failed watch start reaching the next successful one."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterator, List

import pytest

from code_indexer.daemon.watch_manager import WATCH_STARTING, DaemonWatchManager
from code_indexer.server.services.auto_watch_manager import AutoWatchManager

MANAGER_LOGGER = "code_indexer.server.services.auto_watch_manager"
DAEMON_LOGGER = "code_indexer.daemon.watch_manager"
REPO = "/example/activated-repos/example-user/example-repo"
TIMEOUT_SECONDS = 300


def _track_self_stopped_watch(manager: AutoWatchManager, repo_path: str) -> None:
    manager._watch_state[repo_path] = {
        # ^ invariant True on purpose; a watch that stops itself never clears AutoWatchManager's flag
        "watch_running": True,
        "started_at": 0.0,
        "last_activity": datetime.now(),
        "timeout_seconds": TIMEOUT_SECONDS,
        "watch_instance": DaemonWatchManager(),
    }


def _warnings(caplog: pytest.LogCaptureFixture) -> List[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name in (MANAGER_LOGGER, DAEMON_LOGGER) and r.levelno >= logging.WARNING
    ]


@pytest.fixture
def live_instance() -> Iterator[DaemonWatchManager]:
    # ^ why a real instance in its starting state, not a running watch; a started watch would embed through a real provider
    instance = DaemonWatchManager()
    release = threading.Event()
    instance.watch_thread = threading.Thread(target=release.wait, daemon=True)
    instance.watch_thread.start()
    instance.watch_handler = WATCH_STARTING
    try:
        yield instance
    finally:
        release.set()
        instance.watch_thread.join(timeout=5)


def test_watch_whose_instance_stopped_is_not_watching() -> None:
    manager = AutoWatchManager()
    _track_self_stopped_watch(manager, REPO)

    assert manager.is_watching(REPO) is False


def test_stop_watch_for_removal_is_quiet_without_a_watch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = AutoWatchManager()
    caplog.set_level(logging.INFO)

    manager.stop_watch_for_removal(REPO)
    manager.finish_removal(REPO)

    assert _warnings(caplog) == []


def test_removing_a_self_stopped_watch_logs_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = AutoWatchManager()
    _track_self_stopped_watch(manager, REPO)
    caplog.set_level(logging.INFO)

    manager.stop_watch_for_removal(REPO)
    manager.finish_removal(REPO)

    assert manager.get_state(REPO) is None
    assert _warnings(caplog) == []


def test_starts_are_refused_while_the_workspace_is_removed(tmp_path: Path) -> None:
    repo = str(tmp_path / "example-repo")
    manager = AutoWatchManager(default_timeout=TIMEOUT_SECONDS)

    manager.stop_watch_for_removal(repo)
    refused = manager.start_watch(repo)
    manager.finish_removal(repo)
    allowed = manager.start_watch(repo)

    assert refused["status"] == "error"
    assert "deactivat" in refused["message"]
    assert manager.get_state(repo) is None
    assert "deactivat" not in allowed["message"]


def test_writes_of_failed_starts_reach_the_next_successful_start(
    tmp_path: Path, live_instance: DaemonWatchManager
) -> None:
    repo = str(tmp_path / "example-repo")
    other = str(tmp_path / "other-repo")
    now = [1000.0]
    manager = AutoWatchManager(default_timeout=TIMEOUT_SECONDS)
    manager._clock = lambda: now[0]
    first, second, third = (Path(repo) / f"src/f{i}.py" for i in range(3))

    assert manager.start_watch(other)["status"] == "error"
    assert manager.start_watch(repo, changed_paths=[first])["status"] == "error"
    now[0] += 10
    assert manager.start_watch(repo, changed_paths=[second])["status"] == "error"
    now[0] += TIMEOUT_SECONDS
    manager._watch_state[repo] = {
        "watch_running": True,
        "started_at": now[0],
        "last_activity": datetime.now(),
        "timeout_seconds": TIMEOUT_SECONDS,
        "watch_instance": live_instance,
    }

    assert manager.start_watch(repo, changed_paths=[third])["status"] == "success"

    assert sorted(live_instance._early_changes) == [first, second, third]
    assert other not in manager._failed_start_at
