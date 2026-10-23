"""Backend-agnostic contract of the row-owned, leased AliasLockStore.

Every test runs against real SQLite (per-alias files) and real PostgreSQL
(``TEST_POSTGRES_DSN``; skipped when unset). Each ``make_store()`` call is a
NEW store instance over the same backing storage -- the stand-in for another
worker process. No mocks.
"""

from __future__ import annotations

import multiprocessing
import os
import socket
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from code_indexer.server.services.alias_lock_store.base import (
    AliasLockOwnershipLostError,
    LockOutcome,
    local_holder_identity,
)
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)

MakeStore = Callable[[], Any]
_FOREIGN_HOST = "foreign-host.example.com"
_CHILD_TIMEOUT_SECONDS = 30
_LONG_TTL_SECONDS = 3600
_DAY_TTL_SECONDS = 86400


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

    def test_different_keys_are_independent(self, make_store: MakeStore):
        store = make_store()
        a = store.try_acquire(f"example-a-{uuid.uuid4().hex[:8]}", operation="op")
        b = store.try_acquire(f"example-b-{uuid.uuid4().hex[:8]}", operation="op")
        assert a is not None and b is not None
        store.release(a)
        store.release(b)


class TestExactTokenOwnership:
    def test_release_owned_reports_each_outcome(self, make_store: MakeStore, key: str):
        store = make_store()
        assert store.release_owned(key, "op", owner_token="t1") is LockOutcome.NOT_HELD
        assert store.try_acquire(key, operation="op", owner_token="t1") is not None
        assert (
            store.release_owned(key, "other", owner_token="t1")
            is LockOutcome.OWNER_MISMATCH
        )
        assert (
            store.release_owned(key, "op", owner_token="t2")
            is LockOutcome.OWNER_MISMATCH
        )
        assert store.is_held(key) is True
        assert (
            make_store().release_owned(key, "op", owner_token="t1")
            is LockOutcome.RELEASED
        )

    def test_wrong_token_release_and_renew_raise_and_lock_stays_held(
        self, make_store: MakeStore, key: str
    ):
        store = make_store()
        handle = store.try_acquire(key, operation="op")
        assert handle is not None
        real = handle.owner_token
        handle.owner_token = "not-the-real-token"
        with pytest.raises(AliasLockOwnershipLostError):
            store.renew(handle)
        with pytest.raises(AliasLockOwnershipLostError):
            store.release(handle)
        assert store.is_held(key) is True
        assert store.release_owned(key, "op", owner_token=real) is LockOutcome.RELEASED

    def test_release_and_renew_after_another_worker_released_raise(
        self, make_store: MakeStore, key: str
    ):
        store = make_store()
        handle = store.try_acquire(key, operation="op")
        assert handle is not None
        assert (
            make_store().release_owned(key, "op", owner_token=handle.owner_token)
            is LockOutcome.RELEASED
        )
        with pytest.raises(AliasLockOwnershipLostError):
            store.renew(handle)
        with pytest.raises(AliasLockOwnershipLostError):
            store.release(handle)


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

    def test_cleanup_failure_chains_the_body_exception(
        self, make_store: MakeStore, key: str
    ):
        store = make_store()
        handle = store.try_acquire(key, operation="op")
        assert handle is not None

        class _BodyError(Exception):
            pass

        with pytest.raises(AliasLockOwnershipLostError) as exc_info:
            with handle:
                make_store().release_owned(key, "op", owner_token=handle.owner_token)
                raise _BodyError("body raised before cleanup also failed")
        assert isinstance(exc_info.value.__context__, _BodyError)

    def test_released_or_storeless_handles_refuse_the_with_block(
        self, make_store: MakeStore, key: str
    ):
        store = make_store()
        handle = store.try_acquire(key, operation="op")
        assert handle is not None
        with handle:
            store.release(handle)  # early release inside the body: exit no-ops
        with pytest.raises(RuntimeError):
            with handle:
                pass
        storeless = store.try_acquire(key, operation="op")
        assert storeless is not None
        token = storeless.owner_token
        storeless._store = None
        with pytest.raises(RuntimeError):
            with storeless:
                pass
        assert store.release_owned(key, "op", owner_token=token) is LockOutcome.RELEASED


class TestLease:
    def test_dead_holder_with_this_process_identity_is_taken_over(
        self, make_store: MakeStore, key: str
    ):
        store = make_store()
        assert store.try_acquire(key, operation="op", ttl_seconds=_LONG_TTL_SECONDS)
        _rewrite_holder(store, key, hostname=local_holder_identity(), pid=_dead_pid())

        survivor = make_store()
        assert survivor.is_held(key) is False
        assert survivor.get_holder(key) is None
        taken = survivor.try_acquire(key, operation="successor")
        assert taken is not None
        holder = survivor.get_holder(key)
        assert holder is not None and holder["owner"] == "successor"
        survivor.release(taken)

    @pytest.mark.parametrize(
        "identity", [_FOREIGN_HOST, socket.gethostname()], ids=["foreign", "same-name"]
    )
    def test_holder_of_another_identity_is_kept_until_its_lease_lapses(
        self, make_store: MakeStore, key: str, identity: str
    ):
        """A bare host name may be shared by another container or host with
        its own PID namespace: its PIDs are never checked here."""
        store = make_store()
        handle = store.try_acquire(key, operation="op", ttl_seconds=_LONG_TTL_SECONDS)
        assert handle is not None
        _rewrite_holder(store, key, hostname=identity, pid=_dead_pid())
        assert make_store().is_held(key) is True
        assert make_store().try_acquire(key, operation="successor") is None
        store.release(handle)

    def test_renew_never_shortens_the_lease(self, make_store: MakeStore, key: str):
        store = make_store()
        assert store.try_acquire(
            key, operation="op", owner_token="t1", ttl_seconds=_DAY_TTL_SECONDS
        )
        renew = store.renew_owned
        assert renew(key, "op", owner_token="t1", ttl_seconds=60) is LockOutcome.RENEWED
        holder = store.get_holder(key)
        assert holder is not None and holder["ttl_seconds"] == _DAY_TTL_SECONDS
        assert renew(key, "op", owner_token="t1", ttl_seconds=_DAY_TTL_SECONDS + 1)
        holder = store.get_holder(key)
        assert holder is not None and holder["ttl_seconds"] == _DAY_TTL_SECONDS + 1
        assert renew(key, "op", owner_token="t2") is LockOutcome.OWNER_MISMATCH
        assert store.release_owned(key, "op", owner_token="t1") is LockOutcome.RELEASED
        assert renew(key, "op", owner_token="t1") is LockOutcome.NOT_HELD

    def test_ttl_must_be_positive(self, make_store: MakeStore, key: str):
        with pytest.raises(ValueError):
            make_store().try_acquire(key, operation="op", ttl_seconds=0)
