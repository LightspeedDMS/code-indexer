"""Write-mode marker cleanup releases the lock with the marker's own token.

The marker records the lock's owner token at enter; another worker took the
lock, so the cleaning scheduler holds no remembered token of its own. Real
RefreshScheduler, real SQLite alias-lock store, real marker files.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from code_indexer.global_repos.alias_lock_coordinator import AliasLockCoordinator
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)
from tests.fixtures.refresh_scheduler_stores import real_metadata_store

_ALIAS = "cidx-meta"
_OWNER = "mcp_write_mode"
_TOKEN = "write-mode-token"


class _ReleaseFails(SqliteAliasLockStore):
    def release_owned(self, lock_key, operation, *, owner_token):
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


def _enter_on_another_worker(tmp_path: Path, token: str | None) -> Path:
    lock_dir = tmp_path / "alias_locks"
    other = AliasLockCoordinator(
        golden_repos_dir=tmp_path / "golden-repos",
        db_backed_enabled_getter=lambda: True,
        store_resolver=lambda: SqliteAliasLockStore(lock_dir),
    )
    assert other.acquire(_ALIAS, owner_name=_OWNER, owner_token=_TOKEN)
    marker = tmp_path / "golden-repos" / ".write_mode" / f"{_ALIAS}.json"
    marker.parent.mkdir(parents=True)
    content = {"alias": _ALIAS, "entered_at": "2026-01-01T00:00:00+00:00"}
    if token is not None:
        content["owner_token"] = token
    marker.write_text(json.dumps(content))
    return marker


class TestMarkerCleanupUsesTheMarkersToken:
    def test_startup_cleanup_releases_with_the_token_then_removes_the_marker(
        self, tmp_path: Path
    ) -> None:
        marker = _enter_on_another_worker(tmp_path, _TOKEN)
        scheduler = _scheduler(tmp_path, SqliteAliasLockStore(tmp_path / "alias_locks"))

        scheduler.cleanup_stale_write_mode_markers(force=True)

        assert scheduler.is_write_locked(_ALIAS) is False
        assert not marker.exists()

    def test_failed_release_keeps_the_marker_for_a_retry(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        marker = _enter_on_another_worker(tmp_path, _TOKEN)
        scheduler = _scheduler(tmp_path, _ReleaseFails(tmp_path / "alias_locks"))

        with caplog.at_level(logging.WARNING):
            scheduler.cleanup_stale_write_mode_markers(force=True)

        assert marker.exists()
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_marker_without_a_token_is_removed_and_the_lock_left_to_its_lease(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        marker = _enter_on_another_worker(tmp_path, None)
        scheduler = _scheduler(tmp_path, SqliteAliasLockStore(tmp_path / "alias_locks"))

        with caplog.at_level(logging.WARNING):
            scheduler.cleanup_stale_write_mode_markers(force=True)

        assert not marker.exists()
        assert scheduler.is_write_locked(_ALIAS) is True
        assert any("token" in r.getMessage() for r in caplog.records)
