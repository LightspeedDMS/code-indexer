"""Bug #1829: discriminating regression coverage for the temporal-indexing
chunks.db lock-contention fix.

AC1 (TestBug1829TemporalConcurrentWritersSurviveLockContention): a
discriminating regression test proving that concurrent temporal-shaped
writers against a single real chunks.db, contending with a genuine held
EXCLUSIVE lock from a separate connection, abort with
sqlite3.OperationalError("database is locked") on the CURRENT (unfixed)
code -- and must NOT abort once the fix (bounded retry, reusing the
existing is_fatal_chunk_store_write_error() classifier) lands.

AC3 (TestBug1829NoCommitDroppedUnderConcurrentContention): many real
concurrent writers under natural self-contention must all end up
persisted -- exact point-id-count equality between attempted and stored,
proving the fix does not merely mask the abort by silently dropping a
commit.

AC4 (TestBug1829FatalChunkStoreErrorStillAbortsLoud): a genuinely fatal
chunk-store condition (unwritable chunks.db) must still raise loud on the
very first attempt, with no retry delay wasted on a condition retrying
can never fix.

AC5 (TestBug1829RetryExhaustionIsBoundedAndLoud): the retry wrapper's own
loop, exercised directly with a tiny parameterized attempt/backoff
schedule and a forced-always-transient failure, must terminate within a
provably bounded time and raise loud -- never hang, never swallow.

Root cause (verified in the issue, not re-derived here): every temporal
worker thread calls vector_store.upsert_points() per commit
(temporal_indexer.py:1237), and FilesystemVectorStore._upsert_points_
chunks_db() opens a BRAND-NEW ChunkStore connection to the SAME chunks.db
per call with no cross-thread application write lock. Python's sqlite3
default busy-timeout is 5.0s; journal_mode=DELETE gives zero read/write
concurrency, so a lock held longer than 5s is genuinely exhausted, not a
scheduling artifact.

Same proven technique as tests/unit/storage/test_filesystem_vector_store_
1746_preflight.py's TestPreflightTransientLockContentionDoesNotAbort and
tests/e2e/server/test_22_chunk_store_lock_contention_1746.py: a real
second sqlite3 connection holds a genuine BEGIN EXCLUSIVE lock from a
background thread, held deliberately longer than the 5s default so the
failure is deterministic, not a lucky race. Unlike those two (which cover
the preflight check and a single write respectively), this test drives AT
LEAST TWO concurrent real writers through the actual production write
path at once -- the shape Bug #1829 is specifically about (temporal's own
worker threads contending with EACH OTHER, not just with an external
holder).

Real filesystem I/O via FilesystemVectorStore + tmp_path throughout -- no
mocking.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.sqlite_chunk_store import open_chunk_store_for_path

VECTOR_DIM = 8

# Held comfortably longer than sqlite3's default 5.0s busy-timeout so the
# concurrent writers' open()+write() genuinely observes "database is
# locked" exhaustion, not a lucky race.
_LOCK_HOLD_SECONDS = 6.0


def _make_temporal_point(commit_hash: str) -> Dict[str, Any]:
    """Shape matches what temporal_indexer.py builds per commit (a single
    chunk per commit is enough to exercise the write path)."""
    return {
        "id": f"commit:{commit_hash}",
        "vector": [0.1] * VECTOR_DIM,
        "payload": {
            "type": "commit",
            "commit_hash": commit_hash,
            "path": f"commit:{commit_hash}",
        },
    }


class TestBug1829TemporalConcurrentWritersSurviveLockContention:
    """AC1: concurrent real production writers (temporal-shaped) against
    one real chunks.db, contending with a genuine held EXCLUSIVE lock from
    a separate connection/thread, must not abort the run on transient
    'database is locked' contention -- they must wait/retry until they
    succeed."""

    def test_concurrent_temporal_writers_do_not_abort_on_transient_lock(
        self, tmp_path
    ) -> None:
        from code_indexer.storage.filesystem_vector_store import (
            FilesystemVectorStore,
        )

        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("temporal_coll", vector_size=VECTOR_DIM)
        store.begin_indexing("temporal_coll")
        # Seed one point so chunks.db exists on disk before we start
        # contending for it.
        store.upsert_points("temporal_coll", [_make_temporal_point("seed0000")])

        chunks_db_path = tmp_path / "temporal_coll" / "chunks.db"
        assert chunks_db_path.exists()

        # Hold a REAL exclusive lock from a separate connection/thread --
        # exactly the transient contention shape N temporal worker threads
        # produce against EACH OTHER in production (each opens a brand-new
        # ChunkStore connection per upsert_points() call -- Bug #1829's
        # verified root cause).
        lock_acquired = threading.Event()

        def _hold_lock() -> None:
            conn = sqlite3.connect(str(chunks_db_path))
            try:
                conn.execute("BEGIN EXCLUSIVE")
                lock_acquired.set()
                time.sleep(_LOCK_HOLD_SECONDS)
            finally:
                try:
                    conn.execute("ROLLBACK")
                finally:
                    conn.close()

        lock_thread = threading.Thread(target=_hold_lock, daemon=True)
        lock_thread.start()
        assert lock_acquired.wait(timeout=5.0), (
            "lock-holder thread never acquired the lock"
        )

        # At least two concurrent REAL production writers -- the same
        # vector_store.upsert_points() call every temporal worker thread
        # makes per commit (temporal_indexer.py:1237).
        errors: List[BaseException] = []
        errors_lock = threading.Lock()

        def _writer(commit_hash: str) -> None:
            try:
                store.upsert_points(
                    "temporal_coll", [_make_temporal_point(commit_hash)]
                )
            except BaseException as exc:  # noqa: BLE001 -- captured for assertion
                with errors_lock:
                    errors.append(exc)

        writer_threads = [
            threading.Thread(target=_writer, args=(f"commit{i:04d}",)) for i in range(2)
        ]
        for t in writer_threads:
            t.start()

        # Bounded join -- the assertion below independently proves
        # liveness, this just guards the test itself from hanging.
        join_deadline = _LOCK_HOLD_SECONDS + 40.0
        for t in writer_threads:
            t.join(timeout=join_deadline)
        lock_thread.join(timeout=_LOCK_HOLD_SECONDS + 5.0)

        assert not any(t.is_alive() for t in writer_threads), (
            "writer thread(s) did not finish within the bounded join timeout"
        )

        # RED (current/unfixed code): at least one concurrent writer's
        # brand-new ChunkStore connection exhausts sqlite3's default 5s
        # busy-timeout while the lock is held and raises
        # sqlite3.OperationalError("database is locked"), proving the bug
        # and aborting exactly like temporal_indexer.py's worker() does
        # via future.result().
        #
        # GREEN (after the fix lands): this list must be empty -- the
        # bounded retry absorbs the transient contention and every writer
        # succeeds once the lock releases.
        assert not errors, (
            f"Bug #1829: {len(errors)} of {len(writer_threads)} concurrent "
            f"temporal-shaped writer(s) raised instead of surviving "
            f"transient lock contention: {errors!r}"
        )


_RUNNING_AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

# AC3: enough concurrent writers to guarantee natural self-contention
# under journal_mode=DELETE's whole-file locking, with no artificial
# lock holder needed.
_AC3_NUM_WRITERS = 12
_WRITER_JOIN_TIMEOUT_SECONDS = 60.0

# AC4: chmod bits for "genuinely unwritable" vs. the restore value.
_UNWRITABLE_MODE = 0o000
_RESTORE_MODE = 0o644
# Well under even ONE retry backoff (0.5s) plus a wide safety margin --
# see the assertion below for why this is discriminating.
_AC4_MAX_ELAPSED_SECONDS = 2.0

# AC5: tiny attempt/backoff schedule so bounded-termination is proven in
# milliseconds instead of the ~32.5s production worst case.
_AC5_MAX_ATTEMPTS = 3
_AC5_BACKOFF_SCHEDULE = (0.01, 0.02)
_AC5_MAX_ELAPSED_SECONDS = 5.0


def _run_concurrent_writers(
    store: FilesystemVectorStore, collection_name: str, commit_hashes: List[str]
) -> List[BaseException]:
    """Fire one real production upsert_points() call per commit hash on
    its own thread, join with a bounded timeout, and return whatever
    exceptions each writer raised (empty list means every writer
    succeeded)."""
    errors: List[BaseException] = []
    errors_lock = threading.Lock()

    def _writer(commit_hash: str) -> None:
        try:
            store.upsert_points(collection_name, [_make_temporal_point(commit_hash)])
        except BaseException as exc:  # noqa: BLE001 -- captured for assertion
            with errors_lock:
                errors.append(exc)

    writer_threads = [
        threading.Thread(target=_writer, args=(h,)) for h in commit_hashes
    ]
    for t in writer_threads:
        t.start()
    for t in writer_threads:
        t.join(timeout=_WRITER_JOIN_TIMEOUT_SECONDS)

    assert not any(t.is_alive() for t in writer_threads), (
        "writer thread(s) did not finish within the bounded join timeout"
    )
    return errors


class TestBug1829NoCommitDroppedUnderConcurrentContention:
    """AC3: transient lock contention must never silently drop a commit.
    Many real concurrent writers (natural self-contention -- N threads
    hammering one chunks.db under journal_mode=DELETE is contention
    enough on its own, no artificial lock holder needed) must all end up
    persisted: exact point-id-count equality between attempted and
    stored, proving the fix does not merely mask the abort by quietly
    losing a commit."""

    def test_all_concurrent_distinct_writes_are_persisted(self, tmp_path) -> None:
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("temporal_coll", vector_size=VECTOR_DIM)
        store.begin_indexing("temporal_coll")

        commit_hashes = [f"concurrent{i:04d}" for i in range(_AC3_NUM_WRITERS)]
        errors = _run_concurrent_writers(store, "temporal_coll", commit_hashes)
        assert not errors, (
            f"Bug #1829 AC3: {len(errors)} of {_AC3_NUM_WRITERS} concurrent "
            f"writers raised instead of being retried to success: {errors!r}"
        )

        persisted_ids, persisted_count = self._read_back(tmp_path)

        expected_ids = {f"commit:{h}" for h in commit_hashes}
        assert persisted_ids == expected_ids, (
            f"Bug #1829 AC3: commit set mismatch -- missing="
            f"{expected_ids - persisted_ids!r}, "
            f"unexpected={persisted_ids - expected_ids!r}"
        )
        assert persisted_count == _AC3_NUM_WRITERS, (
            f"Bug #1829 AC3: expected exactly {_AC3_NUM_WRITERS} persisted "
            f"records, found {persisted_count} -- a silent partial index"
        )

    @staticmethod
    def _read_back(tmp_path) -> "tuple[set, int]":
        collection_path = tmp_path / "temporal_coll"
        chunk_store = open_chunk_store_for_path(
            collection_path / "chunks.db", str(collection_path), read_only=True
        )
        try:
            return chunk_store.all_point_ids(), chunk_store.count()
        finally:
            chunk_store.close()


class TestBug1829FatalChunkStoreErrorStillAbortsLoud:
    """AC4: a genuinely fatal chunk-store condition (unwritable chunks.db)
    must still raise LOUD on the very first attempt -- no retries wasted
    on a condition retrying can never fix, classified via the SAME
    is_fatal_chunk_store_write_error() the retry wrapper uses for
    transient contention."""

    @pytest.mark.skipif(
        _RUNNING_AS_ROOT,
        reason="chmod 000 does not deny a root/DAC-override process",
    )
    def test_unwritable_chunks_db_raises_immediately_without_retry_delay(
        self, tmp_path
    ) -> None:
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("temporal_coll", vector_size=VECTOR_DIM)
        store.begin_indexing("temporal_coll")
        store.upsert_points("temporal_coll", [_make_temporal_point("seed0000")])

        chunks_db_path = tmp_path / "temporal_coll" / "chunks.db"
        assert chunks_db_path.exists()
        os.chmod(chunks_db_path, _UNWRITABLE_MODE)

        try:
            start = time.monotonic()
            with pytest.raises(Exception) as exc_info:
                store.upsert_points(
                    "temporal_coll", [_make_temporal_point("commit_fatal")]
                )
            elapsed = time.monotonic() - start
        finally:
            os.chmod(chunks_db_path, _RESTORE_MODE)

        # If the fatal classification were broken and this fell through
        # to the transient-retry path instead, the accumulated backoff
        # sleeps alone (0.5+1+2+4=7.5s minimum) would blow well past this
        # bound -- so a fast failure here proves attempt 1 short-circuited
        # on the fatal classifier rather than retrying.
        assert elapsed < _AC4_MAX_ELAPSED_SECONDS, (
            f"Bug #1829 AC4: fatal chunk-store error took {elapsed:.2f}s "
            f"to surface -- looks like it was retried instead of failing "
            f"immediately on the first attempt"
        )
        assert "unable to open database file" in str(exc_info.value).lower()


class TestBug1829RetryExhaustionIsBoundedAndLoud:
    """AC5: any wait/retry introduced must be provably bounded -- never an
    unbounded loop or a hang. Exercises the retry wrapper's own loop
    directly (_write_chunks_db_with_retry) with a tiny parameterized
    attempt/backoff schedule and a forced-always-transient failure
    (patching the sqlite I/O seam the wrapper calls, not its own retry
    logic) so the bound is proven in milliseconds instead of needing to
    hold a real lock for the full ~32.5s production worst case."""

    def test_exhausted_retries_raise_loud_within_a_bounded_time(self, tmp_path) -> None:
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("temporal_coll", vector_size=VECTOR_DIM)
        store.begin_indexing("temporal_coll")

        collection_path = tmp_path / "temporal_coll"

        call_count = 0

        # Mocks open_chunk_store_for_path(db_path, collection_path, *,
        # read_only=False) -- *args/**kwargs deliberately absorb that
        # exact signature since this double always raises regardless of
        # which arguments the wrapper passes on any given attempt.
        def _always_locked(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            raise sqlite3.OperationalError("database is locked")

        with patch(
            "code_indexer.storage.sqlite_chunk_store.open_chunk_store_for_path",
            side_effect=_always_locked,
        ):
            start = time.monotonic()
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                store._write_chunks_db_with_retry(
                    collection_path,
                    [],
                    [],
                    max_attempts=_AC5_MAX_ATTEMPTS,
                    backoff_schedule=_AC5_BACKOFF_SCHEDULE,
                )
            elapsed = time.monotonic() - start

        assert call_count == _AC5_MAX_ATTEMPTS, (
            f"Bug #1829 AC5: expected exactly max_attempts="
            f"{_AC5_MAX_ATTEMPTS} open attempts before exhaustion, got "
            f"{call_count} -- the retry loop is not respecting its own "
            f"bound"
        )
        assert elapsed < _AC5_MAX_ELAPSED_SECONDS, (
            f"Bug #1829 AC5: exhausted-retry path took {elapsed:.2f}s -- "
            f"expected a bounded few hundred milliseconds given tiny test "
            f"backoffs {_AC5_BACKOFF_SCHEDULE}; this looks unbounded, not "
            f"merely slow"
        )


class TestBug1829RetryClosesStoreBeforeBackoff:
    """Defect 1: release a write-failed store before sleeping to retry."""

    def test_write_failure_closes_store_before_sleep(self, tmp_path) -> None:
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("temporal_coll", vector_size=VECTOR_DIM)
        store.begin_indexing("temporal_coll")

        events = []

        class _WriteFailingStore:
            def __init__(self, attempt: int) -> None:
                self.attempt = attempt

            def write_batch(self, records) -> None:
                events.append(("write", self.attempt))
                if self.attempt == 0:
                    raise sqlite3.OperationalError("database is locked")

            def close(self) -> None:
                events.append(("close", self.attempt))

        opened_stores: List[_WriteFailingStore] = []

        def _open_store(*args: Any, **kwargs: Any) -> _WriteFailingStore:
            chunk_store = _WriteFailingStore(len(opened_stores))
            opened_stores.append(chunk_store)
            return chunk_store

        with (
            patch(
                "code_indexer.storage.sqlite_chunk_store.open_chunk_store_for_path",
                side_effect=_open_store,
            ),
            patch(
                "code_indexer.storage.filesystem_vector_store.time.sleep",
                side_effect=lambda delay: events.append(("sleep", delay)),
            ),
        ):
            store._write_chunks_db_with_retry(
                tmp_path / "temporal_coll",
                [{"id": "point-1"}],
                [],
                max_attempts=2,
                backoff_schedule=(0.01,),
            )

        assert len(opened_stores) == 2
        # Defect 2 (jitter) randomizes the sleep duration, so the sleep
        # event is located by tag rather than by exact scheduled value --
        # the property under test is ordering, not the duration itself.
        sleep_index = next(i for i, e in enumerate(events) if e[0] == "sleep")
        assert events.index(("write", 0)) < sleep_index
        assert events.index(("close", 0)) < sleep_index


class TestBug1829RetryUsesBoundedJitter:
    """Defect 2: desynchronize transient retries without widening the bound."""

    def test_retry_sleeps_for_jittered_backoff(self, tmp_path) -> None:
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("temporal_coll", vector_size=VECTOR_DIM)
        store.begin_indexing("temporal_coll")

        def _always_locked(*args: Any, **kwargs: Any) -> None:
            raise sqlite3.OperationalError("database is locked")

        sleep_values: List[float] = []
        with (
            patch(
                "code_indexer.storage.sqlite_chunk_store.open_chunk_store_for_path",
                side_effect=_always_locked,
            ),
            patch(
                "code_indexer.storage.filesystem_vector_store.random.uniform",
                return_value=0.123,
            ) as uniform_mock,
            patch(
                "code_indexer.storage.filesystem_vector_store.time.sleep",
                side_effect=sleep_values.append,
            ),
        ):
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                store._write_chunks_db_with_retry(
                    tmp_path / "temporal_coll",
                    [],
                    [],
                    max_attempts=2,
                    backoff_schedule=(0.5,),
                )

        uniform_mock.assert_called_once_with(0, 0.5)
        assert sleep_values == [0.123]
