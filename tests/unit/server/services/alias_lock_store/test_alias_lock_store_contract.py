"""Backend-agnostic contract of the row-owned, leased AliasLockStore.

Every test runs against real SQLite (per-alias files) and real PostgreSQL
(``TEST_POSTGRES_DSN``; skipped when unset). Each ``make_store()`` call is a
NEW store instance over the same backing storage -- the stand-in for another
worker process. No mocks.
"""

from __future__ import annotations

import multiprocessing
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from code_indexer.server.services.alias_lock_store.base import (
    AliasLockOwnershipLostError,
    LockOutcome,
)
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)

MakeStore = Callable[[], Any]
_FOREIGN_HOST = "foreign-host.example.com"
_CHILD_TIMEOUT_SECONDS = 30


@pytest.fixture(params=["sqlite", "postgres"])
def make_store(request, tmp_path: Path) -> Iterator[MakeStore]:
    if request.param == "sqlite":
        lock_dir = tmp_path / "alias_locks"
        yield lambda: SqliteAliasLockStore(lock_dir)
        return
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN not set")
    from code_indexer.server.services.alias_lock_store.postgres_store import (
        PostgresAliasLockStore,
    )

    yield lambda: PostgresAliasLockStore(dsn)


@pytest.fixture
def key() -> str:
    return f"example-repo-{uuid.uuid4().hex[:12]}"


def _rewrite_holder(store: Any, key: str, *, hostname: str, pid: int) -> None:
    """Make the committed row look as if another process wrote it."""
    if isinstance(store, SqliteAliasLockStore):
        import sqlite3

        from code_indexer.server.services.alias_lock_store.sqlite_store import (
            _db_path_for_lock_key,
        )

        conn = sqlite3.connect(str(_db_path_for_lock_key(store._lock_dir, key)))
        with conn:
            conn.execute(
                "UPDATE golden_repo_alias_locks SET hostname = ?, pid = ? "
                "WHERE lock_key = ?",
                (hostname, pid, key),
            )
        conn.close()
        return
    import psycopg

    with psycopg.connect(store._dsn) as pg_conn:
        pg_conn.execute(
            "UPDATE golden_repo_alias_locks SET hostname = %s, pid = %s "
            "WHERE lock_key = %s",
            (hostname, pid, key),
        )


def _dead_pid() -> int:
    ctx = multiprocessing.get_context("fork")
    child = ctx.Process(target=os._exit, args=(0,))
    child.start()
    child.join(_CHILD_TIMEOUT_SECONDS)
    assert child.pid is not None
    return child.pid


class TestAcquireRelease:
    def test_handle_fields_and_explicit_token(self, make_store: MakeStore, key: str):
        store = make_store()
        handle = store.try_acquire(key, operation="add_golden_repo", owner_token="t1")
        assert handle is not None
        assert (handle.lock_key, handle.operation, handle.owner_token) == (
            key,
            "add_golden_repo",
            "t1",
        )
        store.release(handle)

    def test_second_acquire_fails_from_any_store_while_held(
        self, make_store: MakeStore, key: str
    ):
        holder = make_store()
        handle = holder.try_acquire(key, operation="op")
        assert handle is not None
        assert make_store().try_acquire(key, operation="op") is None
        assert make_store().is_held(key) is True
        holder.release(handle)
        assert make_store().is_held(key) is False
        again = make_store().try_acquire(key, operation="op")
        assert again is not None
        again._store.release(again)

    def test_different_keys_are_independent(self, make_store: MakeStore):
        store = make_store()
        a = store.try_acquire(f"example-a-{uuid.uuid4().hex[:8]}", operation="op")
        b = store.try_acquire(f"example-b-{uuid.uuid4().hex[:8]}", operation="op")
        assert a is not None and b is not None
        store.release(a)
        store.release(b)

    def test_release_owned_reports_each_outcome(self, make_store: MakeStore, key: str):
        store = make_store()
        assert store.release_owned(key, "op") is LockOutcome.NOT_HELD
        assert store.try_acquire(key, operation="op", owner_token="t1") is not None
        assert store.release_owned(key, "other") is LockOutcome.OWNER_MISMATCH
        assert (
            store.release_owned(key, "op", owner_token="t2")
            is LockOutcome.OWNER_MISMATCH
        )
        assert make_store().release_owned(key, "op") is LockOutcome.RELEASED


