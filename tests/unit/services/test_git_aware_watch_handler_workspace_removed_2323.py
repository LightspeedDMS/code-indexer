"""#2323: a server watch whose workspace is removed under it (deactivation,
possibly on another node) stops itself and never recreates the workspace."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, cast

import pytest

from code_indexer.config import Config
from code_indexer.services import git_aware_watch_handler as handler_module
from code_indexer.services.git_aware_watch_handler import GitAwareWatchHandler
from code_indexer.services.git_topology_service import GitTopologyService
from code_indexer.services.watch_metadata import WatchMetadata

HANDLER_LOGGER = "code_indexer.services.git_aware_watch_handler"


@dataclass
class _Stats:
    files_processed: int = 1
    failed_files: int = 0
    failed_paths: frozenset = frozenset()


@dataclass
class _RecordingIndexer:
    calls: List[str] = field(default_factory=list)

    def process_files_incrementally(self, paths: List[str], **_: Any) -> _Stats:
        self.calls.extend(paths)
        return _Stats()

    def publish_owed_hnsw(self) -> bool:
        self.calls.append("publish")
        return True


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _workspace(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "module.py").write_text("def f():\n    return 1\n")
    (root / ".code-indexer").mkdir()
    (root / ".gitignore").write_text(".code-indexer/\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root / "src" / "module.py"


def _handler(root: Path, indexer: _RecordingIndexer) -> GitAwareWatchHandler:
    config = Config(codebase_dir=root)
    return GitAwareWatchHandler(
        config=config,
        # ^ why cast, not a real SmartIndexer; the recording double has exactly the two methods a cycle calls
        smart_indexer=cast(Any, indexer),
        git_topology_service=GitTopologyService(root, config),
        watch_metadata=WatchMetadata(),
        debounce_seconds=0.1,
        hold_indexing_lock=True,
    )


def _wait_until(condition: Callable[[], bool], seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.05)
    return condition()


def _handler_records(caplog: pytest.LogCaptureFixture) -> List[logging.LogRecord]:
    return [r for r in caplog.records if r.name == HANDLER_LOGGER]


def test_watch_stops_itself_when_its_workspace_is_removed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "workspace"
    source = _workspace(root)
    indexer = _RecordingIndexer()
    handler = _handler(root, indexer)
    handler.start_watching()
    try:
        source.write_text("def f():\n    return 2\n")
        handler.queue_changes([source])
        assert _wait_until(lambda: "src/module.py" in indexer.calls)
        caplog.clear()
        caplog.set_level(logging.INFO, logger=HANDLER_LOGGER)

        shutil.rmtree(root)

        assert _wait_until(lambda: not handler.is_watching())
        assert handler.observer is not None
        assert _wait_until(lambda: not handler.observer.is_alive())  # type: ignore[union-attr]
    finally:
        handler.stop_watching()
    assert not root.exists()
    records = _handler_records(caplog)
    assert [r for r in records if r.levelno >= logging.WARNING] == []
    assert len([r for r in records if "workspace" in r.getMessage()]) == 1


@dataclass
class _GatedIndexer(_RecordingIndexer):
    entered: threading.Event = field(default_factory=threading.Event)
    gate: threading.Event = field(default_factory=threading.Event)

    def process_files_incrementally(self, paths: List[str], **kw: Any) -> _Stats:
        self.entered.set()
        assert self.gate.wait(timeout=60)
        return super().process_files_incrementally(paths, **kw)


def test_discard_stop_waits_for_the_running_cycle_and_does_no_new_work(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    source = _workspace(root)
    indexer = _GatedIndexer()
    handler = _handler(root, indexer)
    handler.start_watching()
    handler.queue_changes([source])
    assert indexer.entered.wait(timeout=10)
    handler.queue_changes([source])

    stopper = threading.Thread(target=handler.stop_watching, kwargs={"discard": True})
    stopper.start()
    stopper.join(timeout=1.0)
    still_waiting = stopper.is_alive()
    indexer.gate.set()
    stopper.join(timeout=30)

    assert still_waiting is True
    assert not stopper.is_alive()
    assert handler.processing_thread is not None
    assert not handler.processing_thread.is_alive()
    assert indexer.calls == ["src/module.py"]
    assert not (root / ".code-indexer" / "watch_metadata.json").exists()


def _stat_with(path: Path, dev: int, ino: int) -> os.stat_result:
    real = os.stat(path)
    return os.stat_result((real.st_mode, ino, dev, *tuple(real)[3:10]))


def test_stale_handle_is_not_removal_and_warns_once_per_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "workspace"
    _workspace(root)
    handler = _handler(root, _RecordingIndexer())
    real_stat = os.stat
    failing = [True]

    def stale_then_real(path: Path) -> os.stat_result:
        if failing[0]:
            raise OSError(errno.ESTALE, "Stale file handle")
        return real_stat(path)

    # ^ why a stubbed stat, not a real NFS mount; ESTALE cannot be produced on a local disk
    monkeypatch.setattr(handler_module, "_stat_workspace", stale_then_real)
    caplog.set_level(logging.INFO, logger=HANDLER_LOGGER)

    assert handler._workspace_removed() is False
    assert handler._workspace_removed() is False
    failing[0] = False
    assert handler._workspace_removed() is False
    failing[0] = True
    assert handler._workspace_removed() is False

    warnings = [r for r in _handler_records(caplog) if r.levelno == logging.WARNING]
    assert len(warnings) == 2


def test_remount_is_not_removal_but_a_new_inode_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    _workspace(root)
    handler = _handler(root, _RecordingIndexer())
    real = os.stat(root)
    seen = [(real.st_dev + 1, real.st_ino)]
    monkeypatch.setattr(
        handler_module, "_stat_workspace", lambda path: _stat_with(path, *seen[0])
    )

    assert handler._workspace_removed() is False
    seen[0] = (real.st_dev + 1, real.st_ino + 1)
    assert handler._workspace_removed() is True


def test_lock_on_removed_workspace_recreates_nothing(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _workspace(root)
    handler = _handler(root, _RecordingIndexer())
    shutil.rmtree(root)

    with handler._repository_lock() as locked:
        assert locked is False

    assert not root.exists()


def test_indexing_lock_without_parents_never_recreates_a_removed_root(
    tmp_path: Path,
) -> None:
    from code_indexer.services.indexing_lock import (
        IndexingLockError,
        create_indexing_lock,
    )

    root = tmp_path / "workspace"
    lock = create_indexing_lock(root / ".code-indexer", create_parents=False)

    with pytest.raises(IndexingLockError):
        lock.acquire(str(root))

    assert not root.exists()


def test_metadata_save_without_parents_never_recreates_a_removed_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"

    with pytest.raises(FileNotFoundError):
        WatchMetadata().save_to_disk(
            root / ".code-indexer" / "watch_metadata.json", create_parents=False
        )

    assert not root.exists()


def test_owed_changes_of_removed_workspace_are_not_saved(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "workspace"
    source = _workspace(root)
    handler = _handler(root, _RecordingIndexer())
    handler.queue_changes([source])
    shutil.rmtree(root)
    caplog.set_level(logging.INFO, logger=HANDLER_LOGGER)

    handler._persist_owed_changes(set())
    handler._drain_on_stop()

    assert not root.exists()
    assert [r for r in _handler_records(caplog) if r.levelno >= logging.WARNING] == []


def test_watch_stops_when_its_workspace_is_replaced(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _workspace(root)
    indexer = _RecordingIndexer()
    handler = _handler(root, indexer)
    handler.start_watching()
    try:
        shutil.rmtree(root)
        (root / ".code-indexer").mkdir(parents=True)
        (root / "src").mkdir()
        replacement = root / "src" / "module.py"
        replacement.write_text("def g():\n    return 3\n")
        handler.queue_changes([replacement])

        assert _wait_until(lambda: not handler.is_watching())
    finally:
        handler.stop_watching()
    assert indexer.calls == []
    assert sorted(p.name for p in (root / ".code-indexer").iterdir()) == []


def test_stop_of_existing_workspace_still_records_owed_changes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    source = _workspace(root)
    handler = _handler(root, _RecordingIndexer())
    handler.queue_changes([source])

    handler._persist_owed_changes({source})

    saved = WatchMetadata.load_from_disk(root / ".code-indexer" / "watch_metadata.json")
    assert saved.files_being_processed == [str(source)]
    assert saved.processing_interrupted is True
