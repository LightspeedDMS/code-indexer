"""Tests for PostgresAliasLockStore (Issue #1546 Phase 1).

Skip-gated on TEST_POSTGRES_DSN, matching this project's established
live-PG test convention (see e.g.
tests/unit/server/services/test_bug1235_pg_duplicate_claim_race.py). Real
PostgreSQL only -- no mocking of the lock mechanism.

Uses the REAL golden_repo_alias_locks table migration 043 creates (the
store's own __init__ issues an idempotent CREATE TABLE IF NOT EXISTS
against that exact schema, so these tests validate production reality,
not a throwaway table). Every test uses a fresh UUID-suffixed lock_key so
concurrent test runs against the same shared database never collide, and
every test releases every handle it acquires -- deleting its own row.
Crash/severed-connection tests rely on PostgreSQL's own server-side
rollback of the abandoned session's uncommitted INSERT: no row from any
test ever persists past that test, so no separate teardown/cleanup
fixture is required.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import List, Tuple

import pytest

from code_indexer.server.services.alias_lock_store.base import (
    AliasLockOwnershipLostError,
)
from code_indexer.server.services.alias_lock_store.postgres_store import (
    PostgresAliasLockStore,
)

_GENEROUS_ACQUIRE_TIMEOUT_SECONDS = 5.0
_THREAD_JOIN_TIMEOUT_SECONDS = 60
_LINEARIZABILITY_NUM_THREADS = 6
_LINEARIZABILITY_ACQUISITIONS_PER_THREAD = 15
_LINEARIZABILITY_ATTEMPT_BUDGET_MULTIPLIER = 50
_HOLD_TIME_SCALE_SECONDS = 0.001
_HOLD_TIME_MODULUS = 3
_CRASH_RECOVERY_POLL_SLEEP_SECONDS = 0.05

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set; skipping real-PG alias lock store tests",
)


@pytest.fixture
def store():
    dsn = os.environ["TEST_POSTGRES_DSN"]
    return PostgresAliasLockStore(dsn)


@pytest.fixture
def unique_key():
    """A fresh, test-run-scoped lock_key -- avoids collisions across
    concurrent test runs against the same shared database."""
    return f"test-alias-lock-{uuid.uuid4().hex[:12]}"


def _acquire_until(store, lock_key, timeout_seconds):
    """Poll try_acquire() until it succeeds or timeout_seconds elapses.
    Used only in crash-recovery tests, where PostgreSQL's server-side
    detection of an abandoned session is not guaranteed instantaneous
    from another session's point of view."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        handle = store.try_acquire(lock_key, operation="op")
        if handle is not None:
            return handle
        time.sleep(_CRASH_RECOVERY_POLL_SLEEP_SECONDS)
    return None


class TestAcquireReleaseBasics:
    def test_acquire_returns_handle_with_expected_fields(self, store, unique_key):
        handle = store.try_acquire(unique_key, operation="add_golden_repo")
        assert handle is not None
        try:
            assert handle.lock_key == unique_key
            assert handle.operation == "add_golden_repo"
            assert handle.owner_token
        finally:
            store.release(handle)

    def test_acquire_with_explicit_owner_token_is_honored(self, store, unique_key):
        handle = store.try_acquire(unique_key, operation="op", owner_token="tok-1")
        assert handle is not None
        try:
            assert handle.owner_token == "tok-1"
        finally:
            store.release(handle)

    def test_second_acquire_of_same_key_fails_while_first_is_held(
        self, store, unique_key
    ):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        competitor = None
        try:
            competitor = store.try_acquire(unique_key, operation="op")
            assert competitor is None
        finally:
            if competitor is not None:
                store.release(competitor)
            store.release(handle)


class TestAcquireReleaseCycle:
    def test_after_release_a_new_acquire_of_same_key_succeeds(self, store, unique_key):
        handle1 = store.try_acquire(unique_key, operation="op")
        assert handle1 is not None
        store.release(handle1)

        handle2 = store.try_acquire(unique_key, operation="op")
        assert handle2 is not None
        store.release(handle2)

    def test_renew_succeeds_and_lock_remains_held(self, store, unique_key):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        competitor = None
        try:
            store.renew(handle)  # must not raise, must not release the lock

            competitor = store.try_acquire(unique_key, operation="op")
            assert competitor is None, "renew() must not release the lock"
        finally:
            if competitor is not None:
                store.release(competitor)
            store.release(handle)

    def test_different_lock_keys_are_independent_while_both_held(self, store):
        """Unlike SQLite's whole-file writer lock, PostgreSQL uses true
        per-row locking -- two DIFFERENT lock_keys must be independently
        acquirable at the same time."""
        key_a = f"test-alias-lock-a-{uuid.uuid4().hex[:12]}"
        key_b = f"test-alias-lock-b-{uuid.uuid4().hex[:12]}"
        handle_a = store.try_acquire(key_a, operation="op")
        assert handle_a is not None
        handle_b = None
        try:
            handle_b = store.try_acquire(key_b, operation="op")
            assert handle_b is not None, (
                "expected key_b's acquire to succeed independently of key_a "
                "(true per-row locking)"
            )
        finally:
            if handle_b is not None:
                store.release(handle_b)
            store.release(handle_a)


