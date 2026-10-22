"""Write-mode marker cleanup releases a lock only when its holder is gone.

The marker lives on shared golden-repos storage, so another node's (or
worker's) live write-mode session survives this process's restart. Cleanup --
forced at startup or not -- must keep a live holder's lock and marker, decide
from ONE read of the marker, and never delete a marker it did not evaluate.

Real RefreshScheduler, real alias-lock stores (SQLite, and PostgreSQL via
``TEST_POSTGRES_DSN``), real marker files, real threads; no sleeps.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from unittest.mock import Mock

import pytest

from code_indexer.global_repos.alias_lock_coordinator import AliasLockCoordinator
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.alias_lock_store.base import local_holder_identity
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)
from tests.fixtures.refresh_scheduler_stores import real_metadata_store

_ALIAS = "cidx-meta"
_OWNER = "mcp_write_mode"
_JOIN_TIMEOUT_SECONDS = 30


def _foreign_identity() -> str:
    # ^ why another PID namespace on THIS boot: a different boot id would be an earlier boot
    boot = local_holder_identity().split("|")[1]
    return f"foreign-host.example.com|{boot}|pid:[4026531836]"


MakeStore = Callable[..., Any]


def _set_holder(store: Any, hostname: str, pid: int) -> None:
    if isinstance(store, SqliteAliasLockStore):
        import sqlite3

        conn = sqlite3.connect(str(store._lock_dir / f"{_ALIAS}.db"))
        with conn:
            conn.execute(
                "UPDATE golden_repo_alias_locks SET hostname = ?, pid = ?",
                (hostname, pid),
            )
        conn.close()
        return
    import psycopg

    with psycopg.connect(store._dsn) as pg:
        pg.execute(
            "UPDATE golden_repo_alias_locks SET hostname = %s, pid = %s "
            "WHERE lock_key = %s",
            (hostname, pid, _ALIAS),
        )


def _dead_pid() -> int:
    child = multiprocessing.get_context("fork").Process(target=os._exit, args=(0,))
    child.start()
    child.join(_JOIN_TIMEOUT_SECONDS)
    assert child.pid is not None
    return child.pid


@pytest.fixture(params=["sqlite", "postgres"])
def make_store(request, tmp_path: Path) -> Iterator[MakeStore]:
    if request.param == "sqlite":
        lock_dir = tmp_path / "alias_locks"

        def sqlite_store(cls: type = SqliteAliasLockStore) -> Any:
            return cls(lock_dir)

        yield sqlite_store
        return
    from code_indexer.server.services.alias_lock_store.postgres_store import (
        PostgresAliasLockStore,
    )
    from tests.shared.alias_lock_pg import postgres_lock_store_dsn

    with postgres_lock_store_dsn(_ALIAS) as dsn:

        def pg_store(cls: type = PostgresAliasLockStore) -> Any:
            return cls(dsn)

        yield pg_store


def _coordinator(golden: Path, store: Any) -> AliasLockCoordinator:
    return AliasLockCoordinator(
        golden_repos_dir=golden,
        db_backed_enabled_getter=lambda: True,
        store_resolver=lambda: store,
    )


def _scheduler(tmp_path: Path, store: Any) -> RefreshScheduler:
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


def _enter(golden: Path, store: Any, token: str) -> Path:
    """What enter_write_mode leaves behind: the lock and its marker."""
    assert _coordinator(golden, store).acquire(
        _ALIAS, owner_name=_OWNER, owner_token=token
    )
    marker = golden / ".write_mode" / f"{_ALIAS}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "alias": _ALIAS,
                "owner_token": token,
                "entered_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )
    return marker


def _marker_token(marker: Path) -> Optional[str]:
    return (
        json.loads(marker.read_text()).get("owner_token") if marker.exists() else None
    )


class TestCleanupReleasesOnlyAGoneHolder:
    def test_live_holder_of_another_identity_survives_startup_cleanup(
        self, tmp_path: Path, make_store: MakeStore
    ) -> None:
        golden = tmp_path / "golden-repos"
        store = make_store()
        marker = _enter(golden, store, "live-token")
        _set_holder(store, _foreign_identity(), _dead_pid())

        _scheduler(tmp_path, make_store()).cleanup_stale_write_mode_markers(force=True)

        assert _coordinator(golden, make_store()).is_locked(_ALIAS) is True
        assert _marker_token(marker) == "live-token"

    def test_dead_holder_of_this_identity_is_released_and_its_marker_removed(
        self, tmp_path: Path, make_store: MakeStore
    ) -> None:
        golden = tmp_path / "golden-repos"
        store = make_store()
        marker = _enter(golden, store, "dead-token")
        _set_holder(store, local_holder_identity(), _dead_pid())

        _scheduler(tmp_path, make_store()).cleanup_stale_write_mode_markers(force=True)

        assert _coordinator(golden, make_store()).is_locked(_ALIAS) is False
        assert not marker.exists()

    def test_refused_release_keeps_the_marker(
        self, tmp_path: Path, make_store: MakeStore
    ) -> None:
        golden = tmp_path / "golden-repos"
        store = make_store()
        marker = _enter(golden, store, "row-token")
        _set_holder(store, local_holder_identity(), _dead_pid())
        marker.write_text(json.dumps({"alias": _ALIAS, "owner_token": "other-token"}))

        _scheduler(tmp_path, make_store()).cleanup_stale_write_mode_markers(force=True)

        assert _marker_token(marker) == "other-token"

    def test_session_entering_during_cleanup_keeps_its_lock_and_marker(
        self, tmp_path: Path, make_store: MakeStore
    ) -> None:
        golden = tmp_path / "golden-repos"
        store = make_store()
        marker = _enter(golden, store, "dead-token")
        _set_holder(store, local_holder_identity(), _dead_pid())
        base = type(store)

        class NewSessionEntersDuringRelease(base):  # type: ignore[misc,valid-type]
            def release_owned(
                self, lock_key, operation, *, owner_token, only_if_stale=False
            ):
                outcome = super().release_owned(
                    lock_key,
                    operation,
                    owner_token=owner_token,
                    only_if_stale=only_if_stale,
                )
                entering = threading.Thread(
                    target=_enter, args=(golden, make_store(), "new-token")
                )
                entering.start()
                entering.join(_JOIN_TIMEOUT_SECONDS)
                return outcome

        cleaner = _scheduler(tmp_path, make_store(NewSessionEntersDuringRelease))
        cleaner.cleanup_stale_write_mode_markers(force=True)

        assert _coordinator(golden, make_store()).is_locked(_ALIAS) is True
        assert _marker_token(marker) == "new-token"
