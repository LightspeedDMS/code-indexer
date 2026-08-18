"""Regression tests for the concurrent `_ensure_type_column` TOCTOU race.

Real production failure (E2E, `e2e-automation.sh` Phase 1, CLI standalone,
real subprocess, no mocks): `cidx index --index-commits` against a real
seed repo (markupsafe) failed with

    ERROR    CRITICAL: Failed to index commit 8d96ba7: duplicate column name: type

Root cause: `ChunkStore._ensure_type_column()` (sqlite_chunk_store.py) uses
a check-then-act pattern -- read `PRAGMA table_info(chunks)`, decide
`column_added = "type" not in cols`, then unconditionally run
`ALTER TABLE chunks ADD COLUMN type TEXT` if `column_added`. This is safe
for a SINGLE connection, but `temporal_indexer.py`'s per-commit
`ThreadPoolExecutor` (default 8 workers, see `_get_temporal_thread_count`)
drives EVERY worker thread's `upsert_points()` call through
`FilesystemVectorStore._upsert_points_chunks_db`, which calls
`open_chunk_store_for_path(...)` -- and therefore constructs a BRAND NEW
`ChunkStore` (a brand new `sqlite3.connect()`) -- on EVERY SINGLE upsert
call, never a shared/cached connection. When a fresh temporal shard's
`chunks.db` does not yet exist, multiple worker threads can each open
their own `ChunkStore`, each read `PRAGMA table_info` BEFORE either has
committed the `ALTER TABLE ADD COLUMN`, and each conclude
`column_added = True` -- the second `ALTER TABLE` to actually execute
then raises `sqlite3.OperationalError: duplicate column name: type`.

These tests reproduce the race using ONLY real, unmodified production
code -- no mocking or patching of `ChunkStore` or any of its internals
(Messi Rule #1, Anti-Mock: the code under test must never be mocked). A
`threading.Barrier` synchronizes when each worker thread BEGINS its call
into the real `ChunkStore(db_path)` constructor (entirely OUTSIDE the
class, in this test's own thread-launcher code) to maximize the chance of
genuine overlap, and each test repeats the attempt many times against
FRESH per-iteration databases, since real OS thread scheduling makes a
single attempt's collision probabilistic rather than guaranteed (per this
project's TDD-must-be-discriminating memory: races are proven
probabilistically, not via an artificial deterministic hook).

Confirmed live (RED, before the fix): running this module against the
unmodified production code raises
``sqlite3.OperationalError('duplicate column name: type')`` from multiple
racing threads, exactly matching the real E2E failure message.

Each worker closes its own `ChunkStore` inside the SAME thread that
opened it -- `sqlite3` connections may only be used (including closed)
from their creating thread, so results are reported back as a plain
success count rather than by handing closeable connection objects across
threads.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from typing import Iterator, List, Tuple

import numpy as np
import zstandard

from code_indexer.storage.sqlite_chunk_store import (
    _SCHEMA_SQL,
    ChunkStore,
)

# Mirrors `_get_temporal_thread_count`'s real production default
# (`voyage_ai.parallel_requests` / `cohere.parallel_requests` default of
# 8) -- the exact worker count that produced the real E2E failure.
_RACE_THREAD_COUNT = 8

# How many fresh-database iterations to attempt, since a real-scheduling
# race is probabilistic -- matches the bug investigation's own "run it in
# a loop, e.g. 20-30 times" ask (rounded up for extra confidence).
_RACE_ITERATION_COUNT = 40

_THREAD_START_BARRIER_TIMEOUT_SECONDS = 5.0
_THREAD_JOIN_TIMEOUT_SECONDS = 15.0

# Fixture constants for the "pre-existing data" scenario (a single real
# chunk row already present, in the pre-#1575 schema shape, before the
# `type` column migration ever runs).
_TEST_VECTOR_DIMENSION = 8
_TEST_VECTOR_SEED = 1
_SAMPLE_POINT_ID = "point-1"
_SAMPLE_FILE_PATH = "src/foo.py"
_SAMPLE_CHUNK_TEXT = "def foo():\n    return 42\n"
_SAMPLE_RECORD_TYPE = "content"


def _make_sample_vector() -> List[float]:
    rng = np.random.RandomState(_TEST_VECTOR_SEED)
    result: List[float] = rng.rand(_TEST_VECTOR_DIMENSION).astype(np.float32).tolist()
    return result


def _encode_vector_for_test(vector: List[float]) -> bytes:
    return np.asarray(vector, dtype="<f4").tobytes()


def _encode_data_for_test(record: dict) -> bytes:
    compressor = zstandard.ZstdCompressor()
    passthrough = {k: v for k, v in record.items() if k not in ("id", "vector")}
    raw = json.dumps(passthrough).encode("utf-8")
    return compressor.compress(raw)


def _make_synchronized_open_thread(
    db_path,
    start_barrier: threading.Barrier,
    success_count: List[int],
    errors: List[BaseException],
    lock: threading.Lock,
) -> threading.Thread:
    """Build one worker thread that waits at `start_barrier` and then
    calls the real, completely unmodified `ChunkStore(db_path)`
    constructor -- no part of `ChunkStore` is patched or mocked.

    The barrier wait itself is inside the same exception-capture path as
    the constructor call, so a `threading.BrokenBarrierError` (e.g. if a
    sibling thread failed to arrive in time) is captured into `errors`
    exactly like any other failure, rather than escaping as an unreported
    thread exception. The opened store is closed immediately, in the same
    thread that created it (sqlite3 connections cannot be used, including
    closed, from a different thread), and only a success COUNT crosses
    the thread boundary -- never the connection object itself.
    """

    def open_store() -> None:
        try:
            start_barrier.wait(timeout=_THREAD_START_BARRIER_TIMEOUT_SECONDS)
            store = ChunkStore(db_path)
            store.close()
        except BaseException as exc:  # noqa: BLE001 - captured for assertion
            with lock:
                errors.append(exc)
            return
        with lock:
            success_count[0] += 1

    return threading.Thread(target=open_store, daemon=True)


def _run_and_join_threads(threads: List[threading.Thread]) -> None:
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
    still_alive = [t for t in threads if t.is_alive()]
    if still_alive:
        raise TimeoutError(
            f"{len(still_alive)} of {len(threads)} ChunkStore-open threads "
            f"did not finish within {_THREAD_JOIN_TIMEOUT_SECONDS}s -- "
            f"possible deadlock in the race/recovery path under test"
        )


def _open_chunk_stores_concurrently(
    db_path, thread_count: int
) -> Tuple[int, List[BaseException]]:
    """Open `thread_count` real `ChunkStore` instances against `db_path`
    concurrently from separate threads, all released together from a
    shared start barrier. Returns `(success_count, errors)`.
    """
    success_count = [0]
    errors: List[BaseException] = []
    lock = threading.Lock()
    start_barrier = threading.Barrier(thread_count)

    threads = [
        _make_synchronized_open_thread(
            db_path, start_barrier, success_count, errors, lock
        )
        for _ in range(thread_count)
    ]

    _run_and_join_threads(threads)
    return success_count[0], errors


@contextlib.contextmanager
def _real_connection(db_path) -> Iterator[sqlite3.Connection]:
    """A plain, unpatched sqlite3 connection for post-race verification
    queries -- never touches `ChunkStore` or any production code."""
    conn = sqlite3.connect(str(db_path))
    try:
        yield conn
    finally:
        conn.close()


class TestConcurrentTypeColumnMigrationFreshDatabase:
    """Multiple ChunkStore instances race to open the SAME brand-new
    (never existed on disk) chunks.db -- the exact first-write-to-a-
    fresh-shard scenario from the real E2E failure.
    """

    def test_repeated_concurrent_opens_on_fresh_db_never_raise(self, tmp_path):
        for i in range(_RACE_ITERATION_COUNT):
            db_path = tmp_path / f"chunks_{i}.db"
            success_count, errors = _open_chunk_stores_concurrently(
                db_path, _RACE_THREAD_COUNT
            )
            assert not errors, (
                f"Iteration {i}: concurrent ChunkStore open on a fresh db "
                f"raised: {errors!r}"
            )
            assert success_count == _RACE_THREAD_COUNT

    def test_schema_has_type_column_and_index_exactly_once_after_race(self, tmp_path):
        db_path = tmp_path / "chunks.db"

        success_count, errors = _open_chunk_stores_concurrently(
            db_path, _RACE_THREAD_COUNT
        )
        assert not errors, (
            f"Concurrent ChunkStore open on a fresh db raised: {errors!r}"
        )
        assert success_count == _RACE_THREAD_COUNT

        with _real_connection(db_path) as conn:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(chunks)")]
            assert cols.count("type") == 1
            index_names = {
                row[1]
                for row in conn.execute(
                    "SELECT * FROM sqlite_master WHERE type = 'index'"
                )
            }
            assert "idx_chunks_type" in index_names


class TestConcurrentTypeColumnMigrationPreExistingData:
    """Multiple ChunkStore instances race to open a PRE-EXISTING db (one
    real row already written, schema predates the `type` column) --
    verifies the backfill still runs, exactly once, with correct data,
    even under concurrent migration attempts.
    """

    def _seed_pre_migration_database(self, db_path) -> None:
        """Create a chunks.db in the PRE-#1575 shape: schema WITHOUT the
        `type` column, with one real row already present -- so opening it
        exercises the backfill path, not just the fresh-table no-op path.
        """
        with _real_connection(db_path) as conn:
            conn.executescript(_SCHEMA_SQL)
            record = {
                "metadata": {"language": "python", "type": _SAMPLE_RECORD_TYPE},
                "payload": {"path": _SAMPLE_FILE_PATH, "type": _SAMPLE_RECORD_TYPE},
                "chunk_text": _SAMPLE_CHUNK_TEXT,
            }
            conn.execute(
                "INSERT INTO chunks (point_id, path, vector, data) VALUES (?, ?, ?, ?)",
                (
                    _SAMPLE_POINT_ID,
                    _SAMPLE_FILE_PATH,
                    _encode_vector_for_test(_make_sample_vector()),
                    _encode_data_for_test(record),
                ),
            )
            conn.commit()

    def test_concurrent_migration_of_existing_data_backfills_correctly(self, tmp_path):
        db_path = tmp_path / "chunks.db"
        self._seed_pre_migration_database(db_path)

        success_count, errors = _open_chunk_stores_concurrently(
            db_path, _RACE_THREAD_COUNT
        )
        assert not errors, f"Concurrent migration of existing data raised: {errors!r}"
        assert success_count == _RACE_THREAD_COUNT

        with _real_connection(db_path) as conn:
            row = conn.execute(
                "SELECT type FROM chunks WHERE point_id = ?", (_SAMPLE_POINT_ID,)
            ).fetchone()

        assert row is not None
        assert row[0] == _SAMPLE_RECORD_TYPE
