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

The two threaded tests below (`test_schema_has_type_column_and_index_
exactly_once_after_race`, `test_concurrent_migration_of_existing_data_
backfills_correctly`) reproduce the race using ONLY real, unmodified
production code called from real OS threads -- no mocking or patching of
`ChunkStore` or any of its internals (Messi Rule #1, Anti-Mock: the code
under test must never be mocked). A `threading.Barrier` synchronizes when
each worker thread BEGINS its call into the real `ChunkStore(db_path)`
constructor (entirely OUTSIDE the class, in this test's own
thread-launcher code) to maximize the chance of genuine overlap; real OS
thread scheduling still makes a single attempt's collision probabilistic
rather than guaranteed, so these two remain smoke/property checks (real
multi-thread contention must not raise, deadlock, or corrupt the schema)
rather than the primary discrimination guard for the #1585 race.

The third test (`test_concurrent_open_recovers_from_deterministically_
constructed_toctou_race`, below) takes a DIFFERENT, complementary
approach and DOES patch one thing: `sqlite3.connect`, scoped to the exact
target database path only (see `_open_chunk_store_with_deterministic_
race`'s own docstring), as a narrow TIMING seam -- never `ChunkStore`,
never `_ensure_type_column`, and never any connection to any OTHER
database. Both the connection under test and the "racer" connection it
triggers are genuine `sqlite3.Connection` objects against a genuine
on-disk file; only the moment the racer connection commits its `ALTER
TABLE` is pinned relative to the connection under test's own execution.
That test carries full discrimination responsibility for the #1585 race,
deterministically, on every single run (see `_RaceInjectingConnection`'s
own docstring for the full mechanism).

Confirmed live (RED, before the fix): running this module against the
unmodified production code raises
``sqlite3.OperationalError('duplicate column name: type')`` from multiple
racing threads, exactly matching the real E2E failure message.

Each worker closes its own `ChunkStore` inside the SAME thread that
opened it -- `sqlite3` connections may only be used (including closed)
from their creating thread, so results are reported back as a plain
success count rather than by handing closeable connection objects across
threads.

Bug #1823: the original probabilistic thread-based approach (40, then 10,
iterations of 8-thread contention against fresh per-iteration databases)
turned out to be a poor discriminator (per-iteration collision
probability ~1%, so it needed roughly 10 iterations to have even a
1-in-10 chance of catching a regression) AND slow in its worst case
(24.58s observed in one isolated run) -- both because SQLite's own
exclusive write lock serializes threads through `_ensure_schema()`'s
shared setup well before most of them ever reach the narrow TOCTOU
window, so raw thread/iteration count was not an effective lever. That
iteration loop and its now-pointless 2-iteration remnant (`_RACE_
ITERATION_COUNT`) were deleted outright -- see
`test_schema_has_type_column_and_index_exactly_once_after_race` and
`test_concurrent_migration_of_existing_data_backfills_correctly` above
for the two real-thread-contention checks that remain, each already
providing the same "no raise/deadlock under genuine 8-thread contention"
property at 8 threads x 1 iteration. The PRIMARY discrimination guard for
the #1585 race is `_RaceInjectingConnection` (below): a real second
`sqlite3` connection is driven, from the test itself, to win the `ALTER
TABLE` race at the exact moment the connection under test has just
observed `PRAGMA table_info(chunks)` -- deterministically constructing
the #1585 race on every single run instead of hoping real OS thread
scheduling collides. Neither connection is mocked -- both are genuine
`sqlite3.Connection` objects against a genuine on-disk file; only the
TIMING of the second connection's write is pinned via a thin forwarding
seam, and `ChunkStore._ensure_type_column` itself is never patched or
stubbed.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import unittest.mock
from typing import Iterator, List, Tuple

import numpy as np
import pytest
import zstandard

import code_indexer.storage.sqlite_chunk_store as sqlite_chunk_store
from code_indexer.storage.sqlite_chunk_store import (
    _SCHEMA_SQL,
    ChunkStore,
)

# Mirrors `_get_temporal_thread_count`'s real production default
# (`voyage_ai.parallel_requests` / `cohere.parallel_requests` default of
# 8) -- the exact worker count that produced the real E2E failure.
_RACE_THREAD_COUNT = 8

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


