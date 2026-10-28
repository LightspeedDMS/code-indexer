"""Process-wide lifetime of ChunkStoreThreadCache handles (#2223, #2218).

Real threads, real ChunkStore files, real lease files, descriptors read from
/proc/self/fd. Only the lease TTL constant is shortened.
"""

from __future__ import annotations

import gc
import logging
import os
import queue
import shutil
import sqlite3
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
from code_indexer.storage.shared import chunk_store_cache, chunk_store_closer
from code_indexer.storage.shared.chunk_store_cache import ChunkStoreThreadCache
from code_indexer.storage.sqlite_chunk_store import ChunkStore, ChunkStoreError

_SHORT_TTL_SECONDS = 0.6
_SEVERAL_RENEWAL_TICKS_SECONDS = 1.5
_JOIN_TIMEOUT_SECONDS = 30


def _fd_targets_under(root: Path) -> List[str]:
    targets = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith(str(root)):
            targets.append(target)
    return targets


def _make_store(collection: Path) -> Tuple[Path, str]:
    collection.mkdir(parents=True)
    db_path = collection / "chunks.db"
    ChunkStore(db_path).close()
    return db_path, str(collection)


def _is_snapshot_root(path: str) -> bool:
    return PurePosixPath(path).parent.parent.name == ".versioned"


def _run_on(pool: ThreadPoolExecutor, fn: Callable[..., Any], *args: Any) -> Any:
    return pool.submit(fn, *args).result(_JOIN_TIMEOUT_SECONDS)


def _open_reader(cache: ChunkStoreThreadCache, db: Path, collection: str) -> ChunkStore:
    return cache.get_or_open(db, collection, read_only=True)


def _wait(seconds: float) -> None:
    threading.Event().wait(seconds)


_POLL_INTERVAL_SECONDS = 0.05
_CLOSER_SETTLE_SECONDS = 0.3


def _eventually(predicate: Callable[[], bool]) -> bool:
    """Poll ``predicate`` until true or ``_JOIN_TIMEOUT_SECONDS`` elapse."""
    for _ in range(int(_JOIN_TIMEOUT_SECONDS / _POLL_INTERVAL_SECONDS)):
        if predicate():
            return True
        _wait(_POLL_INTERVAL_SECONDS)
    return predicate()


@dataclass
class World:
    cache: ChunkStoreThreadCache
    golden: Path
    lease_root: Path

    def snapshot(self, version: str) -> Tuple[str, Path, str]:
        root = self.golden / ".versioned" / "example-repo" / version
        db_path, collection = _make_store(
            root / ".code-indexer" / "index" / "voyage-code-3"
        )
        return str(root), db_path, collection

    def has_live_reader(self, snapshot_root: str) -> bool:
        return snapshot_has_live_reader(snapshot_root, lease_root=self.lease_root)


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> Iterator[World]:
    monkeypatch.setattr(
        chunk_store_cache, "SNAPSHOT_READER_LEASE_TTL_SECONDS", _SHORT_TTL_SECONDS
    )
    golden = tmp_path / "golden-repos"
    lease_root = golden / "cidx-meta"
    lease_root.mkdir(parents=True)
    ensure_primary_lease_directory(lease_root)
    cache = ChunkStoreThreadCache(
        lease_root=lease_root, is_versioned_snapshot=_is_snapshot_root
    )
    yield World(cache, golden, lease_root)
    cache._lease_stop_event.set()


def _hold_open_on_threads(
    cache: ChunkStoreThreadCache, stores: List[Tuple[Path, str]], release: Any
) -> List[threading.Thread]:
    opened = threading.Barrier(len(stores) + 1)

    def open_and_idle(db: Path, collection: str) -> None:
        _open_reader(cache, db, collection)
        opened.wait(_JOIN_TIMEOUT_SECONDS)
        release.wait(_JOIN_TIMEOUT_SECONDS)

    threads = [
        threading.Thread(target=open_and_idle, args=store, daemon=True)
        for store in stores
    ]
    for thread in threads:
        thread.start()
    opened.wait(_JOIN_TIMEOUT_SECONDS)
    return threads


