"""Tests for SqliteAliasLockStore (Issue #1546 Phase 1).

Real SQLite only -- no mocking of the lock mechanism. This module covers
the happy path (acquire/release/renew) and ownership-loss/crash-recovery
scenarios. Concurrency (many-thread and many-process linearizability)
lives in test_alias_lock_store_sqlite_concurrency_1546.py.
"""

from __future__ import annotations

import glob
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from code_indexer.server.services.alias_lock_store.base import (
    AliasLockOwnershipLostError,
)
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)

_SHORT_BUSY_TIMEOUT_SECONDS = 0.2
_GENEROUS_BUSY_TIMEOUT_SECONDS = 5.0
_CRASH_RECOVERY_MAX_SECONDS = 1.0
_SPILL_CACHE_SIZE_PAGES = 1
_SPILL_ROW_VALUE_SIZE_BYTES = 5000
_SPILL_ROW_COUNT = 3000


def _make_store(tmp_path: Path, **kwargs) -> SqliteAliasLockStore:
    """Fix #2: the store now takes a lock DIRECTORY (one dedicated file
    per alias underneath it), never a single shared file path."""
    return SqliteAliasLockStore(tmp_path / "alias_locks", **kwargs)


def _sever_raw_file_descriptor(conn) -> None:
    """Fix #4 fault injection: close the OS-level file descriptor(s)
    sqlite3 opened for `conn`'s database file, out from under the still
    -open Python sqlite3.Connection object. Empirically proven (see
    Issue #1546 Phase 1 rework investigation) to leave subsequent reads
    served from the page cache (no error), but a COMMIT that must
    actually touch disk raises a genuine `sqlite3.OperationalError:
    disk I/O error` -- while the Python-level connection object is
    NEVER marked closed (sqlite3.Connection has no `.closed` attribute
    at all), unlike an explicit `.close()` call. This is the SQLite
    analogue of PostgreSQL's `pg_terminate_backend` server-side
    disconnect used in the postgres test module.
    """
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    # WAL mode (this store's default) writes COMMIT data to a SEPARATE
    # "-wal" file over its own file descriptor -- severing only the main
    # database file's fd leaves the WAL fd untouched, and COMMIT would
    # then succeed via that fd, never reproducing the fault. Sever all
    # three fds real SQLite WAL-mode files can have open.
    targets = {db_path, f"{db_path}-wal", f"{db_path}-shm"}
    for fd_path in glob.glob("/proc/self/fd/*"):
        try:
            target = os.readlink(fd_path)
        except OSError:
            continue
        if target in targets:
            os.close(int(os.path.basename(fd_path)))


class TestAcquireReleaseBasics:
    def test_acquire_returns_handle_with_expected_fields(self, tmp_path):
        store = _make_store(tmp_path)
        handle = store.try_acquire("my-alias", operation="add_golden_repo")
        assert handle is not None
        try:
            assert handle.lock_key == "my-alias"
            assert handle.operation == "add_golden_repo"
            assert handle.owner_token
        finally:
            store.release(handle)

    def test_acquire_with_explicit_owner_token_is_honored(self, tmp_path):
        store = _make_store(tmp_path)
        handle = store.try_acquire("my-alias", operation="op", owner_token="tok-1")
        assert handle is not None
        try:
            assert handle.owner_token == "tok-1"
        finally:
            store.release(handle)

    def test_second_acquire_of_same_key_fails_while_first_is_held(self, tmp_path):
        store = _make_store(tmp_path, busy_timeout_seconds=_SHORT_BUSY_TIMEOUT_SECONDS)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None
        competitor = None
        try:
            competitor = store.try_acquire("my-alias", operation="op")
            assert competitor is None
        finally:
            if competitor is not None:
                store.release(competitor)
            store.release(handle)


