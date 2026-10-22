"""A lock held by an earlier boot of THIS node is stale.

After a reboot no process of the previous boot exists, so its locks must not
wait out their (up to 24 h) lease. SQLite lock files are node-local: any
holder from a different boot id is stale. PostgreSQL is shared by nodes, and
host names and machine ids are duplicated by cloned VMs and images, so only
the configured cluster node id identifies a node: a holder is an earlier boot
of this node only when it recorded this node's (non-empty) node id and a
different boot id. Without a node id, a row frees only by its lease (or the
death of a same-identity process). Real stores; the recorded holder is
rewritten to stand for the earlier boot.
"""

from __future__ import annotations

import multiprocessing
import os
import socket
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Iterator, List

import pytest

from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)
from tests.shared.alias_lock_pg import postgres_lock_store_dsn

_KEY = "example-repo"
_DAY_SECONDS = 24 * 3600
_OTHER_BOOT = "00000000-0000-0000-0000-000000000000"
_THIS_NODE = "node-a"
_OTHER_NODE = "node-b"

MakeStore = Callable[..., Any]


def _fields() -> List[str]:
    """Host name, kernel boot id and PID namespace, read from the system."""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        namespace = os.readlink("/proc/self/ns/pid")
    except OSError:
        pytest.skip("boot id or PID namespace unreadable on this host")
    return [socket.gethostname(), boot, namespace]


def _earlier_boot(*suffix: str) -> str:
    host, _boot, namespace = _fields()
    return "|".join([host, _OTHER_BOOT, namespace, *suffix])


def _machine_id() -> str:
    try:
        machine = Path("/etc/machine-id").read_text().strip()
    except OSError:
        pytest.skip("machine id unreadable on this host")
    if not machine:
        pytest.skip("empty machine id on this host")
    return machine


def _dead_pid() -> int:
    child = multiprocessing.get_context("fork").Process(target=os._exit, args=(0,))
    child.start()
    child.join(30)
    assert child.pid is not None
    return child.pid


@pytest.fixture
def sqlite_store(tmp_path: Path) -> MakeStore:
    return lambda: SqliteAliasLockStore(tmp_path / "locks")


@pytest.fixture
def pg_store() -> Iterator[MakeStore]:
    from code_indexer.server.services.alias_lock_store.postgres_store import (
        PostgresAliasLockStore,
    )

    with postgres_lock_store_dsn(_KEY) as dsn:

        def make(node_id: str = "") -> Any:
            if node_id:
                return PostgresAliasLockStore(dsn, node_id=node_id)
            return PostgresAliasLockStore(dsn)

        yield make


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


def _successor_after(make: Callable[[], Any], identity: str) -> Any:
    """Acquire with a 24 h lease, rewrite the holder as ``identity`` with a
    dead PID, and return what a second acquirer gets."""
    store = make()
    assert store.try_acquire(_KEY, "refresh_scheduler", ttl_seconds=_DAY_SECONDS)
    _record_holder(store, identity)
    return make().try_acquire(_KEY, "successor")


class TestNodeLocalStore:
    def test_holder_from_an_earlier_boot_is_taken_over(
        self, sqlite_store: MakeStore
    ) -> None:
        assert _successor_after(sqlite_store, _earlier_boot()) is not None


class TestSharedStore:
    def test_cloned_vm_with_same_host_and_machine_id_keeps_its_lease(
        self, pg_store: MakeStore
    ) -> None:
        # ^ why VMs cloned from one template share host name and machine id
        identity = _earlier_boot(_machine_id())
        assert _successor_after(pg_store, identity) is None

    def test_holder_of_another_node_keeps_its_lease(self, pg_store: MakeStore) -> None:
        identity = _earlier_boot(_OTHER_NODE)
        assert _successor_after(lambda: pg_store(_THIS_NODE), identity) is None

    def test_holder_from_an_earlier_boot_of_this_node_is_taken_over(
        self, pg_store: MakeStore
    ) -> None:
        identity = _earlier_boot(_THIS_NODE)
        assert _successor_after(lambda: pg_store(_THIS_NODE), identity) is not None

    def test_no_configured_node_id_proves_nothing(self, pg_store: MakeStore) -> None:
        identity = _earlier_boot(_THIS_NODE)
        assert _successor_after(pg_store, identity) is None

    def test_row_without_a_node_id_keeps_its_lease(self, pg_store: MakeStore) -> None:
        identity = _earlier_boot()
        assert _successor_after(lambda: pg_store(_THIS_NODE), identity) is None