class TestProcessWideCap:
    def test_default_process_cap_is_4096(self):
        assert ChunkStoreThreadCache()._max_open_stores == 4096

    def test_open_handles_across_threads_never_exceed_the_process_cap(
        self, tmp_path: Path
    ):
        cache = ChunkStoreThreadCache(max_open_stores=4)
        stores = [_make_store(tmp_path / f"repo-{i}" / "index") for i in range(8)]
        release = threading.Event()
        threads = _hold_open_on_threads(cache, stores, release)
        try:
            assert _eventually(lambda: len(_fd_targets_under(tmp_path)) <= 4)
        finally:
            release.set()
            for thread in threads:
                thread.join(_JOIN_TIMEOUT_SECONDS)

    def test_owner_reads_a_store_another_thread_evicted(self, tmp_path: Path):
        cache = ChunkStoreThreadCache(max_open_stores=1)
        db_a, coll_a = _make_store(tmp_path / "repo-a" / "index")
        db_b, coll_b = _make_store(tmp_path / "repo-b" / "index")
        with (
            ThreadPoolExecutor(max_workers=1) as owner,
            ThreadPoolExecutor(max_workers=1) as other,
        ):
            store = _run_on(owner, _open_reader, cache, db_a, coll_a)
            _run_on(other, _open_reader, cache, db_b, coll_b)
            assert _eventually(lambda: not _fd_targets_under(tmp_path / "repo-a"))

            count = _run_on(owner, cache.run_reading, store, ChunkStore.count)
            assert count == 0

    def test_store_being_read_is_never_closed_and_cap_converges_after(
        self, tmp_path: Path
    ):
        cache = ChunkStoreThreadCache(max_open_stores=1)
        db_a, coll_a = _make_store(tmp_path / "repo-a" / "index")
        db_b, coll_b = _make_store(tmp_path / "repo-b" / "index")
        reading, finish = threading.Event(), threading.Event()

        def long_read(store: ChunkStore) -> int:
            reading.set()
            assert finish.wait(_JOIN_TIMEOUT_SECONDS)
            return store.count()

        with (
            ThreadPoolExecutor(max_workers=1) as owner,
            ThreadPoolExecutor(max_workers=1) as other,
        ):
            store = _run_on(owner, _open_reader, cache, db_a, coll_a)
            pending = owner.submit(cache.run_reading, store, long_read)
            assert reading.wait(_JOIN_TIMEOUT_SECONDS)

            _run_on(other, _open_reader, cache, db_b, coll_b)
            _wait(_CLOSER_SETTLE_SECONDS)
            assert len(_fd_targets_under(tmp_path / "repo-a")) == 1

            finish.set()
            assert pending.result(_JOIN_TIMEOUT_SECONDS) == 0
            assert _eventually(lambda: len(_fd_targets_under(tmp_path)) <= 1)


class TestErrorPaths:
    def test_cap_below_one_is_rejected(self):
        with pytest.raises(ValueError):
            ChunkStoreThreadCache(max_open_stores=0)

    def test_reading_a_closed_store_without_reopen_recipe_raises(self, tmp_path: Path):
        cache = ChunkStoreThreadCache()
        db, _collection = _make_store(tmp_path / "repo" / "index")
        foreign = ChunkStore(db, read_only=True)
        cache._close_store(foreign)
        with pytest.raises(ChunkStoreError, match="no reopen recipe"):
            cache.run_reading(foreign, ChunkStore.count)

    def test_reopen_attempts_are_bounded(self, tmp_path: Path, monkeypatch):
        cache = ChunkStoreThreadCache()
        db, collection = _make_store(tmp_path / "repo" / "index")
        store = _open_reader(cache, db, collection)
        cache._close_store(store)
        reopens: List[Any] = []

        def always_closed(*args: Any, **kwargs: Any) -> ChunkStore:
            reopens.append(args)
            return store

        monkeypatch.setattr(cache, "get_or_open", always_closed)
        with pytest.raises(ChunkStoreError, match="after 3 reopens"):
            cache.run_reading(store, ChunkStore.count)
        assert len(reopens) == 3

    def test_dead_thread_store_leaves_the_registry(self, tmp_path: Path):
        cache = ChunkStoreThreadCache()
        db, collection = _make_store(tmp_path / "repo" / "index")
        thread = threading.Thread(target=_open_reader, args=(cache, db, collection))
        thread.start()
        thread.join(_JOIN_TIMEOUT_SECONDS)
        gc.collect()
        assert len(cache._open_stores) == 0
        assert _fd_targets_under(tmp_path) == []


