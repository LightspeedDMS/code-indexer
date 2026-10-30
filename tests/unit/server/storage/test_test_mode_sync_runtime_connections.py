from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import List

import pytest

from code_indexer.server.auth.login_rate_limiter import _SqliteStore
from code_indexer.server.storage.database_manager import DatabaseConnectionManager
from tests.unit.server._fast_sqlite import FAST_SQLITE_ENV, _real_connect

_OFF = 0
_FULL = 2


@pytest.fixture(autouse=True)
def _real_sqlite_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    # ^ why real sqlite3.connect, not the conftest's connect_without_fsync; that patch => every case passes
    monkeypatch.setattr(sqlite3, "connect", _real_connect)


def _synchronous(conn: sqlite3.Connection) -> int:
    value: int = conn.execute("PRAGMA synchronous").fetchone()[0]
    return value


def _manager_synchronous(db_path: Path, opens: int = 1) -> List[int]:
    manager = DatabaseConnectionManager(str(db_path))
    seen: List[int] = []

    def probe() -> None:
        for _ in range(opens):
            seen.append(_synchronous(manager.get_connection()))
            manager.close_thread_connection()

    worker = threading.Thread(target=probe)
    worker.start()
    worker.join()
    assert len(seen) == opens
    return seen


def _throttle_synchronous(db_path: Path) -> int:
    store = _SqliteStore(str(db_path))
    return _synchronous(store._thread_connection())


def test_connection_manager_connection_skips_fsync_in_test_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAST_SQLITE_ENV, "1")
    assert _manager_synchronous(tmp_path / "cidx_server.db") == [_OFF]


def test_reopened_manager_connection_also_skips_fsync_in_test_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAST_SQLITE_ENV, "1")
    assert _manager_synchronous(tmp_path / "cidx_server.db", opens=2) == [_OFF, _OFF]


def test_connection_manager_connection_keeps_full_sync_without_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(FAST_SQLITE_ENV, raising=False)
    assert _manager_synchronous(tmp_path / "cidx_server.db") == [_FULL]


def test_login_throttle_connection_skips_fsync_in_test_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAST_SQLITE_ENV, "1")
    assert _throttle_synchronous(tmp_path / "cidx_server.db") == _OFF


def test_login_throttle_connection_keeps_full_sync_without_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(FAST_SQLITE_ENV, raising=False)
    assert _throttle_synchronous(tmp_path / "cidx_server.db") == _FULL


def test_flag_must_be_exactly_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAST_SQLITE_ENV, "true")
    assert _manager_synchronous(tmp_path / "cidx_server.db") == [_FULL]
