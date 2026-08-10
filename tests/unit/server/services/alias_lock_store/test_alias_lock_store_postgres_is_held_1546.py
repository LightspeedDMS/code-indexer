"""Tests for PostgresAliasLockStore.is_held() (Issue #1546 Phase 2).

Mirrors test_alias_lock_store_is_held_1546.py (the SQLite backend's
suite) for the identical, backend-agnostic contract: `is_held()` is a
bounded, non-destructive contention probe -- never a metadata read, since
an uncommitted row is invisible to any other session under MVCC on
PostgreSQL exactly as it is under SQLite's own transaction isolation.

Skip-gated on TEST_POSTGRES_DSN, matching this suite's established
live-PG test convention. Real PostgreSQL only -- no mocking.
"""

from __future__ import annotations

import time

from code_indexer.server.services.alias_lock_store.postgres_store import (
    PostgresAliasLockStore,
)

from .conftest import postgres_skip_marker

pytestmark = postgres_skip_marker

# Generous relative to the store's default acquire_lock_timeout_seconds
# (0.5s) so the elapsed-time assertion fails loudly (not flakily) if a
# regression reintroduces a full-timeout wait instead of a fast probe.
_IS_HELD_MAX_ELAPSED_SECONDS = 2.0


class TestIsHeldNotContended:
    def test_returns_false_when_never_acquired(self, store, unique_key):
        assert store.is_held(unique_key) is False

    def test_returns_false_after_release(self, store, unique_key):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        store.release(handle)
        assert store.is_held(unique_key) is False

    def test_reflects_a_different_key_independently(self, store, unique_key):
        other_key = f"{unique_key}-other"
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        try:
            assert store.is_held(other_key) is False
        finally:
            store.release(handle)


class TestIsHeldWhileContended:
    def test_returns_true_from_a_second_store_instance_while_held(
        self, store, pg_dsn, unique_key
    ):
        observer_store = PostgresAliasLockStore(pg_dsn)
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        try:
            assert observer_store.is_held(unique_key) is True
        finally:
            store.release(handle)

    def test_probe_does_not_block_for_the_full_acquire_timeout(
        self, store, pg_dsn, unique_key
    ):
        observer_store = PostgresAliasLockStore(pg_dsn)
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        try:
            started = time.monotonic()
            result = observer_store.is_held(unique_key)
            elapsed = time.monotonic() - started
            assert result is True
            assert elapsed < _IS_HELD_MAX_ELAPSED_SECONDS, (
                f"is_held() took {elapsed:.3f}s -- it must probe with a "
                f"bounded timeout, not hang"
            )
        finally:
            store.release(handle)

    def test_probe_never_persists_a_row_when_uncontended(self, store, unique_key):
        """is_held() on a FREE key must roll back its own probe -- a
        second, genuine try_acquire() immediately afterward must still
        succeed (the probe never actually held the lock)."""
        assert store.is_held(unique_key) is False
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        store.release(handle)
