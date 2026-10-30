"""#2093: a server-watch cycle holds the repository indexing lock (the one
`cidx index` holds), so it never writes while a server-spawned `cidx index`
runs on the same repository, and releases it after the cycle."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, List, cast

import pytest

from code_indexer.config import Config
from code_indexer.services.git_aware_watch_handler import GitAwareWatchHandler
from code_indexer.services.git_topology_service import GitTopologyService
from code_indexer.services.watch_metadata import WatchMetadata


@dataclass
class _Stats:
    files_processed: int = 1
    failed_files: int = 0
    failed_paths: frozenset = frozenset()


@dataclass
class _RecordingIndexer:
    """Stands in for the SmartIndexer: records what the cycle asked of it."""

    calls: List[str] = field(default_factory=list)

    def process_files_incrementally(self, paths: List[str], **_: Any) -> _Stats:
        self.calls.extend(paths)
        return _Stats()

    def publish_owed_hnsw(self) -> bool:
        self.calls.append("publish")
        return True


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


@pytest.fixture
def other_live_process() -> Iterator[int]:
    proc = subprocess.Popen(["sleep", "120"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _hold_lock(root: Path, pid: int) -> Path:
    heartbeat = root / ".code-indexer" / "indexing_heartbeat.json"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    heartbeat.write_text(
        json.dumps(
            {
                "pid": pid,
                "project_path": str(root),
                "started_at": now,
                "last_heartbeat": now,
            }
        )
    )
    return heartbeat


def test_cycle_defers_while_another_process_holds_the_lock(
    tmp_path: Path, other_live_process: int
) -> None:
    source = tmp_path / "src" / "module.py"
    source.parent.mkdir()
    source.write_text("def f():\n    return 1\n")
    heartbeat = _hold_lock(tmp_path, other_live_process)
    indexer = _RecordingIndexer()
    handler = _handler(tmp_path, indexer)
    handler.queue_changes([source])

    handler._process_pending_changes()

    assert indexer.calls == []
    assert handler._change_backoff.scheduled() == {source}
    assert json.loads(heartbeat.read_text())["pid"] == other_live_process


def test_cycle_runs_and_releases_a_free_lock(tmp_path: Path) -> None:
    source = tmp_path / "src" / "module.py"
    source.parent.mkdir()
    source.write_text("def f():\n    return 1\n")
    indexer = _RecordingIndexer()
    handler = _handler(tmp_path, indexer)
    handler.queue_changes([source])

    handler._process_pending_changes()

    assert indexer.calls == ["src/module.py", "publish"]
    assert not (tmp_path / ".code-indexer" / "indexing_heartbeat.json").exists()