class TestOwnershipLossViaWrongToken:
    """Zero-rows-affected DELETE/UPDATE path: forcing a token mismatch on
    the SAME still-open connection must raise, never silently succeed.

    Both PostgresAliasLockStore.release() and .renew() call conn.rollback()
    immediately before raising on this path, and release() additionally
    closes the connection in a `finally` (renew() closes it in its own
    failure-path `except`). ROLLBACK discards the ENTIRE transaction,
    including the original (never-committed) INSERT that created the
    lock row -- so after the failure, the lock is genuinely free again,
    which is what these tests assert. This mirrors
    SqliteAliasLockStore's identical, already-verified behavior (see
    test_alias_lock_store_sqlite_1546.py's TestOwnershipLossViaWrongToken).
    """

    def test_release_with_wrong_token_raises_and_frees_the_lock(
        self, store, unique_key
    ):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        handle.owner_token = "not-the-real-token"

        with pytest.raises(AliasLockOwnershipLostError):
            store.release(handle)

        # release()'s failure path rolled back and closed the connection,
        # discarding the original uncommitted INSERT -- the row is gone.
        new_handle = store.try_acquire(unique_key, operation="op")
        assert new_handle is not None
        try:
            pass
        finally:
            store.release(new_handle)

    def test_renew_with_wrong_token_raises_and_frees_the_lock(self, store, unique_key):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        handle.owner_token = "not-the-real-token"

        with pytest.raises(AliasLockOwnershipLostError):
            store.renew(handle)

        # renew()'s failure path rolled back and closed the connection,
        # discarding the original uncommitted INSERT -- the row is gone.
        new_handle = store.try_acquire(unique_key, operation="op")
        assert new_handle is not None
        try:
            pass
        finally:
            store.release(new_handle)


class TestOwnershipLossViaSeveredConnection:
    """Simulate a crash: close the holder's connection out from under it,
    then attempt release()/renew(). Must fail cleanly and loudly rather
    than silently succeeding. There is no store-level pool or shared
    resource to tear down afterward: PostgresAliasLockStore holds no
    persistent connection of its own beyond schema initialization (closed
    immediately in __init__) -- every acquire opens and owns exactly one
    dedicated connection, which in these tests IS the resource being
    (deliberately) severed."""

    def test_release_after_severed_connection_raises_loudly(self, store, unique_key):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        handle._connection.close()  # simulate crash

        with pytest.raises(AliasLockOwnershipLostError):
            store.release(handle)

    def test_renew_after_severed_connection_raises_loudly(self, store, unique_key):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        handle._connection.close()  # simulate crash

        with pytest.raises(AliasLockOwnershipLostError):
            store.renew(handle)


class TestCrashRecovery:
    def test_new_acquisition_succeeds_after_holder_connection_dies(
        self, store, unique_key
    ):
        """No TTL: killing the holder's connection must eventually free
        the lock (PostgreSQL rolls back the abandoned session's
        transaction), and a new acquire must then succeed."""
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None

        assert store.try_acquire(unique_key, operation="op") is None

        handle._connection.close()  # simulate crash

        new_handle = _acquire_until(
            store, unique_key, _GENEROUS_ACQUIRE_TIMEOUT_SECONDS
        )
        assert new_handle is not None, (
            "expected crash recovery to free the lock within "
            f"{_GENEROUS_ACQUIRE_TIMEOUT_SECONDS}s"
        )
        store.release(new_handle)

    def test_uncommitted_insert_never_becomes_visible_after_crash(
        self, store, unique_key
    ):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        handle._connection.close()  # crash before commit -> server-side rollback

        new_handle = _acquire_until(
            store, unique_key, _GENEROUS_ACQUIRE_TIMEOUT_SECONDS
        )
        assert new_handle is not None
        store.release(new_handle)


class TestLinearizabilityThreads:
    def test_many_threads_racing_for_same_key_never_overlap(self, store, unique_key):
        """Many real threads repeatedly race to acquire the SAME lock_key
        against a real PostgreSQL database. Record (start, end) intervals
        for every successful acquire/release cycle and verify NO two
        intervals overlap -- proving mutual exclusion across many trials.
        """
        operation = "add_golden_repo"
        num_threads = _LINEARIZABILITY_NUM_THREADS
        acquisitions_per_thread = _LINEARIZABILITY_ACQUISITIONS_PER_THREAD
        max_attempts_per_thread = (
            acquisitions_per_thread * _LINEARIZABILITY_ATTEMPT_BUDGET_MULTIPLIER
        )

        events: List[Tuple[float, float, int]] = []
        events_lock = threading.Lock()
        errors: List[BaseException] = []

        def worker(thread_id: int) -> None:
            try:
                completed = 0
                attempts = 0
                while completed < acquisitions_per_thread:
                    attempts += 1
                    if attempts > max_attempts_per_thread:
                        raise AssertionError(
                            f"thread {thread_id} exceeded bounded attempt "
                            f"budget ({max_attempts_per_thread})"
                        )
                    handle = store.try_acquire(unique_key, operation=operation)
                    if handle is None:
                        continue
                    try:
                        start = time.monotonic()
                        time.sleep(
                            _HOLD_TIME_SCALE_SECONDS * (thread_id % _HOLD_TIME_MODULUS)
                        )
                        end = time.monotonic()
                    finally:
                        store.release(handle)
                    with events_lock:
                        events.append((start, end, thread_id))
                    completed += 1
            except BaseException as exc:  # noqa: BLE001
                with events_lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
        for t in threads:
            assert not t.is_alive(), "worker thread failed to terminate within timeout"

        assert not errors, f"worker thread(s) raised: {errors!r}"
        assert len(events) == num_threads * acquisitions_per_thread

        events_sorted = sorted(events, key=lambda e: e[0])
        for i in range(1, len(events_sorted)):
            prev_start, prev_end, prev_tid = events_sorted[i - 1]
            cur_start, cur_end, cur_tid = events_sorted[i]
            assert cur_start >= prev_end, (
                f"overlap detected: thread {prev_tid} held [{prev_start:.6f}, "
                f"{prev_end:.6f}] and thread {cur_tid} held [{cur_start:.6f}, "
                f"{cur_end:.6f}] -- mutual exclusion violated"
            )