class TestAcquireReleaseCycle:
    def test_after_release_a_new_acquire_of_same_key_succeeds(self, tmp_path):
        store = _make_store(tmp_path)
        handle1 = store.try_acquire("my-alias", operation="op")
        assert handle1 is not None
        try:
            pass
        finally:
            store.release(handle1)

        handle2 = store.try_acquire("my-alias", operation="op")
        assert handle2 is not None
        try:
            pass
        finally:
            store.release(handle2)

    def test_renew_succeeds_and_lock_remains_held(self, tmp_path):
        store = _make_store(tmp_path, busy_timeout_seconds=_SHORT_BUSY_TIMEOUT_SECONDS)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None
        competitor = None
        try:
            store.renew(handle)  # must not raise, must not release the lock

            competitor = store.try_acquire("my-alias", operation="op")
            assert competitor is None, "renew() must not release the lock"
        finally:
            if competitor is not None:
                store.release(competitor)
            store.release(handle)

    def test_different_lock_keys_are_independent_while_both_held(self, tmp_path):
        """Fix #2: each alias now lives in its OWN dedicated SQLite
        file, so acquiring alias-a must NEVER make alias-b's acquire
        fail -- unlike the retired single-shared-file design (whole
        -file writer lock), where this was a false-negative correctness
        bug: alias-b would report "held" despite never being touched."""
        store = _make_store(tmp_path, busy_timeout_seconds=_SHORT_BUSY_TIMEOUT_SECONDS)
        handle_a = store.try_acquire("alias-a", operation="op")
        assert handle_a is not None
        try:
            handle_b = None
            try:
                handle_b = store.try_acquire("alias-b", operation="op")
                assert handle_b is not None, (
                    "expected alias-b's acquire to succeed independently "
                    "of alias-a (per-alias dedicated files)"
                )
            finally:
                if handle_b is not None:
                    store.release(handle_b)
        finally:
            store.release(handle_a)


class TestContextManagerProtocol:
    """Escalated by round-3 review: F2's vacuum-pinning cost means a
    missed `finally` doesn't just leak a connection -- it leaks an OPEN
    WRITE TRANSACTION pinning vacuum indefinitely. `with handle:` now
    releases the lock on exit, so Phase 2's ~8 real call sites don't
    each need to hand-roll their own try/finally."""

    def test_with_block_releases_the_lock_on_normal_exit(self, tmp_path):
        store = _make_store(tmp_path)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None
        with handle:
            competitor = store.try_acquire("my-alias", operation="op")
            assert competitor is None, "lock must still be held inside the with block"

        new_handle = store.try_acquire("my-alias", operation="op")
        try:
            assert new_handle is not None, "with block exit must have released the lock"
        finally:
            if new_handle is not None:
                store.release(new_handle)

    def test_with_block_releases_the_lock_even_on_exception(self, tmp_path):
        store = _make_store(tmp_path)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None

        class _ProbeError(Exception):
            pass

        with pytest.raises(_ProbeError):
            with handle:
                raise _ProbeError("body raised -- must still release on exit")

        new_handle = store.try_acquire("my-alias", operation="op")
        try:
            assert new_handle is not None, (
                "with block must release the lock even when its body raises"
            )
        finally:
            if new_handle is not None:
                store.release(new_handle)

    def test_with_block_entry_raises_when_already_released(self, tmp_path):
        """Round-5 review fix (Finding #4): entering `with handle:` on a
        handle ALREADY released before entry must raise loudly, never
        silently run the body with no lock actually held.
        `store.try_acquire(...); store.release(handle); with handle:
        mutate_filesystem()` must never look like a safe, lock-protected
        operation."""
        store = _make_store(tmp_path)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None
        store.release(handle)

        with pytest.raises(RuntimeError):
            with handle:
                pass  # must never reach here

    def test_with_block_body_release_then_exit_is_a_clean_no_op(self, tmp_path):
        """The LEGITIMATE no-op case round-4's fix targeted: entering a
        still-LIVE handle, calling release() explicitly INSIDE the
        body, then exiting normally -- __exit__ must see
        _released=True (set by the body's own release() call) and skip
        cleanly rather than attempting a doomed second release that
        would raise AliasLockOwnershipLostError and mask nothing (the
        body itself raised no exception here)."""
        store = _make_store(tmp_path)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None

        with handle:
            store.release(handle)  # legitimate early release from inside the body
        # __exit__ must have no-op'd cleanly -- no exception escaped.

    def test_with_block_raises_loudly_when_store_is_unset(self, tmp_path):
        """Round-4 review fix: a handle with no `_store` reference (never
        constructed by try_acquire()) must raise loudly on `with
        handle:` exit rather than silently doing nothing -- this
        project's anti-silent-failure standard."""
        store = _make_store(tmp_path)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None
        try:
            handle._store = None  # simulate a handle built without try_acquire()
            with pytest.raises(RuntimeError):
                with handle:
                    pass
        finally:
            store.release(handle)

    def test_with_block_cleanup_failure_chains_the_original_body_exception(
        self, tmp_path
    ):
        """Round-4 review fix: if the `with` body raises AND the cleanup
        release() ALSO fails (a genuine severed-connection scenario, not
        a double-release), the cleanup exception must chain the body's
        original exception via `__context__` rather than silently
        replacing it -- Python's own with-statement machinery does this
        automatically, but this proves it holds for THIS __exit__."""
        store = _make_store(tmp_path)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None

        class _BodyError(Exception):
            pass

        with pytest.raises(AliasLockOwnershipLostError) as exc_info:
            with handle:
                handle._connection.close()  # sever it -- cleanup's release() will fail
                raise _BodyError("body raised before cleanup also failed")

        chain = []
        current = exc_info.value.__context__
        while current is not None and current not in chain:
            chain.append(current)
            current = current.__context__
        assert any(isinstance(exc, _BodyError) for exc in chain), (
            "the cleanup exception must chain the body's original exception "
            f"somewhere in __context__, got chain={chain!r}"
        )


