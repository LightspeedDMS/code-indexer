from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple, cast

import pytest

from code_indexer.config import Config, ConfigManager
from code_indexer.services.embedding_factory import EmbeddingProviderFactory
from code_indexer.services.git_aware_watch_handler import GitAwareWatchHandler
from code_indexer.services.git_topology_service import GitTopologyService
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.services.watch_metadata import WatchMetadata
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from tests.unit.global_repos.test_local_git_source_removal_2194 import (
    _DOOMED,
    _FAKE_KEY,
    _KEPT,
    _git,
    _indexed_git_repo,
)
from tests.utils.cidx_fake_provider_repo import (
    COLLECTION,
    FakeProviderRepo,
    unique_python_text,
)
from tests.utils.fake_voyage_provider import running_fake_voyage_server

_FEATURE = "feature"


class _FlakyStore(FilesystemVectorStore):
    """A real store whose next ``failures`` payload updates fail non-fatally."""

    failures = 0

    def _batch_update_payload_only(
        self, points: List[Dict[str, Any]], collection_name: str
    ) -> bool:
        if self.failures > 0:
            self.failures -= 1
            return False
        return super()._batch_update_payload_only(points, collection_name)


def _index_dir(repo: FakeProviderRepo) -> Path:
    return repo.repo / ".code-indexer" / "index"


def _reader(repo: FakeProviderRepo) -> FilesystemVectorStore:
    return FilesystemVectorStore(base_path=_index_dir(repo), project_root=repo.repo)


def _content_points(store: FilesystemVectorStore, path: str) -> List[Dict[str, Any]]:
    points, _ = store.scroll_points(
        collection_name=COLLECTION,
        filter_conditions={
            "must": [
                {"key": "type", "match": {"value": "content"}},
                {"key": "path", "match": {"value": path}},
            ]
        },
        limit=1000,
        with_payload=True,
        with_vectors=False,
    )
    return list(points)


def _hidden(store: FilesystemVectorStore, path: str) -> Set[str]:
    points = _content_points(store, path)
    assert points, f"no stored points for {path}"
    return {b for p in points for b in p["payload"].get("hidden_branches", [])}


def _hide_on(store: FilesystemVectorStore, path: str, branch: str) -> None:
    updates = [
        {"id": p["id"], "payload": {"hidden_branches": [branch]}}
        for p in _content_points(store, path)
    ]
    assert store._batch_update_payload_only(updates, COLLECTION)


def _head(repo: FakeProviderRepo) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo.repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class _LockProbeStore(_FlakyStore):
    """Records, per payload update, whether another thread held ``probe_lock``."""

    probe_lock: Any = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.held: List[bool] = []

    def _batch_update_payload_only(
        self, points: List[Dict[str, Any]], collection_name: str
    ) -> bool:
        if self.probe_lock is not None:
            acquired: List[bool] = []

            def probe() -> None:
                got = self.probe_lock.acquire(blocking=False)
                if got:
                    self.probe_lock.release()
                acquired.append(got)

            prober = threading.Thread(target=probe)
            prober.start()
            prober.join(timeout=10)
            self.held.append(acquired == [False])
        return super()._batch_update_payload_only(points, collection_name)


def _watch(
    repo: FakeProviderRepo,
    monkeypatch: pytest.MonkeyPatch,
    failures: int,
    store_class: type = _FlakyStore,
    mutation_lock: Any = None,
) -> Tuple[GitAwareWatchHandler, Any]:
    monkeypatch.setenv("VOYAGE_API_KEY", _FAKE_KEY)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    config = ConfigManager.load_verified_config(repo.repo)
    store = store_class(base_path=_index_dir(repo), project_root=repo.repo)
    store.failures = failures
    store.probe_lock = mutation_lock
    indexer = SmartIndexer(
        config,
        EmbeddingProviderFactory.create(config=config),
        store,
        repo.metadata_path,
    )
    handler = GitAwareWatchHandler(
        config=config,
        smart_indexer=indexer,
        git_topology_service=GitTopologyService(repo.repo, config),
        watch_metadata=WatchMetadata(),
        debounce_seconds=0.1,
        mutation_lock=mutation_lock,
    )
    return handler, store


