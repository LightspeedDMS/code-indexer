"""Bug #2012 Part 1: cancelling a golden-repo refresh must stop the indexing
subprocess it launched -- semantic/FTS, temporal and SCIP alike.

A fake ``cidx`` executable is put first on PATH. It is a REAL child
process: for the step under test it records its own pid and the pid of a
grandchild it spawns into the same process group, then sleeps far longer
than any bound asserted here; for every other step it exits 0 at once.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Tuple, cast
from unittest.mock import Mock

import psutil
import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.services.progress_subprocess_runner import IndexingCancelledError

CHILD_SLEEP_SECONDS = 120
CANCEL_BOUND_SECONDS = 10.0

_FAKE_CIDX = """\
import json, os, subprocess, sys, time
argv = " ".join(sys.argv[1:])
touch_on = os.environ.get("FAKE_CIDX_TOUCH_ON")
if touch_on and touch_on in argv:
    open(os.environ["FAKE_CIDX_TOUCH_FILE"], "w").close()
if os.environ["FAKE_CIDX_SLEEP_ON"] not in argv:
    sys.exit(0)
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep({sleep})"])
with open(os.environ["FAKE_CIDX_PID_FILE"], "w") as fh:
    json.dump({{"child_pid": os.getpid(), "grandchild_pid": g.pid}}, fh)
time.sleep({sleep})
"""


def _is_running(pid: int) -> bool:
    try:
        return bool(psutil.Process(pid).status() != psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return False


def _wait_until_gone(pid: int, bound_seconds: float) -> bool:
    deadline = time.monotonic() + bound_seconds
    while time.monotonic() < deadline:
        if not _is_running(pid):
            return True
        time.sleep(0.05)
    return not _is_running(pid)


@pytest.fixture
def pid_file(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "child_pids.json"
    yield path
    # Teardown: never leave a sleeper behind, even when a test fails.
    if path.exists() and path.stat().st_size > 0:
        for pid in json.loads(path.read_text()).values():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def install_fake_cidx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleep_on: str, pid_file: Path
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "fake_cidx.py"
    script.write_text(_FAKE_CIDX.format(sleep=CHILD_SLEEP_SECONDS))
    shim = bin_dir / "cidx"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CIDX_SLEEP_ON", sleep_on)
    monkeypatch.setenv("FAKE_CIDX_PID_FILE", str(pid_file))


def make_scheduler(tmp_path: Path, repo_info: Dict) -> RefreshScheduler:
    golden = tmp_path / "golden-repos"
    golden.mkdir(exist_ok=True)
    registry = Mock()
    registry.get_global_repo.return_value = repo_info
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    metadata = Mock()
    metadata.get_repo.return_value = None
    return RefreshScheduler(
        golden_repos_dir=str(golden),
        config_source=config,
        query_tracker=Mock(spec=QueryTracker),
        cleanup_manager=Mock(spec=CleanupManager),
        registry=registry,
        golden_repo_metadata_backend=metadata,
    )


def cancel_once_child_started(pid_file: Path) -> Callable[[], bool]:
    def cancel_check() -> bool:
        return pid_file.exists() and pid_file.stat().st_size > 0

    return cancel_check


def assert_group_stopped(pid_file: Path, elapsed: float) -> None:
    pids = json.loads(pid_file.read_text())
    assert elapsed < CANCEL_BOUND_SECONDS, (
        f"cancel took {elapsed:.1f}s; the indexing child must be stopped "
        f"within {CANCEL_BOUND_SECONDS}s"
    )
    assert _wait_until_gone(pids["child_pid"], 5.0), "indexing child survived cancel"
    assert _wait_until_gone(pids["grandchild_pid"], 5.0), (
        "a process in the indexing child's group survived cancel"
    )


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    src = tmp_path / "source_repo"
    src.mkdir()
    (src / "main.py").write_text("def main():\n    pass\n")
    return src


def _run_index_source_expect_cancel(
    scheduler: RefreshScheduler, source_repo: Path, pid_file: Path
) -> float:
    from code_indexer.server.utils.cancellable_subprocess import (
        SubprocessCancelledError,
    )

    start = time.monotonic()
    # Indexing children raise IndexingCancelledError; SCIP (run_with_cancel)
    # raises SubprocessCancelledError -- both are refresh cancellations.
    with pytest.raises((IndexingCancelledError, SubprocessCancelledError)):
        scheduler._index_source(
            alias_name="example-repo-global",
            source_path=str(source_repo),
            cancel_check=cancel_once_child_started(pid_file),
        )
    return time.monotonic() - start


def test_semantic_index_child_is_stopped_on_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_file: Path, source_repo: Path
) -> None:
    install_fake_cidx(tmp_path, monkeypatch, "--fts", pid_file)
    scheduler = make_scheduler(
        tmp_path,
        {"repo_url": "local://example-repo", "enable_temporal": False},
    )
    elapsed = _run_index_source_expect_cancel(scheduler, source_repo, pid_file)
    assert_group_stopped(pid_file, elapsed)


def test_temporal_index_child_is_stopped_on_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_file: Path, source_repo: Path
) -> None:
    install_fake_cidx(tmp_path, monkeypatch, "--index-commits", pid_file)
    scheduler = make_scheduler(
        tmp_path,
        {
            "repo_url": "https://example.com/example-repo.git",
            "enable_temporal": True,
        },
    )
    elapsed = _run_index_source_expect_cancel(scheduler, source_repo, pid_file)
    assert_group_stopped(pid_file, elapsed)


def test_scip_child_is_stopped_on_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_file: Path, source_repo: Path
) -> None:
    install_fake_cidx(tmp_path, monkeypatch, "scip generate", pid_file)
    scheduler = make_scheduler(
        tmp_path,
        {
            "repo_url": "local://example-repo",
            "enable_temporal": False,
            "enable_scip": True,
        },
    )
    elapsed = _run_index_source_expect_cancel(scheduler, source_repo, pid_file)
    assert_group_stopped(pid_file, elapsed)


ALIAS = "example-repo-global"


class RecordingSnapshotManager:
    """Test double for the injected snapshot manager (an external
    collaborator): records every publish attempt instead of cloning."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def create_snapshot(self, repo_name: str, source_path: str) -> str:
        self.calls.append(repo_name)
        raise AssertionError("a cancelled refresh must never create a snapshot")

    def list_snapshots(self, repo_name: str) -> List[str]:
        return []

    def is_versioned_snapshot(self, path: str) -> bool:
        from code_indexer.server.storage.shared.snapshot_paths import (
            is_versioned_snapshot,
        )

        return bool(is_versioned_snapshot(path))


