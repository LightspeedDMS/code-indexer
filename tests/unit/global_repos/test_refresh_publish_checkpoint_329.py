"""The last checks before a refresh publishes: an exact-token lease check
and a job-ownership check run unthrottled right before the alias swap, and
no ownership checkpoint ever shortens the refresh's 24 h lease."""

from __future__ import annotations

import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

import pytest

from code_indexer.global_repos.refresh_fence import (
    JOB_OWNERSHIP_LOST,
    LEASE_LOST,
    RefreshOwnershipLostError,
)
from code_indexer.global_repos.refresh_scheduler import (
    _REFRESH_PUBLISH_LOCK_TTL_SECONDS,
    REFRESH_LOCK_OWNER,
)
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)

from . import test_refresh_scheduler_cancel_2012 as _cancel_2012
from .test_refresh_scheduler_cancel_2012 import (
    ALIAS,
    RecordingSnapshotManager,
    alias_target,
    install_fake_cidx,
    make_local_repo_scheduler,
)

# ^ external pytest registers a fixture under the module attribute name; an alias import would rename it
pid_file = _cancel_2012.pid_file

REPO = "example-repo"
# ^ invariant != _REFRESH_PUBLISH_LOCK_TTL_SECONDS; the surviving holder is identified by it (holders never expose tokens)
SIBLING_LEASE_SECONDS = 3600
BACKENDS = ("file", "sqlite_db")


class SnapshotWithSideEffect(RecordingSnapshotManager):
    """Snapshot-manager double: really creates the versioned directory and
    runs ``during`` while doing so (the window between the pre-publish
    checkpoint and the alias swap)."""

    def __init__(self, golden: Path, during: Callable[[], None]) -> None:
        super().__init__()
        self._golden = golden
        self._during = during
        self.created: List[str] = []

    def create_snapshot(self, repo_name: str, source_path: str) -> str:
        self.calls.append(repo_name)
        snapshot = self._golden / ".versioned" / repo_name / f"v_{time.time_ns()}"
        shutil.copytree(source_path, snapshot)
        (snapshot / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)
        self._during()
        self.created.append(str(snapshot))
        return str(snapshot)


def _scheduler(
    tmp_path: Path, backend: str
) -> Tuple[Any, Optional[SqliteAliasLockStore]]:
    if backend == "file":
        scheduler, _ = make_local_repo_scheduler(tmp_path)
        return scheduler, None
    store = SqliteAliasLockStore(tmp_path / "alias-locks")
    scheduler, _ = make_local_repo_scheduler(
        tmp_path,
        alias_lock_db_backed_enabled_getter=lambda: True,
        alias_lock_store_resolver=lambda: store,
    )
    return scheduler, store


def _held_ttl(scheduler: Any) -> Any:
    info = scheduler.write_lock_manager.get_lock_info(REPO)
    assert info is not None, "the refresh lease must be held"
    return info["ttl_seconds"]


def _take_over_lease(store: SqliteAliasLockStore, lock_dir: Path) -> None:
    with closing(sqlite3.connect(next(lock_dir.glob("*.db")))) as conn, conn:
        conn.execute("DELETE FROM golden_repo_alias_locks")
    assert store.try_acquire(
        REPO,
        REFRESH_LOCK_OWNER,
        owner_token="sibling",
        ttl_seconds=SIBLING_LEASE_SECONDS,
    )


def _install_snapshots(
    scheduler: Any, tmp_path: Path, during: Callable[[], None]
) -> SnapshotWithSideEffect:
    snapshots = SnapshotWithSideEffect(tmp_path / "golden_repos", during)
    scheduler._snapshot_manager = cast(Any, snapshots)
    return snapshots