def _watch_metadata_file(repo: FakeProviderRepo) -> Path:
    return repo.repo / ".code-indexer" / "watch_metadata.json"


def test_failed_branch_unhide_is_retried_and_the_transition_recorded_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, _ = _indexed_git_repo(tmp_path, server, False)
        kept = f"src/{_KEPT}.py"
        reader = _reader(repo)
        _hide_on(reader, kept, _FEATURE)
        _git(repo.repo, "branch", _FEATURE)
        head = _head(repo)
        handler, store = _watch(repo, monkeypatch, failures=1)
        clock = _Clock(_START)
        handler._clock = clock
        handler.watch_metadata.update_git_state("master", head)

        handler._handle_branch_change(
            {
                "old_branch": "master",
                "new_branch": _FEATURE,
                "old_commit": head,
                "new_commit": head,
            }
        )

        assert store.failures == 0
        assert _FEATURE in _hidden(reader, kept)
        assert handler.watch_metadata.current_branch == "master"
        assert not _watch_metadata_file(repo).exists()

        clock.now += _BACKOFF_BASE
        handler._process_pending_changes()

        assert _FEATURE not in _hidden(reader, kept)
        assert handler.watch_metadata.current_branch == _FEATURE
        assert _watch_metadata_file(repo).exists()


def test_failed_watch_deletion_keeps_the_batch_going_and_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed = _indexed_git_repo(tmp_path, server, False)
        doomed.unlink()
        added = repo.repo / "src" / "added_000.py"
        added.write_text(unique_python_text("added_000", 40))
        reader = _reader(repo)
        handler, store = _watch(repo, monkeypatch, failures=1)
        clock = _Clock(_START)
        handler._clock = clock
        handler.pending_changes.update({doomed, added})

        handler._process_pending_changes()

        assert store.failures == 0
        assert _content_points(reader, "src/added_000.py")
        assert handler.pending_changes == set()
        assert handler._change_backoff.scheduled() == {doomed}
        assert handler.watch_metadata.processing_interrupted

        clock.now += _BACKOFF_BASE
        handler._process_pending_changes()

        assert "master" in _hidden(reader, f"src/{_DOOMED}.py")
        assert handler.pending_changes == set()
        assert handler._change_backoff.scheduled() == set()
        assert not handler.watch_metadata.processing_interrupted


def test_failed_changed_file_in_watch_cycle_is_requeued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, _ = _indexed_git_repo(tmp_path, server, False)
        broken = repo.repo / "src" / "kept_001.py"
        broken.write_text(unique_python_text("kept_001_edited", 40))
        handler, _ = _watch(repo, monkeypatch, failures=0)
        handler.pending_changes.add(broken)
        broken.chmod(0)
        try:
            handler._process_pending_changes()
        finally:
            broken.chmod(0o644)

        assert handler._change_backoff.scheduled() == {broken}
        assert handler.watch_metadata.processing_interrupted


def test_failed_branch_change_reindex_file_is_requeued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, _ = _indexed_git_repo(tmp_path, server, False)
        old_head = _head(repo)
        _git(repo.repo, "checkout", "-q", "-b", _FEATURE)
        broken = repo.repo / "src" / "kept_001.py"
        broken.write_text(unique_python_text("kept_001_on_feature", 40))
        _git(repo.repo, "commit", "-qam", "change on feature")
        new_head = _head(repo)
        handler, _ = _watch(repo, monkeypatch, failures=0)
        broken.chmod(0)
        try:
            handler._handle_branch_change(
                {
                    "old_branch": "master",
                    "new_branch": _FEATURE,
                    "old_commit": old_head,
                    "new_commit": new_head,
                }
            )
        finally:
            broken.chmod(0o644)

        assert broken in handler._change_backoff.scheduled()
        assert handler.watch_metadata.processing_interrupted


def test_owed_unhide_of_a_deleted_file_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, _ = _indexed_git_repo(tmp_path, server, False)
        kept = f"src/{_KEPT}.py"
        reader = _reader(repo)
        _hide_on(reader, kept, _FEATURE)
        _git(repo.repo, "branch", _FEATURE)
        head = _head(repo)
        handler, _ = _watch(repo, monkeypatch, failures=1)
        handler._handle_branch_change(
            {
                "old_branch": "master",
                "new_branch": _FEATURE,
                "old_commit": head,
                "new_commit": head,
            }
        )
        (repo.repo / kept).unlink()

        handler._process_pending_changes()

        assert _FEATURE in _hidden(reader, kept)
        assert handler._pending_unhides == {}
        assert handler.watch_metadata.current_branch == _FEATURE
        assert _watch_metadata_file(repo).exists()