class TestSupersededSnapshotHandleIsClosedWithoutItsOwner:
    def test_idle_handle_on_superseded_snapshot_is_closed_without_its_owner(
        self, world: World
    ):
        old_root, old_db, old_collection = world.snapshot("v_1")
        with ThreadPoolExecutor(max_workers=1) as idle_pool:
            _run_on(idle_pool, _open_reader, world.cache, old_db, old_collection)
            assert _fd_targets_under(Path(old_root)) != []

            world.cache.invalidate_prefix(old_root)
            _wait(_SEVERAL_RENEWAL_TICKS_SECONDS)
            assert world.has_live_reader(old_root) is False

            # The owner thread stays idle in its pool: nothing it does may be
            # needed to drop the handle.
            assert _fd_targets_under(Path(old_root)) == []
            shutil.rmtree(old_root)
            assert _fd_targets_under(Path(old_root)) == []


class _ControlledCloseStore(ChunkStore):
    """A real ChunkStore whose close() first runs an injected hook."""

    def __init__(self, *args: Any, before_close: Callable[[], None], **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._before_close = before_close

    def close(self) -> None:
        self._before_close()
        super().close()


def _install_close_behaviour(
    monkeypatch, target_db: Path, before_close: Callable[[], None]
) -> None:
    real_open = chunk_store_cache.open_chunk_store_for_path

    def opener(db_path: Any, collection_path: str, **kwargs: Any) -> ChunkStore:
        if Path(db_path) != target_db:
            return real_open(db_path, collection_path, **kwargs)
        return _ControlledCloseStore(
            db_path,
            read_only=True,
            check_same_thread=kwargs["check_same_thread"],
            before_close=before_close,
        )

    monkeypatch.setattr(chunk_store_cache, "open_chunk_store_for_path", opener)


def _long_reader(reading: threading.Event, finish: threading.Event):
    def long_read(store: ChunkStore) -> int:
        reading.set()
        assert finish.wait(_JOIN_TIMEOUT_SECONDS)
        return store.count()

    return long_read


class TestCloseNeverBlocksRenewal:
    def test_blocked_close_does_not_delay_renewal_of_an_active_reader(
        self, world: World, monkeypatch
    ):
        old_root, old_db, old_coll = world.snapshot("v_1")
        cur_root, cur_db, cur_coll = world.snapshot("v_2")
        close_started, unblock = threading.Event(), threading.Event()

        def blocking_close() -> None:
            close_started.set()
            assert unblock.wait(_JOIN_TIMEOUT_SECONDS)

        _install_close_behaviour(monkeypatch, old_db, blocking_close)
        reading, finish = threading.Event(), threading.Event()
        with (
            ThreadPoolExecutor(max_workers=1) as idle,
            ThreadPoolExecutor(max_workers=1) as reader,
        ):
            _run_on(idle, _open_reader, world.cache, old_db, old_coll)
            current = _run_on(reader, _open_reader, world.cache, cur_db, cur_coll)
            pending = reader.submit(
                world.cache.run_reading, current, _long_reader(reading, finish)
            )
            assert reading.wait(_JOIN_TIMEOUT_SECONDS)
            world.cache.invalidate_prefix(old_root)
            try:
                assert close_started.wait(_JOIN_TIMEOUT_SECONDS)
                _wait(_SHORT_TTL_SECONDS * 3)
                assert world.has_live_reader(cur_root) is True
                assert world.has_live_reader(old_root) is True
            finally:
                unblock.set()
                finish.set()
            assert pending.result(_JOIN_TIMEOUT_SECONDS) == 0
            _wait(_SEVERAL_RENEWAL_TICKS_SECONDS)
            assert world.has_live_reader(old_root) is False
            assert _fd_targets_under(Path(old_root)) == []


class TestCloserLifecycle:
    def test_closer_stops_with_the_renewal_thread(self, world: World):
        root, db, coll = world.snapshot("v_1")
        store = _open_reader(world.cache, db, coll)
        world.cache.invalidate_prefix(root)
        assert _eventually(lambda: not _fd_targets_under(Path(root)))
        assert world.cache._is_closed(store)
        renewal, closer = world.cache._lease_thread, world.cache._closer._thread
        assert renewal is not None and closer is not None
        assert renewal.is_alive() and closer.is_alive()

        world.cache._lease_stop_event.set()
        renewal.join(_JOIN_TIMEOUT_SECONDS)
        closer.join(_JOIN_TIMEOUT_SECONDS)
        assert not renewal.is_alive() and not closer.is_alive()

    def test_stop_during_a_blocked_close_abandons_with_one_warning(
        self, world: World, monkeypatch, caplog
    ):
        caplog.set_level(logging.WARNING, logger=chunk_store_closer.__name__)
        monkeypatch.setattr(chunk_store_closer, "_CLOSER_STOP_WAIT_SECONDS", 0.3)
        old_root, old_db, old_coll = world.snapshot("v_1")
        close_started, unblock = threading.Event(), threading.Event()

        def blocking_close() -> None:
            close_started.set()
            assert unblock.wait(_JOIN_TIMEOUT_SECONDS)

        _install_close_behaviour(monkeypatch, old_db, blocking_close)
        with ThreadPoolExecutor(max_workers=1) as idle:
            _run_on(idle, _open_reader, world.cache, old_db, old_coll)
            world.cache.invalidate_prefix(old_root)
            try:
                assert close_started.wait(_JOIN_TIMEOUT_SECONDS)
                renewal = world.cache._lease_thread
                assert renewal is not None
                world.cache._lease_stop_event.set()
                renewal.join(_JOIN_TIMEOUT_SECONDS)
                assert not renewal.is_alive()
                abandoned = [
                    r for r in caplog.records if "abandoning" in r.getMessage()
                ]
                assert len(abandoned) == 1
            finally:
                unblock.set()

    def test_full_close_queue_skips_and_retries_next_tick(
        self, world: World, monkeypatch
    ):
        world.cache._closer._queue = queue.Queue(maxsize=1)
        snaps = [world.snapshot(f"v_{i}") for i in range(3)]
        close_started, unblock = threading.Event(), threading.Event()

        def blocking_close() -> None:
            close_started.set()
            assert unblock.wait(_JOIN_TIMEOUT_SECONDS)

        _install_close_behaviour(monkeypatch, snaps[0][1], blocking_close)
        with ThreadPoolExecutor(max_workers=1) as idle:
            stores = [
                _run_on(idle, _open_reader, world.cache, db, coll)
                for _root, db, coll in snaps
            ]
            for root, _db, _coll in snaps:
                world.cache.invalidate_prefix(root)
            try:
                assert close_started.wait(_JOIN_TIMEOUT_SECONDS)
                _wait(_SEVERAL_RENEWAL_TICKS_SECONDS)
                assert not world.cache._is_closed(stores[2])
                assert all(world.has_live_reader(root) for root, _d, _c in snaps)
            finally:
                unblock.set()
            _wait(_SEVERAL_RENEWAL_TICKS_SECONDS * 2)
            assert all(world.cache._is_closed(store) for store in stores)
            assert not any(world.has_live_reader(root) for root, _d, _c in snaps)


_FAST_RETRY_BASE_SECONDS = 0.01
_FAILURES_PAST_OLD_GIVE_UP = 5


def _failing_close(failures: dict) -> Callable[[], None]:
    def failing_close() -> None:
        failures["attempts"] += 1
        if failures["left"] > 0:
            failures["left"] -= 1
            raise sqlite3.OperationalError("disk I/O error")

    return failing_close


class TestFailedCloseKeepsTheLease:
    def _supersede_idle_store(self, world: World, monkeypatch, failures: dict):
        monkeypatch.setattr(
            chunk_store_closer, "_RETRY_BASE_SECONDS", _FAST_RETRY_BASE_SECONDS
        )
        old_root, old_db, old_coll = world.snapshot("v_1")
        _install_close_behaviour(monkeypatch, old_db, _failing_close(failures))
        idle = ThreadPoolExecutor(max_workers=1)
        _run_on(idle, _open_reader, world.cache, old_db, old_coll)
        world.cache.invalidate_prefix(old_root)
        return old_root, idle

    def test_close_that_always_fails_keeps_lease_and_errors_once(
        self, world: World, monkeypatch, caplog
    ):
        caplog.set_level(logging.WARNING, logger=chunk_store_closer.__name__)
        failures = {"left": 10**6, "attempts": 0}
        old_root, idle = self._supersede_idle_store(world, monkeypatch, failures)
        try:
            assert _eventually(lambda: failures["attempts"] > 2 * 3)
            assert world.has_live_reader(old_root) is True
            assert _fd_targets_under(Path(old_root)) != []
            closer_records = [
                r for r in caplog.records if r.name == chunk_store_closer.__name__
            ]
            warnings = [r for r in closer_records if r.levelno == logging.WARNING]
            errors = [r for r in closer_records if r.levelno == logging.ERROR]
            assert len(warnings) == 1
            assert "OperationalError" in warnings[0].getMessage()
            assert len(errors) == 1
            assert "OperationalError" in errors[0].getMessage()
        finally:
            idle.shutdown()

    def test_close_that_fails_repeatedly_then_succeeds_releases(
        self, world: World, monkeypatch
    ):
        failures = {"left": _FAILURES_PAST_OLD_GIVE_UP, "attempts": 0}
        old_root, idle = self._supersede_idle_store(world, monkeypatch, failures)
        try:
            assert _eventually(lambda: not world.has_live_reader(old_root))
            assert _fd_targets_under(Path(old_root)) == []
            assert failures["attempts"] == _FAILURES_PAST_OLD_GIVE_UP + 1
        finally:
            idle.shutdown()

    def test_close_retries_converge_without_a_lease_renewal_thread(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(
            chunk_store_closer, "_RETRY_BASE_SECONDS", _FAST_RETRY_BASE_SECONDS
        )
        cache = ChunkStoreThreadCache(max_open_stores=1)
        db_a, coll_a = _make_store(tmp_path / "repo-a" / "index")
        db_b, coll_b = _make_store(tmp_path / "repo-b" / "index")
        failures = {"left": _FAILURES_PAST_OLD_GIVE_UP, "attempts": 0}
        _install_close_behaviour(monkeypatch, db_a, _failing_close(failures))
        with (
            ThreadPoolExecutor(max_workers=1) as owner,
            ThreadPoolExecutor(max_workers=1) as other,
        ):
            _run_on(owner, _open_reader, cache, db_a, coll_a)
            _run_on(other, _open_reader, cache, db_b, coll_b)
            assert _eventually(lambda: not _fd_targets_under(tmp_path / "repo-a"))
            assert _eventually(lambda: len(cache._open_stores) == 1)
            assert failures["attempts"] == _FAILURES_PAST_OLD_GIVE_UP + 1
            assert cache._lease_thread is None


class TestCapCountsFailingCloses:
    def test_failing_close_keeps_count_over_cap_and_warns_once_per_interval(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        interval = 0.5
        caplog.set_level(logging.WARNING, logger=chunk_store_cache.__name__)
        monkeypatch.setattr(
            chunk_store_closer, "_RETRY_BASE_SECONDS", _FAST_RETRY_BASE_SECONDS
        )
        monkeypatch.setattr(
            chunk_store_cache, "_CAP_WARNING_INTERVAL_SECONDS", interval
        )
        cache = ChunkStoreThreadCache(max_open_stores=1)
        repos = [_make_store(tmp_path / f"repo-{n}" / "index") for n in "abcde"]
        failures = {"left": 10**6, "attempts": 0}
        _install_close_behaviour(monkeypatch, repos[0][0], _failing_close(failures))

        def cap_warnings() -> int:
            return sum(
                "exceed the process cap" in r.getMessage() for r in caplog.records
            )

        with (
            ThreadPoolExecutor(max_workers=1) as owner,
            ThreadPoolExecutor(max_workers=1) as other,
        ):
            _run_on(owner, _open_reader, cache, *repos[0])
            for db, coll in repos[1:4]:
                assert _run_on(other, _open_reader, cache, db, coll) is not None
                assert _eventually(lambda: failures["attempts"] > 0)
            assert len(cache._open_stores) >= 2
            assert len(_fd_targets_under(tmp_path)) >= 2
            assert cap_warnings() == 1

            _wait(interval * 2)
            assert _run_on(other, _open_reader, cache, *repos[4]) is not None
            assert cap_warnings() == 2

    def test_healthy_in_flight_close_over_cap_does_not_warn_until_stalled(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        stall_after = 0.3
        caplog.set_level(logging.WARNING, logger=chunk_store_cache.__name__)
        monkeypatch.setattr(chunk_store_closer, "_STALLED_CLOSE_SECONDS", stall_after)
        cache = ChunkStoreThreadCache(max_open_stores=1)
        repos = [_make_store(tmp_path / f"repo-{n}" / "index") for n in "abcd"]
        close_started, unblock = threading.Event(), threading.Event()

        def blocking_close() -> None:
            close_started.set()
            assert unblock.wait(_JOIN_TIMEOUT_SECONDS)

        _install_close_behaviour(monkeypatch, repos[0][0], blocking_close)

        def cap_warnings() -> int:
            return sum(
                "exceed the process cap" in r.getMessage() for r in caplog.records
            )

        with (
            ThreadPoolExecutor(max_workers=1) as owner,
            ThreadPoolExecutor(max_workers=1) as other,
        ):
            _run_on(owner, _open_reader, cache, *repos[0])
            try:
                _run_on(other, _open_reader, cache, *repos[1])
                assert close_started.wait(_JOIN_TIMEOUT_SECONDS)
                _run_on(other, _open_reader, cache, *repos[2])
                assert len(cache._open_stores) > 1
                assert cap_warnings() == 0

                _wait(stall_after * 2)
                _run_on(other, _open_reader, cache, *repos[3])
                assert cap_warnings() == 1
            finally:
                unblock.set()


class TestChurnCounters:
    def _open_in_turn(self, cache: ChunkStoreThreadCache, tmp_path: Path) -> None:
        repos = [_make_store(tmp_path / f"repo-{n}" / "index") for n in "abc"]
        with ThreadPoolExecutor(max_workers=1) as owner:
            for db, coll in repos:
                _run_on(owner, _open_reader, cache, db, coll)

    def _summaries(self, caplog) -> List[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.INFO and "churn" in r.getMessage()
        ]

    def test_cap_evictions_and_opens_are_counted_and_summarised(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        caplog.set_level(logging.INFO, logger=chunk_store_cache.__name__)
        monkeypatch.setattr(chunk_store_cache, "_CHURN_LOG_INTERVAL_SECONDS", 0.0)
        cache = ChunkStoreThreadCache(max_open_stores=1)
        self._open_in_turn(cache, tmp_path)
        assert getattr(cache, "_cap_evictions", None) == 2
        assert getattr(cache, "_store_opens", None) == 3
        summaries = self._summaries(caplog)
        assert summaries
        assert all("cap evictions" in message for message in summaries)
        assert not any(str(tmp_path) in message for message in summaries)

    def test_no_summary_without_evictions(self, tmp_path: Path, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger=chunk_store_cache.__name__)
        monkeypatch.setattr(chunk_store_cache, "_CHURN_LOG_INTERVAL_SECONDS", 0.0)
        cache = ChunkStoreThreadCache()
        self._open_in_turn(cache, tmp_path)
        assert getattr(cache, "_cap_evictions", None) == 0
        assert self._summaries(caplog) == []


class TestEvictionNeverBlocksTheRequestThread:
    def test_blocking_close_during_eviction_does_not_block_get_or_open(
        self, tmp_path: Path, monkeypatch
    ):
        cache = ChunkStoreThreadCache(max_open_stores=1)
        db_a, coll_a = _make_store(tmp_path / "repo-a" / "index")
        db_b, coll_b = _make_store(tmp_path / "repo-b" / "index")
        close_started, unblock = threading.Event(), threading.Event()

        def blocking_close() -> None:
            close_started.set()
            assert unblock.wait(_JOIN_TIMEOUT_SECONDS)

        _install_close_behaviour(monkeypatch, db_a, blocking_close)
        with (
            ThreadPoolExecutor(max_workers=1) as owner,
            ThreadPoolExecutor(max_workers=1) as other,
        ):
            _run_on(owner, _open_reader, cache, db_a, coll_a)
            try:
                opened = other.submit(_open_reader, cache, db_b, coll_b)
                assert close_started.wait(_JOIN_TIMEOUT_SECONDS)
                assert opened.result(timeout=5) is not None
            finally:
                unblock.set()