class TestOwnershipLossViaWrongToken:
    """Zero-rows-affected DELETE/UPDATE path: forcing a token mismatch on
    the SAME still-open connection must raise, never silently succeed.

    release() with a wrong token still rolls back and closes the
    connection -- this is release()'s existing, accepted terminal
    behavior. renew() is different (Fix #3): see
    TestRenewNeverReleasesTheLockOnFailure below, which supersedes the
    old (incorrect) "renew with wrong token frees the lock" expectation.
    """

    def test_release_with_wrong_token_raises_and_frees_the_lock(self, tmp_path):
        store = _make_store(tmp_path)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None
        handle.owner_token = "not-the-real-token"

        with pytest.raises(AliasLockOwnershipLostError):
            store.release(handle)

        # The connection was closed (rolled back) by release()'s failure
        # path, so the original uncommitted lock row is gone too.
        new_handle = store.try_acquire("my-alias", operation="op")
        assert new_handle is not None
        try:
            pass
        finally:
            store.release(new_handle)


class TestRenewNeverReleasesTheLockOnFailure:
    """Fix #3: renew() is a diagnostic-only checkpoint (Phase 2 will use
    it as an ownership-loss checkpoint during long-running operations).
    A failed renew() -- e.g. a caller passing a stale/incorrect token by
    mistake -- must raise WITHOUT releasing the underlying transaction:
    the lock stays held, and only the renew-scoped diagnostic UPDATE
    fails. Releasing the real lock on a bad renew() call would recreate
    exactly the "successor takes over while the original holder is still
    active" hazard this whole story exists to eliminate.

    This supersedes the old (incorrect)
    ``test_renew_with_wrong_token_raises_and_frees_the_lock`` expectation.
    """

    def test_renew_with_wrong_token_raises_but_lock_remains_held(self, tmp_path):
        store = _make_store(tmp_path, busy_timeout_seconds=_SHORT_BUSY_TIMEOUT_SECONDS)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None
        try:
            real_token = handle.owner_token
            handle.owner_token = "not-the-real-token"

            with pytest.raises(AliasLockOwnershipLostError):
                store.renew(handle)

            # The lock must STILL be held: a competing acquire of the
            # same key must fail, proving the original transaction was
            # never rolled back by the failed renew() call.
            competitor = store.try_acquire("my-alias", operation="op")
            try:
                assert competitor is None, (
                    "renew() with a wrong token must NOT release the "
                    "lock -- the original transaction must remain open"
                )
            finally:
                if competitor is not None:
                    store.release(competitor)

            # Restore the real token so the outer finally can release
            # cleanly, proving the connection/transaction is still fully
            # usable after the failed renew() call.
            handle.owner_token = real_token
        finally:
            store.release(handle)


class TestOwnershipLossViaSeveredConnection:
    """Simulate a crash: close the holder's connection out from under it,
    then attempt release()/renew(). Must fail cleanly and loudly rather
    than silently succeeding. No further release is possible or needed
    here: the connection is already closed, and that IS the ownership
    -loss condition under test."""

    def test_release_after_severed_connection_raises_loudly(self, tmp_path):
        store = _make_store(tmp_path)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None
        handle._connection.close()  # simulate crash

        with pytest.raises(AliasLockOwnershipLostError):
            store.release(handle)

    def test_renew_after_severed_connection_raises_loudly(self, tmp_path):
        store = _make_store(tmp_path)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None
        handle._connection.close()  # simulate crash

        with pytest.raises(AliasLockOwnershipLostError):
            store.renew(handle)


