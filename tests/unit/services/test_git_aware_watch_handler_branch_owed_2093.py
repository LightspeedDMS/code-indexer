"""#2093 (confirm review P2 1-3): a branch transition stays owed until it is
applied. An edit queued while it runs is kept; transitions deferred by the
repository lock combine (first base, latest destination); a deferred
transition survives a watch stop; a failed one is retried.

Real pieces: a git repository with branches main, b, c and the real
GitTopologyService analysis. The indexer is a recording double."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, cast

from code_indexer.config import Config
from code_indexer.services.git_aware_watch_handler import GitAwareWatchHandler
from code_indexer.services.git_topology_service import GitTopologyService
from code_indexer.services.watch_metadata import WatchMetadata
from tests.unit.services.test_git_aware_watch_handler_indexing_lock_2093 import (  # noqa: F401
    _hold_lock,
    _Stats,
    other_live_process,
)


class _Store:
    def resolve_collection_name(self, config: Any, provider: Any) -> str:
        return "example-collection"


@dataclass
class _Indexer:
    """Stands in for the SmartIndexer: records what each call was asked."""

    calls: List[List[str]] = field(default_factory=list)
    fail_next: int = 0
    on_call: Optional[Callable[[], None]] = None
    config: Any = None
    embedding_provider: Any = None
    vector_store_client: _Store = field(default_factory=_Store)

    def process_files_incrementally(self, paths: List[str], **_: Any) -> _Stats:
        self.calls.append(sorted(paths))
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("transient store failure")
        if self.on_call is not None:
            hook, self.on_call = self.on_call, None
            hook()
        return _Stats()

    def _ensure_file_visible_in_branch_thread_safe(self, *_: Any) -> None:
        return None

    def publish_owed_hnsw(self) -> bool:
        return True


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()


def _repo(root: Path) -> Dict[str, str]:
    (root / "src").mkdir()
    _git(root, "init", "-q", "-b", "main")
    heads = {}
    for branch, name in (("main", "common"), ("b", "b"), ("c", "c")):
        if branch != "main":
            _git(root, "checkout", "-q", "-b", branch)
        (root / "src" / f"{name}.py").write_text(f"def {name}():\n    return 1\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", branch)
        heads[branch] = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "main")
    return heads


def _event(heads: Dict[str, str], old: str, new: str) -> Dict[str, Any]:
    return {
        "old_branch": old,
        "new_branch": new,
        "old_commit": heads[old],
        "new_commit": heads[new],
    }


def _handler(
    root: Path,
    indexer: _Indexer,
    hold_lock: bool = False,
    metadata: Optional[WatchMetadata] = None,
) -> GitAwareWatchHandler:
    config = Config(codebase_dir=root)
    return GitAwareWatchHandler(
        config=config,
        # ^ why cast, not a real SmartIndexer; the double has exactly what a branch change calls
        smart_indexer=cast(Any, indexer),
        git_topology_service=GitTopologyService(root, config),
        watch_metadata=metadata or WatchMetadata(),
        debounce_seconds=0.05,
        hold_indexing_lock=hold_lock,
    )


def test_an_edit_queued_during_the_branch_change_is_kept(tmp_path: Path) -> None:
    heads = _repo(tmp_path)
    _git(tmp_path, "checkout", "-q", "b")
    indexer = _Indexer()
    handler = _handler(tmp_path, indexer)
    edited = tmp_path / "src" / "b.py"

    def edit_mid_indexing() -> None:
        edited.write_text("def b():\n    return 2\n")
        handler.queue_changes([edited])

    indexer.on_call = edit_mid_indexing

    handler._handle_branch_change(_event(heads, "main", "b"))

    assert edited in handler.pending_changes
    handler._process_pending_changes()
    assert indexer.calls == [["src/b.py"], ["src/b.py"]]


def test_deferred_transitions_combine_first_base_and_latest_destination(
    tmp_path: Path,
    other_live_process: int,  # noqa: F811
) -> None:
    heads = _repo(tmp_path)
    heartbeat = _hold_lock(tmp_path, other_live_process)
    indexer = _Indexer()
    handler = _handler(tmp_path, indexer, hold_lock=True)
    _git(tmp_path, "checkout", "-q", "b")
    handler._handle_branch_change(_event(heads, "main", "b"))
    _git(tmp_path, "checkout", "-q", "c")
    handler._handle_branch_change(_event(heads, "b", "c"))
    assert indexer.calls == []

    heartbeat.unlink()
    handler._process_pending_changes()

    assert indexer.calls == [["src/b.py", "src/c.py"]]


def test_a_transition_deferred_at_stop_is_replayed_by_the_next_watch(
    tmp_path: Path,
    other_live_process: int,  # noqa: F811
) -> None:
    heads = _repo(tmp_path)
    heartbeat = _hold_lock(tmp_path, other_live_process)
    _git(tmp_path, "checkout", "-q", "b")
    handler = _handler(tmp_path, _Indexer(), hold_lock=True)
    handler._handle_branch_change(_event(heads, "main", "b"))
    handler.stop_watching()

    heartbeat.unlink()
    indexer = _Indexer()
    next_watch = _handler(
        tmp_path,
        indexer,
        hold_lock=True,
        metadata=WatchMetadata.load_from_disk(
            tmp_path / ".code-indexer" / "watch_metadata.json"
        ),
    )
    next_watch.start_watching()
    next_watch.stop_watching()
    assert next_watch.processing_thread is not None
    next_watch.processing_thread.join(timeout=10)

    assert ["src/b.py"] in indexer.calls


def test_a_restored_transition_targets_the_current_branch(
    tmp_path: Path,
    other_live_process: int,  # noqa: F811
) -> None:
    heads = _repo(tmp_path)
    heartbeat = _hold_lock(tmp_path, other_live_process)
    _git(tmp_path, "checkout", "-q", "b")
    handler = _handler(tmp_path, _Indexer(), hold_lock=True)
    handler._handle_branch_change(_event(heads, "main", "b"))
    handler.stop_watching()
    heartbeat.unlink()
    _git(tmp_path, "checkout", "-q", "c")

    indexer = _Indexer()
    next_watch = _handler(
        tmp_path,
        indexer,
        hold_lock=True,
        metadata=WatchMetadata.load_from_disk(
            tmp_path / ".code-indexer" / "watch_metadata.json"
        ),
    )
    next_watch.start_watching()
    next_watch.stop_watching()
    assert next_watch.processing_thread is not None
    next_watch.processing_thread.join(timeout=10)

    assert ["src/b.py", "src/c.py"] in indexer.calls


def test_a_failed_transition_is_retried_until_applied(tmp_path: Path) -> None:
    heads = _repo(tmp_path)
    _git(tmp_path, "checkout", "-q", "b")
    indexer = _Indexer(fail_next=1)
    handler = _handler(tmp_path, indexer)
    now = [100.0]
    handler._clock = lambda: now[0]

    handler._handle_branch_change(_event(heads, "main", "b"))
    now[0] += 60.0
    handler._process_pending_changes()

    assert indexer.calls == [["src/b.py"], ["src/b.py"]]
    handler._process_pending_changes()
    assert indexer.calls == [["src/b.py"], ["src/b.py"]]
