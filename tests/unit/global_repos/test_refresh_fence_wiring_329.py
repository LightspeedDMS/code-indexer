"""A running refresh whose job or alias lease now belongs to another run is
fenced: its real indexing child is stopped, nothing is published, and no
self-heal, strike or owed retry is recorded."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from code_indexer.global_repos import refresh_fence
from code_indexer.global_repos.refresh_fence import (
    JOB_OWNERSHIP_LOST,
    LEASE_LOST,
    RefreshOwnershipLostError,
)
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)

from .test_refresh_scheduler_cancel_2012 import (
    ALIAS,
    CANCEL_BOUND_SECONDS,
    _is_running,
    _wait_for,
    _wait_until_gone,
    alias_target,
    install_fake_cidx,
    make_local_repo_scheduler,
)
from . import test_refresh_scheduler_cancel_2012 as _cancel_2012

# ^ external pytest registers a fixture under the module attribute name; an alias import would rename it
pid_file = _cancel_2012.pid_file

REPO = "example-repo"
FAST_PROBE_SECONDS = 0.5
CHILD_START_BOUND_SECONDS = 30
GROUP_GONE_BOUND_SECONDS = 5.0
JOIN_GRACE_SECONDS = 1
ALIVE_OBSERVATION_SECONDS = 6.0
SIBLING_LEASE_SECONDS = 3600


class RefreshThread(threading.Thread):
    def __init__(self, target: Any) -> None:
        super().__init__(daemon=True)
        self._fn = target
        self.error: BaseException | None = None
        self.result: Dict[str, Any] | None = None

    def run(self) -> None:
        try:
            self.result = self._fn()
        except BaseException as exc:  # noqa: BLE001 - recorded for the assert
            self.error = exc


def _child_started(pids: Path) -> bool:
    return pids.exists() and pids.stat().st_size > 0


def _assert_nothing_owed_or_published(
    scheduler: Any, recorder: Any, tmp_path: Path, target: str
) -> None:
    assert recorder.calls == [], "a fenced refresh must never create a snapshot"
    assert alias_target(tmp_path) == target, "a fenced refresh must never swap"
    state = scheduler.golden_repo_metadata.get_refresh_failure_backoff_state(ALIAS)
    assert state is None, f"a fenced run records no strike/backoff/owed retry: {state}"


def test_lease_takeover_fences_running_index_child(tmp_path, monkeypatch, pid_file):
    monkeypatch.setattr(
        refresh_fence, "LEASE_PROBE_INTERVAL_SECONDS", FAST_PROBE_SECONDS
    )
    install_fake_cidx(tmp_path, monkeypatch, "--fts", pid_file)
    lock_dir = tmp_path / "alias-locks"
    store = SqliteAliasLockStore(lock_dir)
    scheduler, recorder = make_local_repo_scheduler(
        tmp_path,
        alias_lock_db_backed_enabled_getter=lambda: True,
        alias_lock_store_resolver=lambda: store,
    )
    target = alias_target(tmp_path)
    run = RefreshThread(
        lambda: scheduler._execute_refresh(ALIAS, cancel_check=lambda: False)
    )
    run.start()
    try:
        assert _wait_for(lambda: _child_started(pid_file), CHILD_START_BOUND_SECONDS)
        # ^ why delete + re-acquire, not release_owned; a stale-lease takeover never has the holder's token
        lock_db = next(lock_dir.glob("*.db"))
        with sqlite3.connect(lock_db) as conn:
            conn.execute("DELETE FROM golden_repo_alias_locks")
        assert store.try_acquire(
            REPO,
            "refresh_scheduler",
            owner_token="sibling",
            ttl_seconds=SIBLING_LEASE_SECONDS,
        )

        pids = json.loads(pid_file.read_text())
        assert _wait_until_gone(pids["child_pid"], CANCEL_BOUND_SECONDS), (
            "indexing child kept writing after its lease was taken over"
        )
        run.join(CANCEL_BOUND_SECONDS)
        assert isinstance(run.error, RefreshOwnershipLostError), repr(run.error)
        assert run.error.reason == LEASE_LOST
        _assert_nothing_owed_or_published(scheduler, recorder, tmp_path, target)
        holder = store.get_holder(REPO)
        assert holder is not None, "the fenced run must not release the sibling's lease"
    finally:
        run.join(JOIN_GRACE_SECONDS)


def test_claimed_job_ownership_loss_fences_running_index_child(
    tmp_path, monkeypatch, pid_file
):
    install_fake_cidx(tmp_path, monkeypatch, "--fts", pid_file)
    scheduler, recorder = make_local_repo_scheduler(tmp_path)
    target = alias_target(tmp_path)

    def still_owns() -> bool:
        return not _child_started(pid_file)

    run = RefreshThread(
        lambda: scheduler.execute_refresh_for_claimed_job(
            ALIAS, job_id="job-1", job_owned=still_owns
        )
    )
    run.start()
    try:
        assert _wait_for(lambda: _child_started(pid_file), CHILD_START_BOUND_SECONDS)
        pids = json.loads(pid_file.read_text())
        assert _wait_until_gone(pids["child_pid"], CANCEL_BOUND_SECONDS)
        assert _wait_until_gone(pids["grandchild_pid"], GROUP_GONE_BOUND_SECONDS)
        run.join(CANCEL_BOUND_SECONDS)
        assert isinstance(run.error, RefreshOwnershipLostError), repr(run.error)
        assert run.error.reason == JOB_OWNERSHIP_LOST
        _assert_nothing_owed_or_published(scheduler, recorder, tmp_path, target)
    finally:
        run.join(JOIN_GRACE_SECONDS)


def test_unreadable_job_ownership_never_stops_the_child(
    tmp_path, monkeypatch, pid_file
):
    install_fake_cidx(tmp_path, monkeypatch, "--fts", pid_file)
    scheduler, _ = make_local_repo_scheduler(tmp_path)
    reads: List[int] = []

    def unreadable() -> bool:
        reads.append(1)
        raise OSError("job store unreachable")

    run = RefreshThread(
        lambda: scheduler.execute_refresh_for_claimed_job(
            ALIAS, job_id="job-1", job_owned=unreadable
        )
    )
    run.start()
    try:
        assert _wait_for(lambda: _child_started(pid_file), CHILD_START_BOUND_SECONDS)
        pids = json.loads(pid_file.read_text())
        time.sleep(ALIVE_OBSERVATION_SECONDS)
        assert _is_running(pids["child_pid"]), "a failed ownership read stopped the run"
        assert len(reads) >= 2, "the ownership check must keep being consulted"
    finally:
        # ^ order SIGKILL before join; the refresh thread ends only once its child dies
        for pid in json.loads(pid_file.read_text()).values():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        run.join(CANCEL_BOUND_SECONDS)