class TestOwnershipLossViaSeveredFileDescriptor:
    """Fix #4: the ownership-loss detection in the previous class relies
    on an EXPLICIT ``.close()`` call, which raises `sqlite3
    .ProgrammingError` on the next statement -- the narrow case the
    original code already handled. A REAL "connection is dead" failure
    (e.g. the underlying file descriptor is severed by the OS) is a
    DIFFERENT failure mode: `sqlite3.Connection` has no `.closed`
    attribute to pre-check at all, and the resulting exception on the
    next statement that actually touches disk is `sqlite3
    .OperationalError` (a different exception class), never
    `ProgrammingError`. release() -- which always closes the connection
    in its own terminal `finally` regardless of outcome -- still
    normalizes this to AliasLockOwnershipLostError. renew() does NOT
    (see F1, TestRenewPropagatesGenuineErrorsWithoutReleasing below):
    round-1 tried to normalize this the same way for renew() too, but
    doing so required calling conn.close() to detect/report it, which
    is itself what released the lock -- exactly the regression Fix F1
    exists to prevent."""

    def test_release_after_severed_file_descriptor_raises_ownership_lost(
        self, tmp_path
    ):
        store = _make_store(tmp_path)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None

        _sever_raw_file_descriptor(handle._connection)

        with pytest.raises(AliasLockOwnershipLostError):
            store.release(handle)


class TestRenewPropagatesGenuineErrorsWithoutReleasing:
    """Fix F1 (round-3 review, CRITICAL): renew() must NEVER call
    conn.close()/rollback() on any path, including a genuine
    statement-execution failure -- doing so (round-1's Fix #4) IS what
    silently released the lock through renew()'s error path while the
    real holder was still running. A genuine disk I/O error propagates
    as the RAW sqlite3.OperationalError (never wrapped into
    AliasLockOwnershipLostError, since renew() no longer attempts any
    classification at all), and -- the actually load-bearing assertion
    -- the lock must still be held afterward: a competing acquire must
    still fail."""

    def test_renew_propagates_the_raw_error_and_leaves_the_lock_held(self, tmp_path):
        store = _make_store(tmp_path, busy_timeout_seconds=_SHORT_BUSY_TIMEOUT_SECONDS)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None
        try:
            # renew() deliberately never commits (sqlite_store.py's
            # design), so a plain UPDATE is otherwise served entirely
            # from in-memory transaction state and never touches disk
            # -- severing the fd alone (as release()'s variant does)
            # would never manifest here. Force an early dirty-page
            # spill to the WAL file, WITHIN this same still-open
            # transaction, via a tiny page cache plus enough large
            # scratch rows to overflow it -- empirically proven to
            # make a SUBSEQUENT statement on this connection genuinely
            # touch disk.
            conn = handle._connection
            conn.execute(f"PRAGMA cache_size={_SPILL_CACHE_SIZE_PAGES}")
            conn.execute("CREATE TABLE filler(x)")
            big_value = "x" * _SPILL_ROW_VALUE_SIZE_BYTES
            for _ in range(_SPILL_ROW_COUNT):
                conn.execute("INSERT INTO filler VALUES (?)", (big_value,))

            _sever_raw_file_descriptor(conn)

            with pytest.raises(sqlite3.OperationalError):
                store.renew(handle)

            # F1's load-bearing assertion: the failed renew() must NOT
            # have touched the connection -- the original transaction
            # (and therefore the lock) must still be held.
            competitor = store.try_acquire("my-alias", operation="op")
            try:
                assert competitor is None, (
                    "renew() must NEVER release the lock on a genuine "
                    "statement-execution error -- F1 regression"
                )
            finally:
                if competitor is not None:
                    store.release(competitor)
        finally:
            # The connection is unusable after a disk I/O error, but it
            # was never explicitly closed by renew() (that's the whole
            # point of this test) -- close it directly here as test
            # cleanup only, never via store.release() (which would
            # itself hit the same severed fd and could mask what this
            # test is proving).
            handle._connection.close()


