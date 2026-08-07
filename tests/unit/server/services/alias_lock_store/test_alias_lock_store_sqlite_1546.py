"""Tests for SqliteAliasLockStore (Issue #1546 Phase 1).

Real SQLite only -- no mocking of the lock mechanism. This module covers
the happy path (acquire/release/renew) and ownership-loss/crash-recovery
scenarios. Concurrency (many-thread and many-process linearizability)
lives in test_alias_lock_store_sqlite_concurrency_1546.py.
"""

from __future__ import annotations

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
    return SqliteAliasLockStore(tmp_path / "alias_locks.db", **kwargs)


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

    def test_different_lock_keys_serialize_on_the_shared_sqlite_file(self, tmp_path):
        """Documented SQLite characteristic (see sqlite_store module
        docstring): a held lock transaction takes the WHOLE file's writer
        lock, not a per-row lock -- so acquiring a DIFFERENT lock_key in
        the SAME alias_locks.db file also fails while any lock is held,
        and only succeeds once that lock is released. This is the
        opposite of the Postgres backend (true per-row locking) and is a
        deliberate, accepted limitation for single-node solo deployments.
        """
        store = _make_store(tmp_path, busy_timeout_seconds=_SHORT_BUSY_TIMEOUT_SECONDS)
        handle_a = store.try_acquire("alias-a", operation="op")
        assert handle_a is not None
        handle_b = None
        try:
            handle_b = store.try_acquire("alias-b", operation="op")
            assert handle_b is None, (
                "expected alias-b's acquire to contend on the shared file "
                "lock while alias-a is held"
            )
        finally:
            if handle_b is not None:
                store.release(handle_b)
            store.release(handle_a)

        # Once alias-a is released, alias-b becomes acquirable.
        handle_b2 = store.try_acquire("alias-b", operation="op")
        assert handle_b2 is not None
        try:
            pass
        finally:
            store.release(handle_b2)


class TestOwnershipLossViaWrongToken:
    """Zero-rows-affected DELETE/UPDATE path: forcing a token mismatch on
    the SAME still-open connection must raise, never silently succeed.
    Because release()/renew() ROLLBACK and close the connection on this
    path (see sqlite_store.py), the underlying uncommitted INSERT is also
    discarded -- the lock becomes free again, which we assert explicitly.
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

    def test_renew_with_wrong_token_raises_and_frees_the_lock(self, tmp_path):
        store = _make_store(tmp_path)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None
        handle.owner_token = "not-the-real-token"

        with pytest.raises(AliasLockOwnershipLostError):
            store.renew(handle)

        new_handle = store.try_acquire("my-alias", operation="op")
        assert new_handle is not None
        try:
            pass
        finally:
            store.release(new_handle)


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


class TestCrashRecovery:
    def test_new_acquisition_succeeds_immediately_after_holder_connection_dies(
        self, tmp_path
    ):
        """No TTL, no wait: killing the holder's connection must free the
        lock right away, and a new acquire must succeed near-instantly."""
        store = _make_store(
            tmp_path, busy_timeout_seconds=_GENEROUS_BUSY_TIMEOUT_SECONDS
        )
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None

        # A competing acquire cannot succeed while the first is alive.
        assert store.try_acquire("my-alias", operation="op") is None

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
