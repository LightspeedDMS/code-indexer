"""A live lock holder keeps its lease fresh; a dead one still frees the lock.

Long holders (dependency-map analysis, indexing, migrations) can work far
past their lease. While the holding process lives, its lease is renewed by
its exact token, so the final ownership checkpoint still passes; release
stops the renewal; a holder process that is gone frees the lock. Real
coordinator, real SQLite / PostgreSQL stores, a shortened (test-only) lease.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from code_indexer.global_repos.alias_lock_coordinator import AliasLockCoordinator
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)
from tests.shared.alias_lock_pg import postgres_lock_store_dsn

_ALIAS = "example-repo"
_OWNER = "dependency_map_service"
_LEASE_SECONDS = 4
_OUTLIVE_SECONDS = 3 * _LEASE_SECONDS
_JOIN_TIMEOUT_SECONDS = 30


@pytest.fixture(params=["sqlite", "postgres"])
def make_store(request, tmp_path: Path) -> Iterator[Callable[[], Any]]:
    if request.param == "sqlite":
        yield lambda: SqliteAliasLockStore(tmp_path / "locks")
        return
    from code_indexer.server.services.alias_lock_store.postgres_store import (
        PostgresAliasLockStore,
    )

    with postgres_lock_store_dsn(_ALIAS) as dsn:
        yield lambda: PostgresAliasLockStore(dsn)


def _coordinator(golden: Path, store: Any) -> AliasLockCoordinator:
    return AliasLockCoordinator(
        golden_repos_dir=golden,
        db_backed_enabled_getter=lambda: True,
        store_resolver=lambda: store,
    )


def _work_for(seconds: float) -> None:
    threading.Event().wait(seconds)  # stands in for work outlasting the lease


def _heartbeats() -> list:
    return [
        t for t in threading.enumerate() if t.name.startswith("alias-lock-heartbeat")
    ]


class TestLiveHolderKeepsItsLease:
    def test_work_past_the_lease_still_passes_the_final_checkpoint(
        self, tmp_path: Path, make_store
    ) -> None:
        holder = _coordinator(tmp_path, make_store())
        assert holder.acquire(_ALIAS, _OWNER, ttl_seconds=_LEASE_SECONDS)
        try:
            _work_for(_OUTLIVE_SECONDS)

            assert holder.renew(_ALIAS, _OWNER, ttl_seconds=_LEASE_SECONDS)
            assert not _coordinator(tmp_path, make_store()).acquire(_ALIAS, "other")
        finally:
            holder.release(_ALIAS, _OWNER)

    def test_release_stops_the_renewal(self, tmp_path: Path, make_store) -> None:
        before = set(_heartbeats())
        holder = _coordinator(tmp_path, make_store())
        assert holder.acquire(_ALIAS, _OWNER, ttl_seconds=_LEASE_SECONDS)
        assert set(_heartbeats()) - before
        assert holder.release(_ALIAS, _OWNER)

        assert set(_heartbeats()) - before == set()
        successor = _coordinator(tmp_path, make_store())
        assert successor.acquire(_ALIAS, "other")
        assert successor.release(_ALIAS, "other")


def _hold_and_die(golden: str, make_store: Callable[[], Any]) -> None:
    _coordinator(Path(golden), make_store()).acquire(
        _ALIAS, _OWNER, ttl_seconds=_LEASE_SECONDS
    )
    os._exit(0)


class TestDeadHolderFreesTheLock:
    def test_holder_process_gone_is_taken_over(
        self, tmp_path: Path, make_store
    ) -> None:
        child = multiprocessing.get_context("fork").Process(
            target=_hold_and_die, args=(str(tmp_path), make_store)
        )
        child.start()
        child.join(_JOIN_TIMEOUT_SECONDS)
        assert child.exitcode == 0
        _work_for(_OUTLIVE_SECONDS)

        taker = _coordinator(tmp_path, make_store())
        assert taker.acquire(_ALIAS, "other", ttl_seconds=_LEASE_SECONDS)
        taker.release(_ALIAS, "other")


class TestFailedReleaseStopsTheRenewal:
    def test_release_that_raises_still_lets_the_lease_lapse(
        self, tmp_path: Path, make_store
    ) -> None:
        import sqlite3

        original = make_store()

        class ReleaseFailsOnce(type(original)):  # type: ignore[misc]
            failed = False

            def release_owned(self, *args: Any, **kwargs: Any) -> Any:
                if not self.failed:
                    self.failed = True
                    raise sqlite3.OperationalError("database is locked")
                return super().release_owned(*args, **kwargs)

        # ^ why same constructor argument as the fixture's store: lock dir (SQLite) or DSN
        source = (
            original._lock_dir
            if isinstance(original, SqliteAliasLockStore)
            else original._dsn
        )
        before = set(_heartbeats())
        holder = _coordinator(tmp_path, ReleaseFailsOnce(source))
        assert holder.acquire(_ALIAS, _OWNER, ttl_seconds=_LEASE_SECONDS)

        with pytest.raises(sqlite3.OperationalError):
            holder.release(_ALIAS, _OWNER)

        assert set(_heartbeats()) - before == set()
        _work_for(_OUTLIVE_SECONDS)
        successor = _coordinator(tmp_path, make_store())
        assert successor.acquire(_ALIAS, "other", ttl_seconds=_LEASE_SECONDS)
        assert successor.release(_ALIAS, "other")


class TestWriteModeLeaseLapses:
    def test_write_mode_lock_has_no_heartbeat(self, tmp_path: Path, make_store) -> None:
        from types import SimpleNamespace

        from code_indexer.server.mcp.handlers import files

        # ^ why an abandoned write-mode session must lapse; its file operations renew it
        before = set(_heartbeats())
        locks = _coordinator(tmp_path, make_store())
        acquired, _owner, token = files._write_mode_acquire_lock(
            SimpleNamespace(write_lock_manager=locks), _ALIAS
        )
        try:
            assert acquired
            assert set(_heartbeats()) - before == set()
        finally:
            locks.release(_ALIAS, owner_name="mcp_write_mode", owner_token=token)
