"""Daemon-wide chunk-mutation serialization (Codex Finding, Story #1488).

The ThreadedServer shares ONE CIDXDaemonService instance across connection
threads, and the process-level `.index-mutation.lock` is held by the daemon
process itself (acquired once at startup) -- so it does NOT serialize two RPC
calls WITHIN the same daemon process. Without an in-process mutation mutex,
two simultaneous daemon-delegated `cidx index` calls -- or a blocking index
concurrent with a background index -- mutate the SAME collection concurrently,
racing writes / corrupting the index.

These tests prove mutual exclusion of the chunk-mutation region deterministically
(no wall-clock ordering sleeps -- only bounded synchronization waits that return
immediately once a second thread genuinely arrives) using a concurrency recorder
that records the maximum number of threads simultaneously inside the mutation.
"""

import threading
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.slow


# Bounded synchronization wait. This is NOT an ordering sleep: it returns
# IMMEDIATELY the moment a second thread genuinely enters the region (the event
# is set), so the RED path completes instantly. It only elapses in full when the
# region is correctly serialized (GREEN) and a second thread can therefore never
# join -- exactly the condition we are proving. Sized generously so the slower
# background-indexing path (which spawns a thread and runs a setup preamble)
# reliably reaches the region within the window while a partner still holds it.
_SYNC_TIMEOUT_SECONDS = 2.0