class TestOwnershipLoss:
    def test_wrong_token_release_raises_and_lock_stays_held(
        self, make_store: MakeStore, key: str
    ):
        store = make_store()
        handle = store.try_acquire(key, operation="op")
        assert handle is not None
        real = handle.owner_token
        handle.owner_token = "not-the-real-token"
        with pytest.raises(AliasLockOwnershipLostError):
            store.release(handle)
        assert store.is_held(key) is True
        assert store.release_owned(key, "op", owner_token=real) is LockOutcome.RELEASED

    def test_wrong_token_renew_raises_and_lock_stays_held(
        self, make_store: MakeStore, key: str
    ):
        store = make_store()
        handle = store.try_acquire(key, operation="op")
        assert handle is not None
        real = handle.owner_token
        handle.owner_token = "not-the-real-token"
        with pytest.raises(AliasLockOwnershipLostError):
            store.renew(handle)
        assert store.is_held(key) is True
        handle.owner_token = real
        store.renew(handle)
        store.release(handle)

    def test_release_after_another_worker_released_raises(
        self, make_store: MakeStore, key: str
    ):
        store = make_store()
        handle = store.try_acquire(key, operation="op")
        assert handle is not None
        assert make_store().release_owned(key, "op") is LockOutcome.RELEASED
        with pytest.raises(AliasLockOwnershipLostError):
            store.renew(handle)
        with pytest.raises(AliasLockOwnershipLostError):
            store.release(handle)

    def test_with_block_cleanup_failure_chains_the_body_exception(
        self, make_store: MakeStore, key: str
    ):
        store = make_store()
        handle = store.try_acquire(key, operation="op")
        assert handle is not None

        class _BodyError(Exception):
            pass

        with pytest.raises(AliasLockOwnershipLostError) as exc_info:
            with handle:
                make_store().release_owned(key, "op")
                raise _BodyError("body raised before cleanup also failed")
        assert isinstance(exc_info.value.__context__, _BodyError)


class TestContextManager:
    def test_with_block_releases_on_exit_and_on_exception(
        self, make_store: MakeStore, key: str
    ):
        store = make_store()
        handle = store.try_acquire(key, operation="op")
        assert handle is not None
        with handle:
            assert store.is_held(key)
        assert not store.is_held(key)

        handle = store.try_acquire(key, operation="op")
        assert handle is not None
        with pytest.raises(ValueError):
            with handle:
                raise ValueError("body")
        assert not store.is_held(key)

    def test_entering_released_handle_raises(self, make_store: MakeStore, key: str):
        store = make_store()
        handle = store.try_acquire(key, operation="op")
        assert handle is not None
        store.release(handle)
        with pytest.raises(RuntimeError):
            with handle:
                pass

    def test_release_inside_body_then_exit_is_a_no_op(
        self, make_store: MakeStore, key: str
    ):
        store = make_store()
        handle = store.try_acquire(key, operation="op")
        assert handle is not None
        with handle:
            store.release(handle)

    def test_handle_without_store_raises_on_exit(self, make_store: MakeStore, key: str):
        store = make_store()
        handle = store.try_acquire(key, operation="op")
        assert handle is not None
        handle._store = None
        with pytest.raises(RuntimeError):
            with handle:
                pass
        store.release_owned(key, "op")


class TestLease:
    def test_dead_local_holder_is_taken_over_immediately(
        self, make_store: MakeStore, key: str
    ):
        import socket

        store = make_store()
        assert store.try_acquire(key, operation="op", ttl_seconds=3600) is not None
        _rewrite_holder(store, key, hostname=socket.gethostname(), pid=_dead_pid())

        survivor = make_store()
        assert survivor.is_held(key) is False
        assert survivor.get_holder(key) is None
        taken = survivor.try_acquire(key, operation="successor")
        assert taken is not None
        assert survivor.get_holder(key)["owner"] == "successor"
        survivor.release(taken)

    def test_foreign_host_holder_is_kept_until_its_lease_lapses(
        self, make_store: MakeStore, key: str
    ):
        store = make_store()
        assert store.try_acquire(key, operation="op", ttl_seconds=3600) is not None
        # ^ why a dead pid on a foreign host => liveness is never checked cross-host
        _rewrite_holder(store, key, hostname=_FOREIGN_HOST, pid=_dead_pid())
        assert make_store().is_held(key) is True
        assert make_store().try_acquire(key, operation="successor") is None
        assert store.release_owned(key, "op") is LockOutcome.RELEASED

    def test_renew_never_shortens_the_lease(self, make_store: MakeStore, key: str):
        store = make_store()
        assert store.try_acquire(key, operation="op", ttl_seconds=86400) is not None
        assert store.renew_owned(key, "op", ttl_seconds=60) is LockOutcome.RENEWED
        assert store.get_holder(key)["ttl_seconds"] == 86400
        assert store.renew_owned(key, "op", ttl_seconds=90000) is LockOutcome.RENEWED
        assert store.get_holder(key)["ttl_seconds"] == 90000
        assert store.renew_owned(key, "other") is LockOutcome.OWNER_MISMATCH
        assert store.release_owned(key, "op") is LockOutcome.RELEASED
        assert store.renew_owned(key, "op") is LockOutcome.NOT_HELD

    def test_ttl_must_be_positive(self, make_store: MakeStore, key: str):
        with pytest.raises(ValueError):
            make_store().try_acquire(key, operation="op", ttl_seconds=0)