def test_unhide_retry_holds_the_mutation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, _ = _indexed_git_repo(tmp_path, server, False)
        kept = f"src/{_KEPT}.py"
        reader = _reader(repo)
        _hide_on(reader, kept, _FEATURE)
        _git(repo.repo, "branch", _FEATURE)
        head = _head(repo)
        lock = threading.RLock()
        handler, store = _watch(
            repo, monkeypatch, 1, store_class=_LockProbeStore, mutation_lock=lock
        )
        clock = _Clock(_START)
        handler._clock = clock
        handler._handle_branch_change(
            {
                "old_branch": "master",
                "new_branch": _FEATURE,
                "old_commit": head,
                "new_commit": head,
            }
        )

        clock.now += _BACKOFF_BASE
        handler._process_pending_changes()

        assert store.held[-1] is True
        assert _FEATURE not in _hidden(reader, kept)


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


_ALWAYS_FAIL = 10_000
_START = 1000.0
_STEP = 0.05
_RAMP_STEPS = 80
_BACKOFF_BASE = 0.1  # the handler's debounce in _watch
_BACKOFF_CAP = 300.0
_RAMP_INTERVALS = [_BACKOFF_BASE * 2**k for k in range(5)]
_CAPPED_FAILURES = 13  # _BACKOFF_BASE * 2**12 > _BACKOFF_CAP
_FAR_FUTURE = 1000.0
_EPS = 1e-6
_PROBE = 0.1


def test_permanently_failing_unhide_backs_off_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, _ = _indexed_git_repo(tmp_path, server, False)
        kept = f"src/{_KEPT}.py"
        reader = _reader(repo)
        _hide_on(reader, kept, _FEATURE)
        _git(repo.repo, "branch", _FEATURE)
        head = _head(repo)
        handler, store = _watch(repo, monkeypatch, failures=_ALWAYS_FAIL)
        clock = _Clock(_START)
        handler._clock = clock
        handler._handle_branch_change(
            {
                "old_branch": "master",
                "new_branch": _FEATURE,
                "old_commit": head,
                "new_commit": head,
            }
        )

        def failed_attempts() -> int:
            return _ALWAYS_FAIL - int(store.failures)

        attempt_times = [_START]
        for i in range(1, _RAMP_STEPS + 1):
            clock.now = _START + i * _STEP
            handler._process_pending_changes()
            if failed_attempts() > len(attempt_times):
                attempt_times.append(clock.now)
        intervals = [b - a for a, b in zip(attempt_times, attempt_times[1:])]
        assert len(intervals) == len(_RAMP_INTERVALS), attempt_times
        for got, expected in zip(intervals, _RAMP_INTERVALS):
            assert expected - _EPS <= got <= expected + _STEP + _EPS, intervals

        for _ in range(_CAPPED_FAILURES):
            if failed_attempts() >= _CAPPED_FAILURES:
                break
            clock.now += _FAR_FUTURE
            handler._process_pending_changes()
        assert failed_attempts() == _CAPPED_FAILURES
        last = clock.now
        clock.now = last + _BACKOFF_CAP - _PROBE
        handler._process_pending_changes()
        assert failed_attempts() == _CAPPED_FAILURES
        clock.now = last + _BACKOFF_CAP + _PROBE
        handler._process_pending_changes()
        assert failed_attempts() == _CAPPED_FAILURES + 1

        store.failures = 0
        clock.now += _BACKOFF_CAP + 1
        handler._process_pending_changes()

        assert _FEATURE not in _hidden(reader, kept)
        assert handler._pending_unhides == {}
        assert handler.watch_metadata.current_branch == _FEATURE


