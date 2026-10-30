import _thread
import inspect
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Callable, List

import pytest

from code_indexer.server.storage import database_manager
from code_indexer.server.storage.database_manager import DatabaseConnectionManager

WAIT_SECONDS = 10.0


@pytest.fixture
def manager(tmp_path: Path):
    mgr = DatabaseConnectionManager.get_instance(str(tmp_path / "owned.db"))
    yield mgr
    mgr.close_all()


def _run_owner_then(
    start: Callable[[Callable[[], None]], None],
    mgr: DatabaseConnectionManager,
    between: Callable[[], None],
) -> List[str]:
    opened = threading.Event()
    proceed = threading.Event()
    finished = threading.Event()
    outcome: List[str] = []

    def owner() -> None:
        try:
            mgr.get_connection()
            opened.set()
            assert proceed.wait(WAIT_SECONDS)
            outcome.append(repr(mgr.get_connection().execute("SELECT 1").fetchone()))
        except BaseException as exc:  # noqa: BLE001
            outcome.append(f"{type(exc).__name__}: {exc}")
        finally:
            finished.set()

    start(owner)
    assert opened.wait(WAIT_SECONDS)
    try:
        between()
    finally:
        proceed.set()
    assert finished.wait(WAIT_SECONDS)
    return outcome


def _start_unregistered(fn: Callable[[], None]) -> None:
    _thread.start_new_thread(fn, ())


def test_cleanup_closes_connection_of_exited_thread(manager) -> None:
    opened: List[sqlite3.Connection] = []
    worker = threading.Thread(target=lambda: opened.append(manager.get_connection()))
    worker.start()
    worker.join(WAIT_SECONDS)
    assert not worker.is_alive() and len(opened) == 1

    manager._cleanup_stale_connections()

    assert opened[0] not in manager._connections.values()
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def _open_fds_for(path: Path) -> int:
    fd_dir = Path("/proc/self/fd")
    count = 0
    for entry in fd_dir.iterdir():
        try:
            if os.readlink(entry) == str(path):
                count += 1
        except OSError:
            continue
    return count


def test_close_all_leaves_live_connection_open_until_owner_exits(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "close_all_owner.db"
    mgr = DatabaseConnectionManager.get_instance(str(db_path))
    fds_while_owner_held: List[int] = []

    def close_all_from_this_thread() -> None:
        mgr.close_all()
        fds_while_owner_held.append(_open_fds_for(db_path))

    worker_box: List[threading.Thread] = []

    def start_registered(fn: Callable[[], None]) -> None:
        worker_box.append(threading.Thread(target=fn))
        worker_box[0].start()

    outcome = _run_owner_then(start_registered, mgr, close_all_from_this_thread)
    worker_box[0].join(WAIT_SECONDS)

    assert outcome == ["(1,)"]
    assert fds_while_owner_held == [1]
    assert _open_fds_for(db_path) == 0


def test_foreign_thread_use_logs_one_error_naming_call_site(manager, caplog) -> None:
    opened: List[sqlite3.Connection] = []
    owner = threading.Thread(target=lambda: opened.append(manager.get_connection()))
    owner.start()
    owner.join(WAIT_SECONDS)
    owner_tid = owner.ident

    caplog.set_level(logging.ERROR, logger=database_manager.__name__)
    results = [opened[0].execute("SELECT 1").fetchone() for _ in range(3)]
    frame = inspect.currentframe()
    assert frame is not None
    expected_line = frame.f_lineno - 3

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert results == [(1,)] * 3
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert str(owner_tid) in message
    assert str(threading.get_ident()) in message
    assert f"{__file__}:{expected_line}" in message
    assert errors[0].stack_info


@pytest.fixture
def foreign_errors(caplog):
    # ^ hazard reported sites are process-wide => clear them, or an earlier test's site hides this one
    database_manager._foreign_use_sites.clear()
    caplog.set_level(logging.ERROR, logger=database_manager.__name__)
    return lambda: [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "used by thread" in r.getMessage()
    ]


def _on_exited_thread(fn: Callable[[], object]) -> object:
    box: List[object] = []
    worker = threading.Thread(target=lambda: box.append(fn()))
    worker.start()
    worker.join(WAIT_SECONDS)
    assert not worker.is_alive() and len(box) == 1
    return box[0]


def _caller_line() -> int:
    frame = inspect.currentframe()
    assert frame is not None and frame.f_back is not None
    return frame.f_back.f_lineno


def _reported_sites(records) -> set:
    return {(r.args[2], r.args[3]) for r in records}


def test_owner_cursor_used_on_another_thread_is_reported(
    manager, foreign_errors
) -> None:
    cursor = _on_exited_thread(lambda: manager.get_connection().cursor())
    assert isinstance(cursor, sqlite3.Cursor)

    cursor.execute("SELECT 1")
    execute_line = _caller_line() - 1
    row = cursor.fetchone()
    fetch_line = _caller_line() - 1

    assert row == (1,)
    assert _reported_sites(foreign_errors()) == {
        (__file__, execute_line),
        (__file__, fetch_line),
    }


def test_context_manager_transaction_on_another_thread_is_reported(
    manager, foreign_errors
) -> None:
    conn = _on_exited_thread(manager.get_connection)
    assert isinstance(conn, sqlite3.Connection)

    with conn:
        pass

    sites = _reported_sites(foreign_errors())
    assert sites
    assert {path for path, _ in sites} == {__file__}


MAX_REUSE_ATTEMPTS = 200


def test_new_thread_reusing_owner_thread_id_is_reported(
    manager, foreign_errors
) -> None:
    opened: List[sqlite3.Connection] = []
    owner = threading.Thread(target=lambda: opened.append(manager.get_connection()))
    owner.start()
    owner.join(WAIT_SECONDS)
    owner_ident = owner.ident
    results: List[object] = []

    def use_if_reused() -> None:
        if threading.get_ident() == owner_ident:
            results.append(opened[0].execute("SELECT 1").fetchone())

    for _ in range(MAX_REUSE_ATTEMPTS):
        candidate = threading.Thread(target=use_if_reused)
        candidate.start()
        candidate.join(WAIT_SECONDS)
        if results:
            break
    if not results:
        pytest.skip("platform did not reuse the owner's thread id")

    assert results == [(1,)]
    assert len(foreign_errors()) == 1


def test_cleanup_keeps_live_unregistered_thread_connection(manager) -> None:
    outcome = _run_owner_then(
        _start_unregistered, manager, manager._cleanup_stale_connections
    )

    assert outcome == ["(1,)"]


@pytest.mark.parametrize(
    "method, sql, params",
    [
        ("execute", "SELECT id FROM t WHERE id = ?", ("a",)),
        ("executemany", "INSERT INTO t (id) VALUES (?)", [("b",)]),
    ],
)
def test_failed_statement_stays_failed_while_its_error_is_retained(
    manager, method: str, sql: str, params: object
) -> None:
    conn = manager.get_connection()
    conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO t VALUES ('a')")
    conn.commit()
    if method == "execute":
        conn.execute(sql, params).fetchone()
    else:
        conn.executemany(sql, params)
        conn.rollback()
    with sqlite3.connect(manager.db_path) as other:
        other.execute("ALTER TABLE t RENAME TO t_away")

    retained: List[BaseException] = []
    outcomes: List[str] = []
    for _ in range(3):
        try:
            getattr(conn, method)(sql, params)
            outcomes.append("no error")
        except sqlite3.OperationalError as exc:
            retained.append(exc)
            outcomes.append("raised")

    assert outcomes == ["raised"] * 3