# Bug #1823: the ORIGINAL (never-patched) `sqlite3.connect` reference,
# captured once at import time -- before any test in this module patches
# `sqlite3.connect`. `_RaceInjectingConnection` and
# `_open_chunk_store_with_deterministic_race` both use ONLY this
# reference to open real connections, so neither the connection under
# test's underlying real connection nor the "racer" connection it spawns
# can ever recursively route back through the patched path.
_ORIGINAL_SQLITE3_CONNECT = sqlite3.connect


class _RaceInjectingConnection:
    """Wraps a REAL `sqlite3.Connection` opened against the SAME db file
    the `ChunkStore` under test is opening. Every operation forwards
    UNCHANGED to that real connection, except the FIRST call whose SQL
    text is exactly production's `PRAGMA table_info(chunks)` probe (the
    "check" half of `_ensure_type_column`'s check-then-act TOCTOU) --
    that call triggers a second, fully independent REAL `sqlite3`
    connection against the SAME file to run and commit the `type` column
    `ALTER TABLE` BEFORE control returns to the connection under test.

    This deterministically constructs the exact #1585 race (connection A
    observes no `type` column, connection B adds and commits it,
    connection A's own `ALTER` then raises `duplicate column name`) on
    every single run, instead of relying on real OS thread scheduling to
    collide inside a narrow window. Neither connection is mocked or
    faked -- both are genuine `sqlite3.Connection` objects against a
    genuine on-disk file; only the TIMING of the second connection's
    write is pinned to a specific point in the first connection's
    execution, via this thin forwarding seam.
    `ChunkStore._ensure_type_column` itself is never patched or stubbed
    -- it is the real, unmodified code under test.
    """

    _PRAGMA_PROBE_SQL = "PRAGMA table_info(chunks)"

    def __init__(
        self,
        real_conn: sqlite3.Connection,
        db_path,
        racer_backfills: bool = False,
    ) -> None:
        self._real_conn = real_conn
        self._db_path = db_path
        self._raced = False
        # Bug #1823 code review Finding H3: on a database with
        # PRE-EXISTING rows, the genuine winner of a real production race
        # (some OTHER real `ChunkStore` instance) also runs its own
        # `_backfill_type_column()` after its `ALTER TABLE` commits -- see
        # `_ensure_type_column`. The minimal racer connection below has no
        # such logic by default (irrelevant on the FRESH-db scenario,
        # where there is nothing to backfill), so tests that seed
        # pre-existing data and need to assert the backfilled value
        # survives the race opt in via this flag.
        self._racer_backfills = racer_backfills

    def execute(self, sql, *params):
        cursor = self._real_conn.execute(sql, *params)
        if not self._raced and sql == self._PRAGMA_PROBE_SQL:
            self._raced = True
            # Materialize and CLOSE the pragma cursor before racing: an
            # open cursor still holds SQLite's SHARED read lock in
            # rollback-journal mode, which would make the racer's
            # `ALTER TABLE` block (or raise "database is locked")
            # instead of committing cleanly -- a timing production
            # itself never hits, since `for row in
            # self._conn.execute(...)` fully drains and implicitly
            # finalizes the statement before any other work happens on
            # that connection. Returning the materialized list (rather
            # than the live cursor) preserves the exact iteration
            # behaviour the caller (`_ensure_type_column`) relies on.
            rows = cursor.fetchall()
            cursor.close()
            self._win_the_race()
            return rows
        return cursor

    def _win_the_race(self) -> None:
        racer = _ORIGINAL_SQLITE3_CONNECT(str(self._db_path))
        try:
            racer.execute("ALTER TABLE chunks ADD COLUMN type TEXT")
            if self._racer_backfills:
                # Bare instance via __new__ (bypasses __init__, so no
                # second sqlite3.connect() call -- avoids re-entering
                # this test's own connect() patch) whose ONLY attributes
                # are the two `_backfill_type_column()` actually reads
                # (`_conn`, `_decompressor`). Calls the REAL, unmodified
                # production method -- never a reimplementation of its
                # decode/UPDATE logic.
                racer_store = ChunkStore.__new__(ChunkStore)
                racer_store._conn = racer
                racer_store._decompressor = zstandard.ZstdDecompressor()
                racer_store._backfill_type_column()
            racer.commit()
        finally:
            racer.close()

    def __getattr__(self, name):
        return getattr(self._real_conn, name)

    def __enter__(self):
        # Bug #1823 code review Finding B2: `__getattr__` cannot supply
        # special-method (dunder) lookups -- Python looks those up on the
        # TYPE, never via instance `__getattr__` -- so without this
        # explicit forwarding, any real production code that does
        # `with self._conn:` would raise `AttributeError: __enter__`
        # instead of exercising sqlite3's real transaction semantics.
        # Returns THIS proxy (not the real connection) so the seam stays
        # in place for every statement executed inside the `with` block.
        self._real_conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._real_conn.__exit__(exc_type, exc_val, exc_tb)


