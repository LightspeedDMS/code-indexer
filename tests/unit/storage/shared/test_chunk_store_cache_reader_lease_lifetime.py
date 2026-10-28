"""Reader-lease lifetime of ChunkStoreThreadCache.

A snapshot reader lease must live exactly as long as a reader can still need
the snapshot:

- variant A: the thread that opened the store exits -> its store is
  collected -> its lease is released (no orphan renewed forever, no id()
  reuse collision);
- variant B: a pool thread that went idle on a SUPERSEDED (invalidated)
  snapshot does not keep renewing its lease, so cleanup can proceed; its
  idle handle is closed together with the lease (#2223);
- a reader of a CURRENT snapshot keeps its lease while idle.

Real threads, real ChunkStore, real lease files. Only the lease TTL constant
is shortened, so the renewal loop ticks in well under a second.
"""

from __future__ import annotations

import gc
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, List, Tuple

import pytest

from code_indexer.global_repos.snapshot_reader_lease import (
    ensure_primary_lease_directory,
    snapshot_has_live_reader,
)
from code_indexer.storage.shared import chunk_store_cache
from code_indexer.storage.shared.chunk_store_cache import ChunkStoreThreadCache
from code_indexer.storage.sqlite_chunk_store import ChunkStore

_SHORT_TTL_SECONDS = 0.6
_SEVERAL_RENEWAL_TICKS_SECONDS = 1.5
_JOIN_TIMEOUT_SECONDS = 30
_SHORT_LIVED_POOL_ROUNDS = 10
_SHORT_LIVED_POOL_WORKERS = 2


@dataclass
class World:
    cache: ChunkStoreThreadCache
    golden: Path
    lease_root: Path
    lease_dir: Path

    def has_live_reader(self, snapshot_root: str) -> bool:
        return snapshot_has_live_reader(snapshot_root, lease_root=self.lease_root)

    def lease_files(self) -> List[Path]:
        return sorted(self.lease_dir.glob("*.json"))

    def snapshot(self, version: str) -> Tuple[str, Path, str]:
        root = self.golden / ".versioned" / "example-repo" / version
        collection = root / ".code-indexer" / "index" / "voyage-code-3"
        collection.mkdir(parents=True)
        db_path = collection / "chunks.db"
        ChunkStore(db_path).close()
        return str(root), db_path, str(collection)


def _is_snapshot_root(path: str) -> bool:
    return PurePosixPath(path).parent.parent.name == ".versioned"


def _run_on(pool: ThreadPoolExecutor, fn: Callable[..., Any], *args: Any) -> Any:
    return pool.submit(fn, *args).result(_JOIN_TIMEOUT_SECONDS)


def _wait(seconds: float) -> None:
    threading.Event().wait(seconds)


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> Iterator[World]:
    monkeypatch.setattr(
        chunk_store_cache, "SNAPSHOT_READER_LEASE_TTL_SECONDS", _SHORT_TTL_SECONDS
    )
    golden = tmp_path / "golden-repos"
    lease_root = golden / "cidx-meta"
    lease_root.mkdir(parents=True)
    lease_dir = ensure_primary_lease_directory(lease_root)
    cache = ChunkStoreThreadCache(
        lease_root=lease_root, is_versioned_snapshot=_is_snapshot_root
    )
    yield World(cache, golden, lease_root, lease_dir)
    cache._lease_stop_event.set()


class TestVariantAThreadExit:
    def test_lease_is_released_when_the_opening_thread_exits(self, world: World):
        root, db_path, collection = world.snapshot("v_1")
        thread = threading.Thread(
            target=world.cache.get_or_open,
            args=(db_path, collection),
            kwargs={"read_only": True},
        )
        thread.start()
        thread.join(_JOIN_TIMEOUT_SECONDS)
        gc.collect()

        assert world.lease_files() == []
        assert world.has_live_reader(root) is False

    def test_many_short_lived_threads_leave_no_live_lease(self, world: World):
        root, db_path, collection = world.snapshot("v_1")
        for _ in range(_SHORT_LIVED_POOL_ROUNDS):
            with ThreadPoolExecutor(max_workers=_SHORT_LIVED_POOL_WORKERS) as pool:
                for _worker in range(_SHORT_LIVED_POOL_WORKERS):
                    pool.submit(
                        world.cache.get_or_open, db_path, collection, read_only=True
                    ).result(_JOIN_TIMEOUT_SECONDS)
        gc.collect()
        _wait(_SEVERAL_RENEWAL_TICKS_SECONDS)

        assert world.lease_files() == []
        assert world.has_live_reader(root) is False