def make_local_repo_scheduler(
    tmp_path: Path, **kwargs: Any
) -> Tuple[RefreshScheduler, RecordingSnapshotManager]:
    """A REAL RefreshScheduler over a REAL GlobalRegistry and AliasManager
    with one registered local:// repo whose config is valid, so a refresh
    goes straight to indexing (no git pull)."""
    from code_indexer.config import ConfigManager
    from code_indexer.global_repos.global_registry import GlobalRegistry

    golden = tmp_path / "golden_repos"
    master = golden / "example-repo"
    (master / ".code-indexer").mkdir(parents=True)
    (master / ".code-indexer" / "config.json").write_text("{}")
    (master / "main.py").write_text("def main():\n    pass\n")
    registry = GlobalRegistry(str(golden))
    registry.register_global_repo(
        "example-repo", ALIAS, "local://example-repo", str(master)
    )
    (golden / "aliases").mkdir(exist_ok=True)
    (golden / "aliases" / f"{ALIAS}.json").write_text(
        json.dumps({"target_path": str(master)})
    )
    query_tracker = QueryTracker()
    recorder = RecordingSnapshotManager()
    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden),
        config_source=ConfigManager(tmp_path / ".code-indexer" / "config.json"),
        query_tracker=query_tracker,
        cleanup_manager=CleanupManager(query_tracker),
        registry=registry,
        # Duck-typed test double for the snapshot-manager collaborator.
        snapshot_manager=cast(Any, recorder),
        **kwargs,
    )
    return scheduler, recorder