def _open_chunk_store_with_deterministic_race(
    db_path, *, racer_backfills: bool = False
) -> ChunkStore:
    """Open a real `ChunkStore(db_path)` whose underlying connection is
    wrapped by `_RaceInjectingConnection`, so the #1585 TOCTOU race is
    constructed deterministically rather than hoped for via concurrent
    threads.

    Patches `sqlite3.connect` as looked up from `sqlite_chunk_store`'s own
    module namespace -- the exact name `ChunkStore._open_connection`
    calls -- restored automatically on context exit. Bug #1823 code
    review Finding B1: the patch's `side_effect` (`_connect_and_wrap`)
    compares the incoming `path` against THIS call's own `db_path` and
    delegates unwrapped to `_ORIGINAL_SQLITE3_CONNECT` for anything else
    -- so any OTHER connection opened anywhere in the process during the
    `with` block (a leftover daemon thread from an earlier test, e.g. a
    BGM worker, log-store writer, or health monitor) is completely
    unaffected; only a connection opened against this exact `db_path`
    gets wrapped. The racer connection spawned from inside the wrapper
    (`_RaceInjectingConnection._win_the_race`) always uses the captured,
    never-patched `_ORIGINAL_SQLITE3_CONNECT` directly, so it can never
    recursively route back through this patch either.

    `racer_backfills` (Bug #1823 Finding H3): pass True when `db_path`
    already has pre-existing rows and the test needs the racer to model a
    genuine winning `ChunkStore`'s full behaviour (ALTER + backfill), not
    just its ALTER TABLE.
    """

    def _connect_and_wrap(path, *args, **kwargs):
        real_conn = _ORIGINAL_SQLITE3_CONNECT(path, *args, **kwargs)
        if str(path) != str(db_path):
            return real_conn
        return _RaceInjectingConnection(
            real_conn, db_path, racer_backfills=racer_backfills
        )

    with unittest.mock.patch.object(
        sqlite_chunk_store.sqlite3, "connect", side_effect=_connect_and_wrap
    ):
        return ChunkStore(db_path)


class _UnrelatedErrorInjectingConnection:
    """Wraps a REAL `sqlite3.Connection`. Forwards everything unchanged,
    EXCEPT the one call executing production's exact `ALTER TABLE chunks
    ADD COLUMN type TEXT` statement (the "act" half of
    `_ensure_type_column`'s check-then-act), which instead raises a
    FABRICATED `sqlite3.OperationalError` whose message does NOT contain
    "duplicate column name" -- proving Messi Rule #13 (Anti-Silent-
    Failure): `_ensure_type_column`'s narrow `except sqlite3.
    OperationalError` (sqlite_chunk_store.py:447-449) re-raises any
    OperationalError OTHER than the one specific benign #1585 race it is
    designed to recover from, instead of silently swallowing every
    OperationalError indiscriminately -- a bare `except sqlite3.
    OperationalError: pass` would still pass every OTHER test in this
    module, so this is the ONLY test that would catch that widening.

    This is a deliberate, narrowly-scoped fault injection (fabricating
    the error rather than provoking a genuine one from real contention):
    a real "database is locked" collision would need Python's default 5s
    busy-timeout to elapse before finally raising, which this project's
    <5s test-budget bar (Bug #1823) cannot afford. `_ensure_type_column`
    itself is never patched or stubbed; only this one targeted SQL
    statement's outcome is substituted.
    """

    _ALTER_SQL = "ALTER TABLE chunks ADD COLUMN type TEXT"
    _INJECTED_MESSAGE = "simulated unrelated sqlite failure"

    def __init__(self, real_conn: sqlite3.Connection) -> None:
        self._real_conn = real_conn

    def execute(self, sql, *params):
        if sql == self._ALTER_SQL:
            raise sqlite3.OperationalError(self._INJECTED_MESSAGE)
        return self._real_conn.execute(sql, *params)

    def __getattr__(self, name):
        return getattr(self._real_conn, name)

    def __enter__(self):
        self._real_conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._real_conn.__exit__(exc_type, exc_val, exc_tb)


