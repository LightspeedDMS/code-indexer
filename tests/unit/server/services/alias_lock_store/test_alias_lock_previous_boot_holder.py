"""A lock held by an earlier boot of THIS machine is stale.

After a reboot no process of the previous boot exists, so its locks must not
wait out their (up to 24 h) lease. SQLite lock files are node-local: any
holder from a different boot id is stale. PostgreSQL is shared by machines:
only a holder carrying this machine's id with a different boot id is stale;
another machine's holder, or a row without a machine id, keeps its lease.
Real stores; the recorded holder is rewritten to stand for the earlier boot.
"""

from __future__ import annotations

import multiprocessing
import os
import socket
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Iterator, Tuple

import pytest

from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)
from tests.shared.alias_lock_pg import postgres_lock_store_dsn

_KEY = "example-repo"
_DAY_SECONDS = 24 * 3600
_OTHER_BOOT = "00000000-0000-0000-0000-000000000000"
_OTHER_MACHINE = "0123456789abcdef0123456789abcdef"

Backend = Tuple[str, Callable[[], Any]]


def _fields() -> list:
    """This process's holder identity fields, read from the system itself:
    host name, kernel boot id, PID namespace, machine id."""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        namespace = os.readlink("/proc/self/ns/pid")
        machine = Path("/etc/machine-id").read_text().strip()
    except OSError:
        pytest.skip("boot id, PID namespace or machine id unreadable on this host")
    if not machine:
        pytest.skip("empty machine id on this host")
    return [socket.gethostname(), boot, namespace, machine]


def _previous_boot() -> str:
    host, _boot, namespace, machine = _fields()
    return "|".join([host, _OTHER_BOOT, namespace, machine])


def _foreign_machine() -> str:
    host, _boot, namespace, _machine = _fields()
    return "|".join([host, _OTHER_BOOT, namespace, _OTHER_MACHINE])


def _legacy_previous_boot() -> str:
    host, _boot, namespace, _machine = _fields()
    return "|".join([host, _OTHER_BOOT, namespace])


def _dead_pid() -> int:
    child = multiprocessing.get_context("fork").Process(target=os._exit, args=(0,))
    child.start()
    child.join(30)
    assert child.pid is not None
    return child.pid


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request, tmp_path: Path) -> Iterator[Backend]:
    if request.param == "sqlite":
        yield "sqlite", lambda: SqliteAliasLockStore(tmp_path / "locks")
        return
    from code_indexer.server.services.alias_lock_store.postgres_store import (
        PostgresAliasLockStore,
    )

    with postgres_lock_store_dsn(_KEY) as dsn:
        yield "postgres", lambda: PostgresAliasLockStore(dsn)


def _record_holder(store: Any, identity: str) -> None:
    pid = _dead_pid()
    if isinstance(store, SqliteAliasLockStore):
        with closing(sqlite3.connect(str(store._lock_dir / f"{_KEY}.db"))) as db, db:
            db.execute(
                "UPDATE golden_repo_alias_locks SET hostname = ?, pid = ?",
                (identity, pid),
            )
        return
    import psycopg

    with closing(psycopg.connect(store._dsn)) as pg, pg:
        pg.execute(
            "UPDATE golden_repo_alias_locks SET hostname = %s, pid = %s "
            "WHERE lock_key = %s",
            (identity, pid, _KEY),
        )


def _held_by(make_store: Callable[[], Any], identity: str) -> Any:
    store = make_store()
    assert store.try_acquire(_KEY, "refresh_scheduler", ttl_seconds=_DAY_SECONDS)
    _record_holder(store, identity)
    return make_store().try_acquire(_KEY, "successor")


class TestEarlierBootHolder:
    def test_holder_from_an_earlier_boot_of_this_machine_is_taken_over(
        self, backend: Backend
    ) -> None:
        _kind, make_store = backend
        assert _held_by(make_store, _previous_boot()) is not None

    def test_holder_of_another_machine_keeps_its_lease(self, backend: Backend) -> None:
        kind, make_store = backend
        if kind == "sqlite":
            pytest.skip("SQLite lock files are node-local: no other machine")
        assert _held_by(make_store, _foreign_machine()) is None

    def test_row_without_a_machine_id(self, backend: Backend) -> None:
        kind, make_store = backend
        successor = _held_by(make_store, _legacy_previous_boot())
        if kind == "sqlite":
            assert successor is not None  # node-local: a different boot is enough
        else:
            assert successor is None  # shared: an unproven machine keeps its lease