def test_lease_taken_over_during_snapshot_never_swaps(tmp_path, monkeypatch, pid_file):
    install_fake_cidx(tmp_path, monkeypatch, "never-matches", pid_file)
    scheduler, store = _scheduler(tmp_path, "sqlite_db")
    assert store is not None
    snapshots = _install_snapshots(
        scheduler, tmp_path, lambda: _take_over_lease(store, tmp_path / "alias-locks")
    )
    target = alias_target(tmp_path)

    with pytest.raises(RefreshOwnershipLostError) as lost:
        scheduler._execute_refresh(ALIAS, cancel_check=lambda: False)

    assert lost.value.reason == LEASE_LOST
    assert len(snapshots.created) == 1, "the takeover must land mid-snapshot"
    assert alias_target(tmp_path) == target, "alias swapped after the lease was lost"
    assert snapshots.created[0] in scheduler.cleanup_manager.get_pending_cleanups()
    holder = store.get_holder(REPO)
    assert holder is not None and holder["ttl_seconds"] == SIBLING_LEASE_SECONDS, (
        f"the sibling's lease must survive the fenced run: {holder}"
    )


def test_job_ownership_lost_during_snapshot_never_swaps(
    tmp_path, monkeypatch, pid_file
):
    install_fake_cidx(tmp_path, monkeypatch, "never-matches", pid_file)
    scheduler, _ = _scheduler(tmp_path, "file")
    state: Dict[str, bool] = {"owned": True}

    def reclaimed() -> None:
        state["owned"] = False

    snapshots = _install_snapshots(scheduler, tmp_path, reclaimed)
    target = alias_target(tmp_path)

    with pytest.raises(RefreshOwnershipLostError) as lost:
        scheduler.execute_refresh_for_claimed_job(
            ALIAS, job_id="job-1", job_owned=lambda: state["owned"]
        )

    assert lost.value.reason == JOB_OWNERSHIP_LOST
    assert len(snapshots.created) == 1
    assert alias_target(tmp_path) == target
    assert snapshots.created[0] in scheduler.cleanup_manager.get_pending_cleanups()


@pytest.mark.parametrize("backend", BACKENDS)
def test_pre_publish_checkpoint_keeps_the_refresh_lease(
    tmp_path, monkeypatch, pid_file, backend
):
    install_fake_cidx(tmp_path, monkeypatch, "never-matches", pid_file)
    scheduler, _ = _scheduler(tmp_path, backend)
    seen: List[Any] = []
    _install_snapshots(scheduler, tmp_path, lambda: seen.append(_held_ttl(scheduler)))

    scheduler._execute_refresh(ALIAS, cancel_check=lambda: False)

    assert seen == [_REFRESH_PUBLISH_LOCK_TTL_SECONDS], (
        "the checkpoint before the snapshot must not shorten the 24 h lease"
    )


def test_ownership_error_after_the_swap_is_not_reported_as_unpublished(
    tmp_path, monkeypatch, pid_file
):
    from code_indexer.server.services.alias_lock_store.base import (
        AliasLockOwnershipLostError,
    )

    install_fake_cidx(tmp_path, monkeypatch, "never-matches", pid_file)
    scheduler, _ = _scheduler(tmp_path, "file")
    snapshots = _install_snapshots(scheduler, tmp_path, lambda: None)

    def lost_after_publish(alias_name: str) -> None:
        raise AliasLockOwnershipLostError("taken over after the swap")

    monkeypatch.setattr(
        scheduler.registry, "update_refresh_timestamp", lost_after_publish
    )

    with pytest.raises(RuntimeError) as failed:
        scheduler._execute_refresh(ALIAS, cancel_check=lambda: False)

    assert not isinstance(failed.value, RefreshOwnershipLostError), (
        "the alias already swapped: never report 'nothing was published'"
    )
    assert alias_target(tmp_path) == snapshots.created[0]


@pytest.mark.parametrize("backend", BACKENDS)
def test_ownership_check_keeps_the_held_lease(tmp_path, backend):
    scheduler, _ = _scheduler(tmp_path, backend)
    assert scheduler.write_lock_manager.acquire(
        REPO,
        owner_name=REFRESH_LOCK_OWNER,
        ttl_seconds=_REFRESH_PUBLISH_LOCK_TTL_SECONDS,
    )
    scheduler._ownership_check(REPO)()
    assert _held_ttl(scheduler) == _REFRESH_PUBLISH_LOCK_TTL_SECONDS
    assert scheduler.write_lock_manager.release(REPO, owner_name=REFRESH_LOCK_OWNER)