class TestCrashRecovery:
    def test_new_acquisition_succeeds_immediately_after_holder_connection_dies(
        self, tmp_path
    ):
        """No TTL, no wait: killing the holder's connection must free
        the lock right away, and a new acquire must succeed near-instantly."""
        store = _make_store(
            tmp_path, busy_timeout_seconds=_GENEROUS_BUSY_TIMEOUT_SECONDS
        )
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None
        try:
            # A competing acquire cannot succeed while the first is alive.
            competitor = store.try_acquire("my-alias", operation="op")
            try:
                assert competitor is None
            finally:
                if competitor is not None:
                    store.release(competitor)
        finally:
            # Simulate a crash: close the connection without releasing.
            handle._connection.close()

        started = time.monotonic()
        new_handle = store.try_acquire("my-alias", operation="op")
        elapsed = time.monotonic() - started
        assert new_handle is not None
        try:
            assert elapsed < _CRASH_RECOVERY_MAX_SECONDS, (
                f"crash recovery took {elapsed:.3f}s -- expected near-instant, "
                f"no TTL wait"
            )
        finally:
            store.release(new_handle)

    def test_uncommitted_insert_never_becomes_visible_after_crash(self, tmp_path):
        """try_acquire()'s INSERT is never committed while the lock is
        held (the transaction stays open by design -- see sqlite_store.py
        module docstring). Closing the connection without calling
        release() therefore triggers SQLite's automatic ROLLBACK of that
        uncommitted transaction: the row must never persist for a future
        acquirer to see."""
        store = _make_store(tmp_path)
        handle = store.try_acquire("ghost-alias", operation="op")
        assert handle is not None
        handle._connection.close()  # crash before commit -> auto rollback

        # A fresh acquire must succeed -- the uncommitted row must be gone.
        new_handle = store.try_acquire("ghost-alias", operation="op")
        assert new_handle is not None
        try:
            pass
        finally:
            store.release(new_handle)


def _run_on_thread_and_collect_outcome(action, timeout_seconds: float):
    """Run `action()` (a zero-arg callable) on a background thread and
    return either "ok" (action returned normally) or the exception it
    raised -- extracted so the cross-thread tests below stay under the
    per-method line budget. Fails loudly if the thread never finishes
    or never reports any outcome at all."""
    result_queue: "queue.Queue" = queue.Queue()

    def worker() -> None:
        try:
            action()
            result_queue.put("ok")
        except Exception as exc:  # noqa: BLE001
            result_queue.put(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        raise AssertionError("cross-thread worker never finished")
    try:
        return result_queue.get_nowait()
    except queue.Empty as exc:
        raise AssertionError("cross-thread worker reported no outcome at all") from exc


class TestCrossThreadAcquireAndRelease:
    """Round-5 review NEW CRITICAL fix (Opus): the realistic Phase 2
    call-site shape is acquire-on-one-thread, release/renew-on-a
    -different-thread (background workers, schedulers, job queues all
    use threads). Without `check_same_thread=False` this used to
    permanently wedge the alias -- reproduced empirically before this
    fix: the cross-thread release() raised a misclassified
    AliasLockOwnershipLostError BEFORE conn.close() could run, leaving
    the BEGIN IMMEDIATE transaction open forever with no
    reaper/TTL/recovery mechanism able to free it."""

    def test_release_on_a_different_thread_than_acquire_succeeds(self, tmp_path):
        store = _make_store(tmp_path)
        handle = store.try_acquire("cross-thread-alias", operation="op")
        assert handle is not None
        new_handle = None
        try:
            outcome = _run_on_thread_and_collect_outcome(
                lambda: store.release(handle), _GENEROUS_BUSY_TIMEOUT_SECONDS
            )
            assert outcome == "ok", (
                f"cross-thread release() must succeed cleanly, got: {outcome!r}"
            )

            new_handle = store.try_acquire("cross-thread-alias", operation="op")
            assert new_handle is not None, (
                "cross-thread release() must have actually freed the lock "
                "-- the alias must never be permanently wedged"
            )
        finally:
            if new_handle is not None:
                store.release(new_handle)
            elif not handle._released:
                store.release(handle)

    def test_renew_on_a_different_thread_than_acquire_succeeds(self, tmp_path):
        store = _make_store(tmp_path, busy_timeout_seconds=_SHORT_BUSY_TIMEOUT_SECONDS)
        handle = store.try_acquire("cross-thread-renew-alias", operation="op")
        assert handle is not None
        competitor = None
        try:
            outcome = _run_on_thread_and_collect_outcome(
                lambda: store.renew(handle), _GENEROUS_BUSY_TIMEOUT_SECONDS
            )
            assert outcome == "ok", (
                f"cross-thread renew() must succeed cleanly, got: {outcome!r}"
            )

            competitor = store.try_acquire("cross-thread-renew-alias", operation="op")
            assert competitor is None, (
                "cross-thread renew() must never release the lock"
            )
        finally:
            if competitor is not None:
                store.release(competitor)
            if not handle._released:
                store.release(handle)
