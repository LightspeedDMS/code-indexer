"""The initial administrator is seeded only when the user store is empty
(PostgreSQL user store, as cluster nodes use it).

Live-PostgreSQL tests on a migrated scratch database
(``migrated_scratch_pg_dsn``, gated by ``TEST_POSTGRES_DSN``; skipped when
unset).  Every node is a real ``UsersPostgresBackend`` on its own pool.
"""

from __future__ import annotations

import threading
from typing import Callable, Iterator, List, Tuple

import pytest

psycopg = pytest.importorskip("psycopg")

from code_indexer.server.auth.user_manager import UserManager, UserRole  # noqa: E402
from code_indexer.server.storage.postgres.connection_pool import (  # noqa: E402
    ConnectionPool,
)
from code_indexer.server.storage.postgres.users_backend import (  # noqa: E402
    UsersPostgresBackend,
)
from tests.unit.server._account_rows import (  # noqa: E402
    PASSWORD as OPERATOR_PASSWORD,
)

OPERATOR = "example-operator"


def _rows(dsn: str) -> List[Tuple[str, str, str]]:
    with psycopg.connect(dsn) as conn:
        return list(
            conn.execute(
                "SELECT username, password_hash, role FROM users ORDER BY username"
            ).fetchall()
        )


@pytest.fixture
def empty_users_dsn(migrated_scratch_pg_dsn: str) -> str:
    with psycopg.connect(migrated_scratch_pg_dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE users CASCADE")
    return migrated_scratch_pg_dsn


@pytest.fixture
def node(empty_users_dsn: str) -> Iterator[UserManager]:
    """One node's user manager over its own pool."""
    pool = ConnectionPool(empty_users_dsn, name="example-seed-node")
    try:
        yield UserManager(storage_backend=UsersPostgresBackend(pool))
    finally:
        pool.close()


def test_seed_creates_nothing_when_another_admin_exists_pg(
    node: UserManager, empty_users_dsn: str
) -> None:
    node.create_user(OPERATOR, OPERATOR_PASSWORD, UserRole.ADMIN)

    seeded = node.seed_initial_admin()

    assert [name for name, _, _ in _rows(empty_users_dsn)] == [OPERATOR]
    assert seeded is False


def test_empty_store_is_seeded_exactly_once_pg(
    node: UserManager, empty_users_dsn: str
) -> None:
    assert node.seed_initial_admin() is True
    assert node.seed_initial_admin() is False

    assert [(n, r) for n, _, r in _rows(empty_users_dsn)] == [("admin", "admin")]
    assert node.authenticate_user("admin", "admin") is not None


def test_existing_admin_is_left_untouched_pg(
    node: UserManager, empty_users_dsn: str
) -> None:
    node.create_user("admin", OPERATOR_PASSWORD, UserRole.ADMIN)
    before = _rows(empty_users_dsn)

    assert node.seed_initial_admin() is False

    assert _rows(empty_users_dsn) == before
    assert node.authenticate_user("admin", "admin") is None


NODES = 6
BARRIER_TIMEOUT_S = 30
JOIN_TIMEOUT_S = 60


def _race(dsn: str, call: Callable[[UsersPostgresBackend, int], bool]) -> List[bool]:
    """Run *call* once per simulated node, each on its own pool, together."""
    pools = [ConnectionPool(dsn, name=f"example-node-{i}") for i in range(NODES)]
    barrier = threading.Barrier(NODES)
    results: List[bool] = []
    errors: List[BaseException] = []

    def run(index: int) -> None:
        try:
            backend = UsersPostgresBackend(pools[index])
            barrier.wait(timeout=BARRIER_TIMEOUT_S)
            results.append(call(backend, index))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(NODES)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=JOIN_TIMEOUT_S)
    finally:
        for pool in pools:
            pool.close()
    assert not any(t.is_alive() for t in threads), "a node thread hung"
    assert errors == []
    assert sorted(results) == [False] * (NODES - 1) + [True]
    return results


def test_concurrent_seeders_on_empty_store_create_one_admin_pg(
    empty_users_dsn: str,
) -> None:
    _race(
        empty_users_dsn,
        lambda backend, _: UserManager(storage_backend=backend).seed_initial_admin(),
    )

    assert [(n, r) for n, _, r in _rows(empty_users_dsn)] == [("admin", "admin")]


def test_concurrent_nodes_create_one_user_on_empty_store_pg(
    empty_users_dsn: str,
) -> None:
    """Distinct names, so only the store's own atomicity keeps it to one."""
    results = _race(
        empty_users_dsn,
        lambda backend, i: backend.create_user_if_store_empty(
            f"example-seed-{i}", "example-hash", "admin"
        ),
    )

    assert len(results) == NODES
    assert len(_rows(empty_users_dsn)) == 1
