"""Tests for AliasLockStore.is_held() (Issue #1546 Phase 2).

`is_held()` answers "is this lock currently held by ANYONE" -- a live
(non-stale) committed holder row, visible from any store instance or worker
process -- WITHOUT acquiring the lock. Holder identity is available
separately through `get_holder()`.

Real SQLite only in this module -- no mocking. The PostgreSQL backend's
`is_held()` is covered by test_alias_lock_store_postgres_1546.py (skipped
locally without TEST_POSTGRES_DSN, per this suite's existing convention).
"""

from __future__ import annotations

import time
from pathlib import Path

from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)

# Deliberately short so the non-blocking assertion below has a tight,
# meaningful bound: if is_held() ever regressed to wait out a full
# busy_timeout instead of probing with its own near-zero bound, it would
# take at least this long to return -- the elapsed-time assertion catches
# that.
_IS_HELD_BUSY_TIMEOUT_SECONDS = 0.3
_IS_HELD_MAX_ELAPSED_SECONDS = _IS_HELD_BUSY_TIMEOUT_SECONDS / 2


def _make_store(tmp_path: Path, **kwargs) -> SqliteAliasLockStore:
    # Two _make_store() calls against the SAME tmp_path share the same
    # on-disk per-alias files -- the local stand-in for "a second
    # process/worker", matching the sibling concurrency test module.
    return SqliteAliasLockStore(tmp_path / "alias_locks", **kwargs)


class TestIsHeldNotContended:
    def test_returns_false_when_never_acquired(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.is_held("never-touched-alias") is False

    def test_returns_false_after_release(self, tmp_path):
        store = _make_store(tmp_path)
        handle = store.try_acquire("my-alias", operation="op")
        assert handle is not None
        store.release(handle)
        assert store.is_held("my-alias") is False

    def test_reflects_a_different_alias_independently(self, tmp_path):
        store = _make_store(tmp_path)
        handle = store.try_acquire("alias-a", operation="op")
        assert handle is not None
        try:
            assert store.is_held("alias-b") is False
        finally:
            store.release(handle)


class TestIsHeldWhileContended:
    def test_returns_true_from_a_second_store_instance_while_held(self, tmp_path):
        """Cross-instance visibility -- the local stand-in for
        cross-process/cross-worker visibility, since is_locked()'s legacy
        file-based behavior (which is_held() must replicate) is
        cross-process by nature."""
        holder_store = _make_store(tmp_path)
        observer_store = _make_store(tmp_path)
        handle = holder_store.try_acquire("my-alias", operation="op")
        assert handle is not None
        try:
            assert observer_store.is_held("my-alias") is True
        finally:
            holder_store.release(handle)

    def test_probe_does_not_block_on_a_held_writer_transaction(self, tmp_path):
        """is_held() must return PROMPTLY (its own small bound) rather
        than waiting out the full busy_timeout configured for genuine
        acquire attempts -- bounded by an elapsed-time assertion (not
        just "did it return") so a regression to a slow/blocking probe
        fails this test rather than merely running slowly.
        """
        holder_store = _make_store(
            tmp_path, busy_timeout_seconds=_IS_HELD_BUSY_TIMEOUT_SECONDS
        )
        observer_store = _make_store(
            tmp_path, busy_timeout_seconds=_IS_HELD_BUSY_TIMEOUT_SECONDS
        )
        handle = holder_store.try_acquire("my-alias", operation="op")
        assert handle is not None
        try:
            started = time.monotonic()
            result = observer_store.is_held("my-alias")
            elapsed = time.monotonic() - started
            assert result is True
            assert elapsed < _IS_HELD_MAX_ELAPSED_SECONDS, (
                f"is_held() took {elapsed:.3f}s -- it must probe with its "
                f"own small bound, not the full acquire-path busy_timeout"
            )
        finally:
            holder_store.release(handle)