def _open_chunk_store_with_unrelated_operational_error(db_path) -> ChunkStore:
    """Bug #1823 Finding H3: mirrors `_open_chunk_store_with_deterministic_
    race`'s scoping discipline (patch `sqlite3.connect`, scoped to this
    exact `db_path`, delegate everything else to the real, never-patched
    reference) but wraps the connection under test with
    `_UnrelatedErrorInjectingConnection` instead of `_RaceInjectingConnection`.
    """

    def _connect_and_wrap(path, *args, **kwargs):
        real_conn = _ORIGINAL_SQLITE3_CONNECT(path, *args, **kwargs)
        if str(path) != str(db_path):
            return real_conn
        return _UnrelatedErrorInjectingConnection(real_conn)

    with unittest.mock.patch.object(
        sqlite_chunk_store.sqlite3, "connect", side_effect=_connect_and_wrap
    ):
        return ChunkStore(db_path)


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

    def test_concurrent_open_recovers_from_deterministically_constructed_toctou_race(
        self, tmp_path
    ):
        """Bug #1823: the PRIMARY discrimination guard for the #1585 race,
        replacing dependence on real thread-scheduling luck.

        Constructs the exact race deterministically (see
        `_RaceInjectingConnection`) so it fires on every single run,
        with 100% detection power instead of the probabilistic collision
        the deleted thread-iteration approach relied on (see this
        module's own docstring for that history), then asserts the store
        opened successfully and the schema ends up correct.
        """
        db_path = tmp_path / "chunks_deterministic_race.db"

        store = _open_chunk_store_with_deterministic_race(db_path)
        store.close()

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

    def test_ensure_type_column_reraises_non_duplicate_operational_error(
        self, tmp_path
    ):
        """Bug #1823 Finding H3 (Messi Rule #13, Anti-Silent-Failure):
        `_ensure_type_column`'s recovery must re-raise any
        `sqlite3.OperationalError` that is NOT the specific benign #1585
        race -- proving the narrow ``"duplicate column name" not in
        str(exc).lower()`` check (sqlite_chunk_store.py:448) is
        load-bearing, not vestigial. If that recovery were ever widened
        to a bare ``except sqlite3.OperationalError: pass``, every OTHER
        test in this module would still pass -- only this one would fail.
        """
        db_path = tmp_path / "chunks_unrelated_error.db"

        with pytest.raises(
            sqlite3.OperationalError,
            match=_UnrelatedErrorInjectingConnection._INJECTED_MESSAGE,
        ):
            _open_chunk_store_with_unrelated_operational_error(db_path)

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

    def test_deterministic_race_on_pre_existing_data_still_backfills_correctly(
        self, tmp_path
    ):
        """Bug #1823 Finding H3 (second gap): the deterministic race test
        above (`test_concurrent_open_recovers_from_deterministically_
        constructed_toctou_race`) only ever runs against a FRESH
        database, so it exercises the `column_added = False` (loser skips
        its own backfill) half of `_ensure_type_column` trivially --
        there was never any pre-existing row to backfill in the first
        place. Pointing the SAME deterministic seam at a seeded
        PRE-migration database, with `racer_backfills=True` so the racer
        models a genuine winning `ChunkStore`'s full behaviour (see
        `_RaceInjectingConnection._win_the_race`), closes that gap: the
        connection under test deterministically LOSES the race and
        correctly skips its own backfill, while the racer's real
        `_backfill_type_column()` call is what actually produces the
        correct value -- proving the loser's recovery does not clobber or
        lose the winner's backfilled data.
        """
        db_path = tmp_path / "chunks_deterministic_race_preexisting.db"
        self._seed_pre_migration_database(db_path)

        store = _open_chunk_store_with_deterministic_race(db_path, racer_backfills=True)
        store.close()

        with _real_connection(db_path) as conn:
            row = conn.execute(
                "SELECT type FROM chunks WHERE point_id = ?", (_SAMPLE_POINT_ID,)
            ).fetchone()

        assert row is not None
        assert row[0] == _SAMPLE_RECORD_TYPE
