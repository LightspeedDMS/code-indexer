"""Write-mode marker cleanup releases a gone holder's lock with the marker's token.

The holder that took the lock is gone (its process on this host is dead), so
the cleaning scheduler holds no remembered token of its own and must use the
token recorded in the marker. Real RefreshScheduler, real SQLite alias-lock
store, real marker files.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional
from unittest.mock import Mock

import pytest

from code_indexer.global_repos.alias_lock_coordinator import AliasLockCoordinator
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.alias_lock_store.base import (
    LockOutcome,
    local_holder_identity,
)
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)
from tests.fixtures.refresh_scheduler_stores import real_metadata_store

_ALIAS = "cidx-meta"
_OWNER = "mcp_write_mode"
_LOCK_TOKEN = "write-mode-token"
_JOIN_TIMEOUT_SECONDS = 30


class _ReleaseFails(SqliteAliasLockStore):
    def release_owned(
        self,
        lock_key: str,
        operation: str,
        *,
        owner_token: str,
        only_if_stale: bool = False,
    ) -> LockOutcome:
        raise sqlite3.OperationalError("disk I/O error")


def _scheduler(tmp_path: Path, store: SqliteAliasLockStore) -> RefreshScheduler:
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    registry = Mock()
    registry.list_global_repos.return_value = []
    return RefreshScheduler(
        golden_repos_dir=str(tmp_path / "golden-repos"),
        config_source=config,
        query_tracker=Mock(spec=QueryTracker),
        cleanup_manager=Mock(spec=CleanupManager),
        registry=registry,
        golden_repo_metadata_backend=real_metadata_store(tmp_path / "server-data"),
        alias_lock_db_backed_enabled_getter=lambda: True,
        alias_lock_store_resolver=lambda: store,
    )


def _dead_pid() -> int:
    child = multiprocessing.get_context("fork").Process(target=os._exit, args=(0,))
    child.start()
    child.join(_JOIN_TIMEOUT_SECONDS)
    assert child.pid is not None
    return child.pid


def _lock_db(tmp_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(tmp_path / "alias_locks" / f"{_ALIAS}.db"))


def _enter_then_holder_dies(tmp_path: Path, marker_token: Optional[str]) -> Path:
    """Lock taken with _LOCK_TOKEN by another worker whose process then died;
    the marker records ``marker_token`` (no token field when None)."""
    other = AliasLockCoordinator(
        golden_repos_dir=tmp_path / "golden-repos",
        db_backed_enabled_getter=lambda: True,
        store_resolver=lambda: SqliteAliasLockStore(tmp_path / "alias_locks"),
    )
    assert other.acquire(_ALIAS, owner_name=_OWNER, owner_token=_LOCK_TOKEN)
    with closing(_lock_db(tmp_path)) as conn, conn:
        conn.execute(
            "UPDATE golden_repo_alias_locks SET hostname = ?, pid = ?",
            (local_holder_identity(), _dead_pid()),
        )
    marker = tmp_path / "golden-repos" / ".write_mode" / f"{_ALIAS}.json"
    marker.parent.mkdir(parents=True)
    content = {"alias": _ALIAS, "entered_at": "2026-01-01T00:00:00+00:00"}
    if marker_token is not None:
        content["owner_token"] = marker_token
    marker.write_text(json.dumps(content))
    return marker


def _row_count(tmp_path: Path) -> int:
    with closing(_lock_db(tmp_path)) as conn:
        (count,) = conn.execute(
            "SELECT count(*) FROM golden_repo_alias_locks"
        ).fetchone()
    return int(count)


class TestMarkerCleanupUsesTheMarkersToken:
    def test_startup_cleanup_releases_with_the_token_then_removes_the_marker(
        self, tmp_path: Path
    ) -> None:
        marker = _enter_then_holder_dies(tmp_path, _LOCK_TOKEN)
        scheduler = _scheduler(tmp_path, SqliteAliasLockStore(tmp_path / "alias_locks"))

        scheduler.cleanup_stale_write_mode_markers(force=True)

        assert _row_count(tmp_path) == 0
        assert not marker.exists()

    def test_failed_release_keeps_the_marker_for_a_retry(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        marker = _enter_then_holder_dies(tmp_path, _LOCK_TOKEN)
        scheduler = _scheduler(tmp_path, _ReleaseFails(tmp_path / "alias_locks"))

        with caplog.at_level(logging.WARNING):
            scheduler.cleanup_stale_write_mode_markers(force=True)

        assert marker.exists()
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_marker_without_a_token_is_removed_and_the_row_left_alone(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        marker = _enter_then_holder_dies(tmp_path, None)
        scheduler = _scheduler(tmp_path, SqliteAliasLockStore(tmp_path / "alias_locks"))

        with caplog.at_level(logging.WARNING):
            scheduler.cleanup_stale_write_mode_markers(force=True)

        assert not marker.exists()
        assert _row_count(tmp_path) == 1
        assert any("token" in r.getMessage() for r in caplog.records)