def alias_target(tmp_path: Path) -> str:
    alias_file = tmp_path / "golden_repos" / "aliases" / f"{ALIAS}.json"
    return str(json.loads(alias_file.read_text())["target_path"])


def _wait_for(predicate: Callable[[], bool], bound_seconds: float) -> bool:
    deadline = time.monotonic() + bound_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def test_cancel_from_another_worker_stops_refresh_child_and_skips_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_file: Path
) -> None:
    """End to end through a REAL BackgroundJobManager: the cancel is written
    by a SEPARATE backend connection (as another worker/node does), and the
    job's DB-backed cancel_check must see it."""
    from code_indexer.server.repositories.background_jobs import (
        BackgroundJobManager,
    )
    from code_indexer.server.storage.sqlite_backends import (
        BackgroundJobsSqliteBackend,
    )

    from code_indexer.server.storage.database_manager import DatabaseSchema

    install_fake_cidx(tmp_path, monkeypatch, "--fts", pid_file)
    db_path = str(tmp_path / "jobs.db")
    DatabaseSchema(db_path).initialize_database()
    manager = BackgroundJobManager(use_sqlite=True, db_path=db_path)
    scheduler, recorder = make_local_repo_scheduler(
        tmp_path, background_job_manager=manager
    )
    target_before = alias_target(tmp_path)
    try:
        job_id = scheduler.trigger_refresh_for_repo(ALIAS)
        assert job_id is not None
        assert _wait_for(lambda: pid_file.exists() and pid_file.stat().st_size > 0, 30)

        cancelled_at = time.monotonic()
        BackgroundJobsSqliteBackend(db_path).update_job(
            job_id, status="cancelled", cancelled=True
        )

        pids = json.loads(pid_file.read_text())
        assert _wait_until_gone(pids["child_pid"], CANCEL_BOUND_SECONDS), (
            "refresh child still running after a cross-worker cancel"
        )
        assert _wait_until_gone(pids["grandchild_pid"], 5.0)
        assert time.monotonic() - cancelled_at < CANCEL_BOUND_SECONDS
        assert _wait_for(lambda: job_id not in manager._running_jobs, 15)

        row = BackgroundJobsSqliteBackend(db_path).get_job(job_id)
        assert row is not None and row["status"] == "cancelled"
        assert recorder.calls == []
        assert alias_target(tmp_path) == target_before
    finally:
        manager.shutdown()


def test_cancel_after_indexing_skips_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_file: Path
) -> None:
    """A cancel that lands after the last indexing step finished must still
    stop the refresh before the snapshot/alias publish."""
    indexed = tmp_path / "semantic_done"
    install_fake_cidx(tmp_path, monkeypatch, "never-matches", pid_file)
    monkeypatch.setenv("FAKE_CIDX_TOUCH_ON", "--fts")
    monkeypatch.setenv("FAKE_CIDX_TOUCH_FILE", str(indexed))
    scheduler, recorder = make_local_repo_scheduler(tmp_path)
    target_before = alias_target(tmp_path)

    with pytest.raises(IndexingCancelledError):
        scheduler._execute_refresh(ALIAS, cancel_check=indexed.exists)

    assert indexed.exists(), "indexing must have run before the cancel"
    assert recorder.calls == []
    assert alias_target(tmp_path) == target_before


def test_cancel_during_integrity_gate_skips_snapshot_and_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_file: Path
) -> None:
    """The cancel lands right after the post-indexing check (i.e. while the
    integrity gate runs): no snapshot may be created, no alias swapped."""
    indexed = tmp_path / "semantic_done"
    install_fake_cidx(tmp_path, monkeypatch, "never-matches", pid_file)
    monkeypatch.setenv("FAKE_CIDX_TOUCH_ON", "--fts")
    monkeypatch.setenv("FAKE_CIDX_TOUCH_FILE", str(indexed))
    scheduler, recorder = make_local_repo_scheduler(tmp_path)
    target_before = alias_target(tmp_path)
    looks_after_indexing: List[int] = []

    def cancel_arrives_during_gate() -> bool:
        if not indexed.exists():
            return False
        looks_after_indexing.append(1)
        return len(looks_after_indexing) > 1

    with pytest.raises(IndexingCancelledError):
        scheduler._execute_refresh(ALIAS, cancel_check=cancel_arrives_during_gate)

    assert recorder.calls == []
    assert alias_target(tmp_path) == target_before