class _ConcurrencyRecorder:
    """Records the maximum number of threads simultaneously inside a region."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.total_entries = 0
        self._two_in_flight = threading.Event()

    def enter_region(self) -> None:
        with self._lock:
            self.active += 1
            self.total_entries += 1
            if self.active > self.max_active:
                self.max_active = self.active
            if self.active >= 2:
                # A second thread genuinely overlapped: release both instantly.
                self._two_in_flight.set()
        # Bounded wait: instant on real overlap, full timeout only when serialized.
        self._two_in_flight.wait(timeout=_SYNC_TIMEOUT_SECONDS)
        with self._lock:
            self.active -= 1


class _FakeStats:
    files_processed = 1
    chunks_created = 1
    failed_files = 0
    duration = 0.0
    cancelled = False


class _FakeConfig:
    def __init__(self, project_path: Path) -> None:
        # The background indexing path logs config.codebase_dir.
        self.codebase_dir = project_path


class _FakeConfigManager:
    def __init__(self, project_path: Path) -> None:
        self.project_path = project_path
        self.config_path = project_path / ".code-indexer" / "config.json"

    @classmethod
    def create_with_backtrack(cls, project_root: Path) -> "_FakeConfigManager":
        return cls(Path(project_root))

    def get_config(self) -> Any:
        return _FakeConfig(self.project_path)


class _FakeBackend:
    def get_vector_store_client(self) -> Any:
        return object()


def _make_fake_smart_indexer(recorder: _ConcurrencyRecorder):
    class _FakeSmartIndexer:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def smart_index(self, *args: Any, **kwargs: Any) -> _FakeStats:
            recorder.enter_region()
            return _FakeStats()

    return _FakeSmartIndexer


def _patch_semantic_indexing(stack: ExitStack, recorder: _ConcurrencyRecorder) -> None:
    """Patch every heavy dependency of the semantic indexing path.

    The daemon imports these lazily inside the method body, so patching them at
    their source modules is what takes effect at call time.
    """
    fake_indexer_cls = _make_fake_smart_indexer(recorder)
    stack.enter_context(
        patch(
            "code_indexer.services.smart_indexer.SmartIndexer",
            fake_indexer_cls,
        )
    )
    stack.enter_context(
        patch(
            "code_indexer.config.ConfigManager",
            _FakeConfigManager,
        )
    )
    stack.enter_context(
        patch(
            "code_indexer.backends.backend_factory.BackendFactory.create",
            lambda *a, **k: _FakeBackend(),
        )
    )
    stack.enter_context(
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
            lambda *a, **k: object(),
        )
    )


def _make_fake_vector_store(recorder: _ConcurrencyRecorder):
    """Fake FilesystemVectorStore whose mutating calls enter the shared region.

    `list_collections` is intentionally NOT instrumented (it is a read, not a
    mutation) -- only `clear_collection` (exposed_clean) and `delete_collection`
    (exposed_clean_data) enter the recorder, mirroring the real mutating calls.
    """

    class _FakeVectorStore:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def list_collections(self) -> list:
            return ["collection1"]

        def clear_collection(
            self, collection_name: str, remove_projection_matrix: bool = False
        ) -> bool:
            recorder.enter_region()
            return True

        def delete_collection(self, collection_name: str) -> bool:
            recorder.enter_region()
            return True

    return _FakeVectorStore


def _patch_clean_vector_store(stack: ExitStack, recorder: _ConcurrencyRecorder) -> None:
    """Patch FilesystemVectorStore at its source module for exposed_clean(_data)."""
    fake_cls = _make_fake_vector_store(recorder)
    stack.enter_context(
        patch(
            "code_indexer.storage.filesystem_vector_store.FilesystemVectorStore",
            fake_cls,
        )
    )


@pytest.fixture
def service():
    from code_indexer.daemon.service import CIDXDaemonService

    svc = CIDXDaemonService()
    yield svc
    svc.eviction_thread.stop()
    svc.eviction_thread.join(timeout=1)


@pytest.fixture
def project_path(tmp_path):
    root = tmp_path / "proj"
    (root / ".code-indexer" / "index").mkdir(parents=True)
    return root


def test_two_blocking_indexes_do_not_overlap(service, project_path):
    """Two concurrent exposed_index_blocking calls must NOT mutate concurrently."""
    recorder = _ConcurrencyRecorder()
    results: list[dict] = []
    results_lock = threading.Lock()

    def run():
        res = service.exposed_index_blocking(str(project_path))
        with results_lock:
            results.append(res)

    # Apply the patches ONCE in the main thread: unittest.mock.patch mutates a
    # shared module global and is NOT thread-safe, so patching concurrently from
    # each worker races (a finishing thread would restore the real SmartIndexer
    # out from under a still-running one). Both workers share these patches.
    with ExitStack() as stack:
        _patch_semantic_indexing(stack, recorder)
        t1 = threading.Thread(target=run)
        t2 = threading.Thread(target=run)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

    assert not t1.is_alive() and not t2.is_alive(), "indexing threads hung"
    # Both must have actually reached the mutation (not errored out of it).
    assert len(results) == 2
    assert all(r.get("status") == "completed" for r in results), results
    # Both threads genuinely reached the chunk-mutation region.
    assert recorder.total_entries == 2, recorder.total_entries
    # The core assertion: never two threads inside the mutation simultaneously.
    assert recorder.max_active == 1, (
        f"chunk mutation was NOT serialized: max_active={recorder.max_active}"
    )


def test_blocking_and_background_index_do_not_overlap(service, project_path):
    """A blocking index and a background index must NOT mutate concurrently."""
    recorder = _ConcurrencyRecorder()
    blocking_result: dict = {}

    with ExitStack() as stack:
        _patch_semantic_indexing(stack, recorder)

        def run_blocking():
            blocking_result.update(service.exposed_index_blocking(str(project_path)))

        t_block = threading.Thread(target=run_blocking)
        # Start the background indexing path (spawns its own daemon thread).
        start = service.exposed_index(str(project_path))
        assert start["status"] == "started", start
        t_block.start()
        t_block.join(timeout=30)

        # Wait for the background indexing thread to finish (bounded).
        bg_thread = service.indexing_thread
        if bg_thread is not None:
            bg_thread.join(timeout=30)

    assert not t_block.is_alive(), "blocking index thread hung"
    assert blocking_result.get("status") == "completed", blocking_result
    # Both the blocking AND background paths genuinely reached the mutation.
    assert recorder.total_entries == 2, recorder.total_entries
    assert recorder.max_active == 1, (
        f"blocking+background mutation was NOT serialized: "
        f"max_active={recorder.max_active}"
    )


def test_clean_data_and_blocking_index_do_not_overlap(service, project_path):
    """A daemon-delegated clean_data must NOT mutate concurrently with an index.

    Finding 1 (16th Codex review, Story #1488): exposed_clean_data deletes a
    collection's chunk storage but is not wrapped by self.mutation_lock, so it
    can race a concurrent exposed_index_blocking mutating the same collection.
    """
    recorder = _ConcurrencyRecorder()
    blocking_result: dict = {}
    clean_result: dict = {}

    with ExitStack() as stack:
        _patch_semantic_indexing(stack, recorder)
        _patch_clean_vector_store(stack, recorder)

        def run_blocking():
            blocking_result.update(service.exposed_index_blocking(str(project_path)))

        def run_clean_data():
            clean_result.update(service.exposed_clean_data(str(project_path)))

        t_block = threading.Thread(target=run_blocking)
        t_clean = threading.Thread(target=run_clean_data)
        t_block.start()
        t_clean.start()
        t_block.join(timeout=30)
        t_clean.join(timeout=30)

    assert not t_block.is_alive() and not t_clean.is_alive(), "threads hung"
    assert blocking_result.get("status") == "completed", blocking_result
    assert clean_result.get("status") == "success", clean_result
    # Both the index AND the clean_data path genuinely reached a mutation.
    assert recorder.total_entries == 2, recorder.total_entries
    assert recorder.max_active == 1, (
        f"clean_data+index mutation was NOT serialized: "
        f"max_active={recorder.max_active}"
    )


def test_clean_and_blocking_index_do_not_overlap(service, project_path):
    """A daemon-delegated clean must NOT mutate concurrently with an index.

    Finding 1 (16th Codex review, Story #1488): exposed_clean clears a
    collection's chunk storage but is not wrapped by self.mutation_lock, so it
    can race a concurrent exposed_index_blocking mutating the same collection.
    """
    recorder = _ConcurrencyRecorder()
    blocking_result: dict = {}
    clean_result: dict = {}

    with ExitStack() as stack:
        _patch_semantic_indexing(stack, recorder)
        _patch_clean_vector_store(stack, recorder)

        def run_blocking():
            blocking_result.update(service.exposed_index_blocking(str(project_path)))

        def run_clean():
            clean_result.update(service.exposed_clean(str(project_path)))

        t_block = threading.Thread(target=run_blocking)
        t_clean = threading.Thread(target=run_clean)
        t_block.start()
        t_clean.start()
        t_block.join(timeout=30)
        t_clean.join(timeout=30)

    assert not t_block.is_alive() and not t_clean.is_alive(), "threads hung"
    assert blocking_result.get("status") == "completed", blocking_result
    assert clean_result.get("status") == "success", clean_result
    # Both the index AND the clean path genuinely reached a mutation.
    assert recorder.total_entries == 2, recorder.total_entries
    assert recorder.max_active == 1, (
        f"clean+index mutation was NOT serialized: max_active={recorder.max_active}"
    )
