"""Tests for SqliteAliasLockStore (Issue #1546 Phase 1).

Real SQLite only -- no mocking of the lock mechanism. This module covers
the happy path (acquire/release/renew) and ownership-loss/crash-recovery
scenarios. Concurrency (many-thread and many-process linearizability)
lives in test_alias_lock_store_sqlite_concurrency_1546.py.
"""

from __future__ import annotations

import glob
import os
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
    `ProgrammingError`. Both release() and renew() must normalize this
    the same way -- never leak the raw sqlite3 exception."""

    def test_release_after_severed_file_descriptor_raises_ownership_lost(
        self, tmp_path
    ):
        store = _make_store(tmp_path)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None

        _sever_raw_file_descriptor(handle._connection)

        with pytest.raises(AliasLockOwnershipLostError):
            store.release(handle)

    def test_renew_after_severed_file_descriptor_raises_ownership_lost(self, tmp_path):
        store = _make_store(tmp_path)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None

        # renew() deliberately never commits (sqlite_store.py's design),
        # so a plain UPDATE is otherwise served entirely from in-memory
        # transaction state and never touches disk -- severing the fd
        # alone (as the release() variant above does) would never
        # manifest here. Force an early dirty-page spill to the WAL
        # file, WITHIN this same still-open transaction, via a tiny
        # page cache plus enough large scratch rows to overflow it --
        # empirically proven to make a SUBSEQUENT statement on this
        # connection genuinely touch disk.
        conn = handle._connection
        conn.execute("PRAGMA cache_size=1")
        conn.execute("CREATE TABLE filler(x)")
        big_value = "x" * 5000
        for _ in range(3000):
            conn.execute("INSERT INTO filler VALUES (?)", (big_value,))

        _sever_raw_file_descriptor(conn)

        with pytest.raises(AliasLockOwnershipLostError):
            store.renew(handle)


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