class CancellingSnapshotManager(RecordingSnapshotManager):
    """Snapshot-manager double that really creates the versioned directory
    and, while doing so, has the job cancelled from another DB connection."""

    def __init__(self, golden: Path, cancel: Callable[[], None]) -> None:
        super().__init__()
        self._golden = golden
        self._cancel = cancel
        self.created: List[str] = []

    def create_snapshot(self, repo_name: str, source_path: str) -> str:
        self.calls.append(repo_name)
        import shutil

        snapshot = self._golden / ".versioned" / repo_name / f"v_{time.time_ns()}"
        shutil.copytree(source_path, snapshot)  # stands in for the CoW clone
        (snapshot / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)
        self._cancel()
        self.created.append(str(snapshot))
        return str(snapshot)


def test_cancel_during_snapshot_creation_never_swaps_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_file: Path
) -> None:
    from code_indexer.server.repositories.background_jobs import (
        BackgroundJobManager,
    )
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import (
        BackgroundJobsSqliteBackend,
    )

    install_fake_cidx(tmp_path, monkeypatch, "never-matches", pid_file)
    db_path = str(tmp_path / "jobs.db")
    DatabaseSchema(db_path).initialize_database()
    manager = BackgroundJobManager(use_sqlite=True, db_path=db_path)
    scheduler, _ = make_local_repo_scheduler(tmp_path, background_job_manager=manager)

    def cancel_running_refresh_from_another_worker() -> None:
        other_worker = BackgroundJobsSqliteBackend(db_path)
        running = other_worker.find_active_job_by_type_and_alias(
            "global_repo_refresh", ALIAS
        )
        assert running is not None, "the refresh job must be running"
        other_worker.update_job(running, status="cancelled", cancelled=True)

    snapshots = CancellingSnapshotManager(
        tmp_path / "golden_repos", cancel_running_refresh_from_another_worker
    )
    scheduler._snapshot_manager = cast(Any, snapshots)
    target_before = alias_target(tmp_path)
    try:
        job_id = scheduler.trigger_refresh_for_repo(ALIAS)
        assert job_id is not None
        reader = BackgroundJobsSqliteBackend(db_path)

        def job_finished() -> bool:
            row = reader.get_job(job_id)
            return row is not None and row["status"] not in ("pending", "running")

        assert _wait_for(job_finished, 60)
        assert _wait_for(lambda: job_id not in manager._running_jobs, 15)

        row = reader.get_job(job_id)
        assert row is not None and row["status"] == "cancelled"
        assert len(snapshots.created) == 1, "the cancel must land mid-snapshot"
        assert alias_target(tmp_path) == target_before, "alias swapped after cancel"
        assert (
            snapshots.created[0] in scheduler.cleanup_manager.get_pending_cleanups()
        ), "the unpublished snapshot of a cancelled refresh must be cleaned up"
    finally:
        manager.shutdown()


def test_no_cancel_check_keeps_scip_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_file: Path, source_repo: Path
) -> None:
    """Regression guard: with no cancel check every step still runs to
    completion (the fake cidx exits 0 for every step here)."""
    install_fake_cidx(tmp_path, monkeypatch, "never-matches", pid_file)
    scheduler = make_scheduler(
        tmp_path,
        {
            "repo_url": "local://example-repo",
            "enable_temporal": False,
            "enable_scip": True,
        },
    )
    calls: List[int] = []

    def never_cancelled() -> bool:
        calls.append(1)
        return False

    scheduler._index_source(
        alias_name="example-repo-global",
        source_path=str(source_repo),
        cancel_check=never_cancelled,
    )
    assert calls, "the cancel check must be consulted"
    assert not pid_file.exists()
