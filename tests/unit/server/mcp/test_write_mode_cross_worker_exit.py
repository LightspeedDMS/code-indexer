"""Write mode entered on one worker is exited on another.

Behind a load balancer the enter_write_mode and exit_write_mode calls for the
catalog can be served by different worker processes. The MCP API is
unchanged (clients pass only ``repo_alias``): the enter records the lock's
owner token in the server-side write-mode marker on shared golden-repos
storage, and the exit -- on whichever worker serves it -- releases with that
token. Each worker below owns its own AliasLockCoordinator and store
instance over the same real lock storage (SQLite per-alias files, or
PostgreSQL via ``TEST_POSTGRES_DSN``); the real handlers run unchanged and
only the refresh itself is recorded instead of run.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, cast
from unittest.mock import patch

import pytest

from code_indexer.global_repos.alias_lock_coordinator import AliasLockCoordinator
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)

_REPO = "cidx-meta-global"
_ALIAS = "cidx-meta"
_WRITE_MODE_OWNER = "mcp_write_mode"


class _Worker:
    """Scheduler-shaped worker: real lock coordinator, recorded refreshes."""

    def __init__(self, golden_repos_dir: Path, store: Any) -> None:
        self.write_lock_manager = AliasLockCoordinator(
            golden_repos_dir=golden_repos_dir,
            db_backed_enabled_getter=lambda: True,
            store_resolver=lambda: store,
        )
        self.refreshed: List[str] = []

    def release_write_lock(
        self,
        alias: str,
        owner_name: str = "refresh_scheduler",
        owner_token: Optional[str] = None,
    ) -> bool:
        return self.write_lock_manager.release(
            alias, owner_name=owner_name, owner_token=owner_token
        )

    def _execute_refresh(self, repo_alias: str) -> Dict[str, Any]:
        self.refreshed.append(repo_alias)
        return {"success": True, "alias": repo_alias}


@pytest.fixture(params=["sqlite", "postgres"])
def make_store(request, tmp_path: Path) -> Iterator[Callable[[], Any]]:
    if request.param == "sqlite":
        lock_dir = tmp_path / "alias_locks"
        yield lambda: SqliteAliasLockStore(lock_dir)
        return
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN not set")
    from code_indexer.server.services.alias_lock_store.postgres_store import (
        PostgresAliasLockStore,
    )

    yield lambda: PostgresAliasLockStore(dsn)


@pytest.fixture
def golden_repos_dir(tmp_path: Path, monkeypatch) -> Path:
    """Golden repos dir with the catalog bound as a write exception on the
    process-wide file_crud_service (monkeypatch restores the singleton)."""
    from code_indexer.server.services.file_crud_service import file_crud_service

    golden = tmp_path / "golden-repos"
    (golden / _ALIAS).mkdir(parents=True)
    monkeypatch.setattr(
        file_crud_service, "_global_write_exceptions", {_REPO: golden / _ALIAS}
    )
    monkeypatch.setattr(file_crud_service, "_golden_repos_dir", golden)
    return golden


def _call(handler_name: str, worker: _Worker, golden: Path) -> Dict[str, Any]:
    from code_indexer.server.mcp import handlers

    user = User(
        username="example-user",
        role=UserRole.POWER_USER,
        password_hash="unused",
        created_at=datetime.now(),
    )
    with (
        patch(
            "code_indexer.server.mcp.handlers._get_app_refresh_scheduler",
            return_value=worker,
        ),
        patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=str(golden),
        ),
    ):
        response = getattr(handlers, handler_name)({"repo_alias": _REPO}, user)
    return cast(Dict[str, Any], json.loads(response["content"][0]["text"]))


def _marker_token(golden: Path) -> str:
    marker = json.loads((golden / ".write_mode" / f"{_ALIAS}.json").read_text())
    return cast(str, marker["owner_token"])


class TestWriteModeExitOnAnotherWorker:
    def test_exit_on_another_worker_releases_the_lock_and_refreshes(
        self, golden_repos_dir: Path, make_store
    ) -> None:
        enter_worker = _Worker(golden_repos_dir, make_store())
        exit_worker = _Worker(golden_repos_dir, make_store())

        entered = _call("handle_enter_write_mode", enter_worker, golden_repos_dir)
        assert entered["success"] is True
        assert _marker_token(golden_repos_dir)
        assert exit_worker.write_lock_manager.is_locked(_ALIAS)

        result = _call("handle_exit_write_mode", exit_worker, golden_repos_dir)

        assert result["success"] is True
        assert exit_worker.refreshed == [_REPO]
        assert exit_worker.write_lock_manager.is_locked(_ALIAS) is False
        assert enter_worker.write_lock_manager.is_locked(_ALIAS) is False

    def test_late_exit_never_frees_a_lock_another_holder_took(
        self, golden_repos_dir: Path, make_store
    ) -> None:
        enter_worker = _Worker(golden_repos_dir, make_store())
        refresher = _Worker(golden_repos_dir, make_store())
        assert _call("handle_enter_write_mode", enter_worker, golden_repos_dir)[
            "success"
        ]
        # ^ why stands in for a lapsed lease taken over by a refresh
        assert refresher.write_lock_manager.release(
            _ALIAS,
            owner_name=_WRITE_MODE_OWNER,
            owner_token=_marker_token(golden_repos_dir),
        )
        assert refresher.write_lock_manager.acquire(
            _ALIAS, owner_name="refresh_scheduler"
        )

        late_exit_worker = _Worker(golden_repos_dir, make_store())
        _call("handle_exit_write_mode", late_exit_worker, golden_repos_dir)

        assert refresher.write_lock_manager.is_locked(_ALIAS) is True
        assert refresher.write_lock_manager.release(
            _ALIAS, owner_name="refresh_scheduler"
        )
