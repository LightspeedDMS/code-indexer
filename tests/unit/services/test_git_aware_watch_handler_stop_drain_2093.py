"""#2093 (review P1-1): stopping a watch never drops edits. A cycle still
running at stop finishes, then the processing thread drains the queue and
publishes before it exits; work it cannot do (the repository indexing lock
is held) is recorded in watch_metadata.json and re-queued by the next
watch start."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, cast

import pytest

from code_indexer.config import Config
from code_indexer.services import git_aware_watch_handler as watch_module
from code_indexer.services.git_aware_watch_handler import GitAwareWatchHandler
from code_indexer.services.git_topology_service import GitTopologyService
from code_indexer.services.watch_metadata import WatchMetadata
from tests.unit.services.test_git_aware_watch_handler_indexing_lock_2093 import (  # noqa: F401
    _hold_lock,
    _Stats,
    other_live_process,
)


@dataclass
class _SlowIndexer:
    """Stands in for the SmartIndexer; its first batch blocks until released."""

    calls: List[Any] = field(default_factory=list)
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    slow_first: bool = True

    def process_files_incrementally(self, paths: List[str], **_: Any) -> _Stats:
        first = not self.calls
        self.calls.append(sorted(paths))
        if first and self.slow_first:
            self.entered.set()
            assert self.release.wait(timeout=60)
        return _Stats()

    def publish_owed_hnsw(self) -> bool:
        self.calls.append("publish")
        return True


def _metadata_path(root: Path) -> Path:
    return root / ".code-indexer" / "watch_metadata.json"


def _handler(
    root: Path,
    indexer: _SlowIndexer,
    hold_lock: bool = False,
    metadata: Optional[WatchMetadata] = None,
) -> GitAwareWatchHandler:
    config = Config(codebase_dir=root)
    return GitAwareWatchHandler(
        config=config,
        # ^ why cast, not a real SmartIndexer; the double has exactly the two methods a cycle calls
        smart_indexer=cast(Any, indexer),
        git_topology_service=GitTopologyService(root, config),
        watch_metadata=metadata or WatchMetadata(),
        debounce_seconds=0.05,
        hold_indexing_lock=hold_lock,
    )


def _sources(root: Path, *names: str) -> List[Path]:
    (root / "src").mkdir(exist_ok=True)
    paths = [root / "src" / name for name in names]
    for path in paths:
        path.write_text("def f():\n    return 1\n")
    return paths


def _recovery(root: Path) -> List[str]:
    return sorted(
        WatchMetadata.load_from_disk(_metadata_path(root)).get_recovery_files()
    )


def test_stop_during_a_slow_cycle_drains_and_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_module, "STOP_JOIN_SECONDS", 0.5)
    first, second = _sources(tmp_path, "a.py", "b.py")
    indexer = _SlowIndexer()
    handler = _handler(tmp_path, indexer)
    handler.start_watching()
    try:
        handler.queue_changes([first])
        assert indexer.entered.wait(timeout=10)
        handler.queue_changes([second])

        handler.stop_watching()

        assert _recovery(tmp_path) == sorted([str(first), str(second)])
    finally:
        indexer.release.set()
    assert handler.processing_thread is not None
    handler.processing_thread.join(timeout=10)
    assert not handler.processing_thread.is_alive()
    assert indexer.calls == [["src/a.py"], ["src/b.py"], "publish"]
    assert _recovery(tmp_path) == []


def test_stop_with_the_lock_held_records_the_work_for_the_next_watch(
    tmp_path: Path,
    other_live_process: int,  # noqa: F811
) -> None:
    (source,) = _sources(tmp_path, "a.py")
    heartbeat = _hold_lock(tmp_path, other_live_process)
    blocked = _SlowIndexer(slow_first=False)
    handler = _handler(tmp_path, blocked, hold_lock=True)
    handler.queue_changes([source])

    handler.stop_watching()

    assert blocked.calls == []
    assert _recovery(tmp_path) == [str(source)]

    heartbeat.unlink()
    resumed = _SlowIndexer(slow_first=False)
    next_watch = _handler(
        tmp_path,
        resumed,
        hold_lock=True,
        metadata=WatchMetadata.load_from_disk(_metadata_path(tmp_path)),
    )
    next_watch.start_watching()
    next_watch.stop_watching()
    assert next_watch.processing_thread is not None
    next_watch.processing_thread.join(timeout=10)

    assert resumed.calls == [["src/a.py"], "publish"]
    assert _recovery(tmp_path) == []
