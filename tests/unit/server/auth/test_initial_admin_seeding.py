"""The initial administrator is seeded only when the user store is empty.

Real stores only: the SQLite user store (``UsersSqliteBackend`` over a real,
migrated ``cidx_server.db``) and the JSON user file.  The PostgreSQL
counterpart lives in
``tests/unit/server/storage/postgres/test_initial_admin_seeding_live_pg.py``.
"""

from __future__ import annotations

import multiprocessing
import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Tuple

import pytest

from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.services.account_data_purge import build_account_data_purger
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import UsersSqliteBackend
from tests.unit.server._account_rows import PASSWORD as OPERATOR_PASSWORD
from tests.unit.server._account_rows import (
    assert_account_keeps_its_rows,
    build_stores,
    seed_account_rows,
)

OPERATOR = "example-operator"

THREAD_SEEDERS = 6
PROCESS_SEEDERS = 4
BARRIER_TIMEOUT_S = 30
THREAD_JOIN_TIMEOUT_S = 60
# Spawned workers need a few seconds to import; they all start at this offset.
PROCESS_START_DELAY_S = 3.0


@pytest.fixture
def sqlite_db(tmp_path: Path) -> str:
    db_path = tmp_path / "data" / "cidx_server.db"
    db_path.parent.mkdir(parents=True)
    DatabaseSchema(str(db_path)).initialize_database()
    return str(db_path)


def _sqlite_users(db_path: str) -> UserManager:
    return UserManager(use_sqlite=True, db_path=db_path)


def test_seed_creates_nothing_when_another_admin_exists_sqlite(
    sqlite_db: str,
) -> None:
    users = _sqlite_users(sqlite_db)
    users.create_user(OPERATOR, OPERATOR_PASSWORD, UserRole.ADMIN)

    _sqlite_users(sqlite_db).seed_initial_admin()

    assert users.get_user("admin") is None
    assert [u.username for u in users.get_all_users()] == [OPERATOR]


def _admin_rows(db_path: str) -> List[Tuple[str, str, str]]:
    conn = sqlite3.connect(db_path)
    try:
        return list(
            conn.execute("SELECT username, password_hash, role FROM users").fetchall()
        )
    finally:
        conn.close()


def test_empty_sqlite_store_is_seeded_exactly_once(sqlite_db: str) -> None:
    users = _sqlite_users(sqlite_db)

    assert users.seed_initial_admin() is True
    assert _sqlite_users(sqlite_db).seed_initial_admin() is False

    rows = _admin_rows(sqlite_db)
    assert [(name, role) for name, _, role in rows] == [("admin", "admin")]
    assert users.authenticate_user("admin", "admin") is not None


def test_existing_admin_is_left_untouched_sqlite(sqlite_db: str) -> None:
    users = _sqlite_users(sqlite_db)
    users.create_user("admin", OPERATOR_PASSWORD, UserRole.ADMIN)
    before = _admin_rows(sqlite_db)

    assert _sqlite_users(sqlite_db).seed_initial_admin() is False

    assert _admin_rows(sqlite_db) == before
    assert users.authenticate_user("admin", OPERATOR_PASSWORD) is not None
    assert users.authenticate_user("admin", "admin") is None


def test_store_with_only_a_normal_user_is_not_seeded_sqlite(sqlite_db: str) -> None:
    users = _sqlite_users(sqlite_db)
    users.create_user(OPERATOR, OPERATOR_PASSWORD, UserRole.NORMAL_USER)

    seeded = users.seed_initial_admin()

    assert [name for name, _, _ in _admin_rows(sqlite_db)] == [OPERATOR]
    assert seeded is False


def _assert_exactly_one_winner(results: List[bool], callers: int) -> None:
    assert sorted(results) == [False] * (callers - 1) + [True]


def test_concurrent_seeders_on_empty_sqlite_store_create_one_admin(
    sqlite_db: str,
) -> None:
    barrier = threading.Barrier(THREAD_SEEDERS)
    results: List[bool] = []
    errors: List[BaseException] = []

    def seed() -> None:
        try:
            manager = _sqlite_users(sqlite_db)
            barrier.wait(timeout=BARRIER_TIMEOUT_S)
            results.append(manager.seed_initial_admin())
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert
            errors.append(exc)

    threads = [threading.Thread(target=seed) for _ in range(THREAD_SEEDERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=THREAD_JOIN_TIMEOUT_S)

    assert not any(t.is_alive() for t in threads), "a seeder thread hung"
    assert errors == []
    _assert_exactly_one_winner(results, THREAD_SEEDERS)
    assert [name for name, _, _ in _admin_rows(sqlite_db)] == ["admin"]


def _create_if_empty_in_process(db_path: str, username: str, start_at: float) -> bool:
    """Spawned worker: one call to the store's atomic conditional create."""
    from code_indexer.server.storage.sqlite_backends import UsersSqliteBackend

    backend = UsersSqliteBackend(db_path)
    time.sleep(max(0.0, start_at - time.time()))  # line the workers up
    return backend.create_user_if_store_empty(username, "example-hash", "admin")


def test_concurrent_processes_create_one_user_on_empty_sqlite_store(
    sqlite_db: str,
) -> None:
    """Distinct names, so only the store's own atomicity keeps it to one."""
    names = [f"example-seed-{i}" for i in range(PROCESS_SEEDERS)]
    start_at = time.time() + PROCESS_START_DELAY_S
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(PROCESS_SEEDERS) as pool:
        results = pool.starmap(
            _create_if_empty_in_process,
            [(sqlite_db, name, start_at) for name in names],
        )

    _assert_exactly_one_winner(results, PROCESS_SEEDERS)
    rows = _admin_rows(sqlite_db)
    assert [name for name, _, _ in rows] == [names[results.index(True)]]


def test_json_store_seeds_only_when_empty(tmp_path: Path) -> None:
    populated = UserManager(users_file_path=str(tmp_path / "populated.json"))
    populated.create_user(OPERATOR, OPERATOR_PASSWORD, UserRole.ADMIN)
    seeded = populated.seed_initial_admin()
    assert populated.get_user("admin") is None
    assert seeded is False

    users = UserManager(users_file_path=str(tmp_path / "users.json"))
    assert users.seed_initial_admin() is True
    assert users.seed_initial_admin() is False
    assert [u.username for u in users.get_all_users()] == ["admin"]


class _StaleEmptinessCheck(UsersSqliteBackend):
    """A seeder whose emptiness check ran just before another's insert."""

    def has_any_user(self) -> bool:
        return False


def test_losing_seeder_keeps_the_seeded_accounts_rows(tmp_path: Path) -> None:
    stores = build_stores(tmp_path)
    assert stores.user_manager.seed_initial_admin() is True
    seeded_rows = seed_account_rows(stores, "admin")
    loser = UserManager(
        storage_backend=_StaleEmptinessCheck(
            str(stores.server_dir / "data" / "cidx_server.db")
        ),
        account_data_purger=build_account_data_purger(
            "sqlite", stores.server_dir, None
        ),
    )

    assert loser.seed_initial_admin() is False

    assert_account_keeps_its_rows(stores, "admin", seeded_rows)
