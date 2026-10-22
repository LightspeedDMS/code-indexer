"""SQLite-specific tests for SqliteAliasLockStore.

The backend-agnostic contract lives in ``test_alias_lock_store_contract.py``.
This module covers what only the solo store has: one file per alias, the
in-place upgrade of lock files written by the previous release, and use from
other threads and other worker processes. Real SQLite only.
"""

from __future__ import annotations

import multiprocessing
import sqlite3
import threading
from pathlib import Path

from code_indexer.server.services.alias_lock_store.base import LockOutcome
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)

_JOIN_TIMEOUT_SECONDS = 30

_PREVIOUS_RELEASE_SCHEMA = """
CREATE TABLE golden_repo_alias_locks (
    lock_key         TEXT PRIMARY KEY,
    owner_token      TEXT NOT NULL,
    operation        TEXT NOT NULL,
    acquired_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_renewed_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def _make_store(tmp_path: Path) -> SqliteAliasLockStore:
    return SqliteAliasLockStore(tmp_path / "alias_locks")


def _on_thread(fn) -> object:
    outcome: dict = {}

    def run() -> None:
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(_JOIN_TIMEOUT_SECONDS)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome
    return outcome.get("value")


def _release_in_child(lock_dir: str, key: str, result) -> None:
    result.put(
        SqliteAliasLockStore(lock_dir).release_owned(key, "mcp_write_mode").value
    )


class TestPerAliasFiles:
    def test_each_alias_gets_its_own_reversibly_named_file(self, tmp_path):
        store = _make_store(tmp_path)
        handle = store.try_acquire("example-repo_1", operation="op")
        assert handle is not None
        assert (tmp_path / "alias_locks" / "example-repo_1.db").is_file()
        store.release(handle)


class TestPreviousReleaseLockFile:
    def test_old_schema_file_is_upgraded_and_its_legacy_row_honoured(self, tmp_path):
        lock_dir = tmp_path / "alias_locks"
        lock_dir.mkdir()
        conn = sqlite3.connect(str(lock_dir / "example-repo.db"))
        with conn:
            conn.execute(_PREVIOUS_RELEASE_SCHEMA)
            conn.execute(
                "INSERT INTO golden_repo_alias_locks (lock_key, owner_token, operation) "
                "VALUES ('example-repo', 'legacy', 'refresh_scheduler')"
            )
        conn.close()

        store = SqliteAliasLockStore(lock_dir)
        # ^ why a leftover row with no pid/ttl => default 1h lease from its last renewal
        assert store.is_held("example-repo") is True
        assert store.try_acquire("example-repo", operation="op") is None
        assert (
            store.release_owned("example-repo", "refresh_scheduler")
            is LockOutcome.RELEASED
        )
        handle = store.try_acquire("example-repo", operation="op", ttl_seconds=60)
        assert handle is not None
        holder = store.get_holder("example-repo")
        assert holder is not None and holder["ttl_seconds"] == 60
        store.release(handle)


class TestOtherThreadsAndProcesses:
    def test_release_and_renew_on_a_different_thread(self, tmp_path):
        store = _make_store(tmp_path)
        handle = store.try_acquire("example-repo", operation="op")
        assert handle is not None
        _on_thread(lambda: store.renew(handle))
        assert store.is_held("example-repo")
        _on_thread(lambda: store.release(handle))
        assert not store.is_held("example-repo")

    def test_release_served_by_another_worker_process(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.try_acquire("example-repo", operation="mcp_write_mode")
        ctx = multiprocessing.get_context("fork")
        result = ctx.Queue()
        child = ctx.Process(
            target=_release_in_child,
            args=(str(tmp_path / "alias_locks"), "example-repo", result),
        )
        child.start()
        child.join(_JOIN_TIMEOUT_SECONDS)
        assert child.exitcode == 0
        assert result.get(timeout=_JOIN_TIMEOUT_SECONDS) == LockOutcome.RELEASED.value
        assert store.is_held("example-repo") is False