def test_failed_change_retries_back_off_and_reset_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, _ = _indexed_git_repo(tmp_path, server, False)
        broken = repo.repo / "src" / "kept_001.py"
        broken.write_text(unique_python_text("kept_001_edited", 40))
        handler, _ = _watch(repo, monkeypatch, failures=0)
        clock = _Clock(_START)
        handler._clock = clock

        def attempted_at(offset: float) -> bool:
            before = handler.indexing_cycles_count
            clock.now = _START + offset
            handler._process_pending_changes()
            return handler.indexing_cycles_count > before

        slack = _STEP / 2
        broken.chmod(0)
        try:
            handler.pending_changes.add(broken)
            assert attempted_at(0.0)
            assert not attempted_at(_BACKOFF_BASE - slack)
            first = _BACKOFF_BASE + slack
            assert attempted_at(first)
            assert not attempted_at(first + 2 * _BACKOFF_BASE - slack)
            second = first + 2 * _BACKOFF_BASE + slack
            assert attempted_at(second)
            broken.chmod(0o644)
            recovered = second + 4 * _BACKOFF_BASE + slack
            assert attempted_at(recovered)
            assert handler._change_backoff.scheduled() == set()
            assert not handler.watch_metadata.processing_interrupted

            broken.chmod(0)
            handler.pending_changes.add(broken)
            refailed = recovered + slack
            assert attempted_at(refailed)
            assert not attempted_at(refailed + _BACKOFF_BASE - slack)
            assert attempted_at(refailed + _BACKOFF_BASE + slack)
        finally:
            broken.chmod(0o644)


def test_cycle_failing_on_a_broken_store_backs_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, _ = _indexed_git_repo(tmp_path, server, False)
        changed = repo.repo / "src" / "kept_001.py"
        changed.write_text(unique_python_text("kept_001_edited", 40))
        repo.chunks_db.unlink()
        handler, _ = _watch(repo, monkeypatch, failures=0)
        clock = _Clock(_START)
        handler._clock = clock
        handler.pending_changes.add(changed)

        handler._process_pending_changes()

        assert changed not in handler.pending_changes
        assert handler._change_backoff.scheduled() == {changed}
        assert handler.watch_metadata.processing_interrupted
        clock.now = _START + _STEP
        handler._process_pending_changes()
        assert changed not in handler.pending_changes


def _bare_handler(root: Path, mutation_lock: Any = None) -> GitAwareWatchHandler:
    config = Config(codebase_dir=root)
    return GitAwareWatchHandler(
        config=config,
        smart_indexer=cast(Any, None),
        git_topology_service=GitTopologyService(root, config),
        watch_metadata=WatchMetadata(),
        debounce_seconds=_BACKOFF_BASE,
        mutation_lock=mutation_lock,
    )


_STOP_BOUND_SECONDS = 5.0


def test_final_cleanup_never_waits_for_a_stuck_cycles_lock(tmp_path: Path) -> None:
    lock = threading.RLock()
    holding = threading.Event()
    release = threading.Event()

    def stuck_cycle() -> None:
        with lock:
            holding.set()
            release.wait(timeout=60)

    holder = threading.Thread(target=stuck_cycle)
    holder.start()
    try:
        assert holding.wait(timeout=_STOP_BOUND_SECONDS)
        handler = _bare_handler(tmp_path, mutation_lock=lock)
        handler.processing_in_progress = True
        handler._pending_unhides["src/owed.py"] = (_FEATURE, COLLECTION)
        cleanup = threading.Thread(
            target=handler._process_pending_changes, kwargs={"final_cleanup": True}
        )
        cleanup.start()
        cleanup.join(timeout=_STOP_BOUND_SECONDS)

        assert not cleanup.is_alive()
    finally:
        release.set()
        holder.join(timeout=_STOP_BOUND_SECONDS)


def test_empty_cycle_keeps_interrupted_while_retries_are_owed(tmp_path: Path) -> None:
    owed = "src/owed.py"
    (tmp_path / "src").mkdir()
    (tmp_path / owed).write_text("x = 1\n")
    handler = _bare_handler(tmp_path)
    clock = _Clock(_START)
    handler._clock = clock
    handler._pending_unhides[owed] = (_FEATURE, COLLECTION)
    handler._unhide_backoff.record_failure(owed, _START)
    handler.watch_metadata.mark_processing_interrupted("un-hide owed")

    handler._process_pending_changes(final_cleanup=True)

    assert handler.watch_metadata.processing_interrupted
    assert owed in handler._pending_unhides