class TestVariantBIdleThread:
    def test_idle_thread_stops_pinning_an_invalidated_snapshot(self, world: World):
        root, db_path, collection = world.snapshot("v_1")
        with ThreadPoolExecutor(max_workers=1) as idle_pool:
            store = _run_on(idle_pool, world.cache.get_or_open, db_path, collection)
            assert world.has_live_reader(root)

            world.cache.invalidate_prefix(root)
            _wait(_SEVERAL_RENEWAL_TICKS_SECONDS)

            assert world.has_live_reader(root) is False
            assert world.cache._is_closed(store)
            assert (
                _run_on(idle_pool, world.cache.run_reading, store, ChunkStore.count)
                == 0
            )

    def test_idle_reader_of_a_current_snapshot_keeps_its_lease(self, world: World):
        root, db_path, collection = world.snapshot("v_1")
        with ThreadPoolExecutor(max_workers=1) as idle_pool:
            _run_on(idle_pool, world.cache.get_or_open, db_path, collection)
            _wait(_SEVERAL_RENEWAL_TICKS_SECONDS)
            assert world.has_live_reader(root) is True

    def test_superseded_snapshot_deletes_under_the_idle_handle_and_converges(
        self, world: World
    ):
        """Local filesystem: unlinking files another thread holds open is safe
        for that reader; the owner drops the stale handle on its next call."""
        old_root, old_db, old_collection = world.snapshot("v_1")
        _new_root, new_db, new_collection = world.snapshot("v_2")
        with ThreadPoolExecutor(max_workers=1) as idle_pool:
            _run_on(idle_pool, world.cache.get_or_open, old_db, old_collection)
            world.cache.invalidate_prefix(old_root)
            _wait(_SEVERAL_RENEWAL_TICKS_SECONDS)
            assert world.has_live_reader(old_root) is False

            shutil.rmtree(old_root)
            assert not Path(old_root).exists()

            _run_on(idle_pool, world.cache.get_or_open, new_db, new_collection)
            cached_paths = _run_on(
                idle_pool, lambda: [key[0] for key in world.cache._entries()]
            )
            assert str(old_db) not in cached_paths
        gc.collect()
        assert world.lease_files() == []


class TestActiveReaderKeepsItsLease:
    def test_active_read_keeps_the_lease_until_the_store_goes_idle(self, world: World):
        """A query still reading a superseded snapshot must keep its lease
        (another node's cleanup would otherwise delete it mid-read)."""
        root, db_path, collection = world.snapshot("v_1")
        reading, finish = threading.Event(), threading.Event()

        def long_read(store: ChunkStore) -> int:
            reading.set()
            assert finish.wait(_JOIN_TIMEOUT_SECONDS)
            return store.count()

        with ThreadPoolExecutor(max_workers=1) as pool:
            store = _run_on(pool, world.cache.get_or_open, db_path, collection)
            pending = pool.submit(world.cache.run_reading, store, long_read)
            assert reading.wait(_JOIN_TIMEOUT_SECONDS)
            world.cache.invalidate_prefix(root)

            _wait(_SEVERAL_RENEWAL_TICKS_SECONDS)
            assert world.has_live_reader(root) is True

            finish.set()
            assert pending.result(_JOIN_TIMEOUT_SECONDS) == 0
            _wait(_SEVERAL_RENEWAL_TICKS_SECONDS)
            assert world.has_live_reader(root) is False
