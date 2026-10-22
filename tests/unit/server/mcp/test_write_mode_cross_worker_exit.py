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
import sqlite3
import stat
from contextlib import closing
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
        self.refresh_result: Dict[str, Any] = {"success": True}

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
        return {**self.refresh_result, "alias": repo_alias}


@pytest.fixture(params=["sqlite", "postgres"])
def make_store(request, tmp_path: Path) -> Iterator[Callable[[], Any]]:
    if request.param == "sqlite":
        lock_dir = tmp_path / "alias_locks"
        yield lambda: SqliteAliasLockStore(lock_dir)
        return
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN not set")
    import psycopg

    from code_indexer.server.services.alias_lock_store.postgres_store import (
        PostgresAliasLockStore,
    )

    def clear_catalog_row() -> None:
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "DELETE FROM golden_repo_alias_locks WHERE lock_key = %s", (_ALIAS,)
            )

    clear_catalog_row()
    yield lambda: PostgresAliasLockStore(dsn)
    clear_catalog_row()


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


class TestExitReportsTheRealOutcome:
    @pytest.mark.parametrize("marker_text", ['{"alias": "cidx-meta"}', "not json"])
    def test_marker_without_a_usable_token_is_an_error(
        self, golden_repos_dir: Path, make_store, marker_text: str
    ) -> None:
        worker = _Worker(golden_repos_dir, make_store())
        assert _call("handle_enter_write_mode", worker, golden_repos_dir)["success"]
        marker = golden_repos_dir / ".write_mode" / f"{_ALIAS}.json"
        marker.write_text(marker_text)

        exit_worker = _Worker(golden_repos_dir, make_store())
        result = _call("handle_exit_write_mode", exit_worker, golden_repos_dir)

        assert result["success"] is False
        assert exit_worker.refreshed == []
        assert not marker.exists()

    def test_exit_after_another_holder_took_the_lock_is_an_error(
        self, golden_repos_dir: Path, make_store
    ) -> None:
        worker = _Worker(golden_repos_dir, make_store())
        assert _call("handle_enter_write_mode", worker, golden_repos_dir)["success"]
        holder = _Worker(golden_repos_dir, make_store()).write_lock_manager
        token = _marker_token(golden_repos_dir)
        assert holder.release(_ALIAS, owner_name=_WRITE_MODE_OWNER, owner_token=token)
        assert holder.acquire(_ALIAS, owner_name=_WRITE_MODE_OWNER)

        exit_worker = _Worker(golden_repos_dir, make_store())
        result = _call("handle_exit_write_mode", exit_worker, golden_repos_dir)

        assert result["success"] is False
        assert exit_worker.refreshed == []
        assert holder.is_locked(_ALIAS) is True
        assert holder.release(_ALIAS, owner_name=_WRITE_MODE_OWNER)

    def test_failed_refresh_is_reported(
        self, golden_repos_dir: Path, make_store
    ) -> None:
        worker = _Worker(golden_repos_dir, make_store())
        assert _call("handle_enter_write_mode", worker, golden_repos_dir)["success"]
        worker.refresh_result = {"success": False, "error": "indexing failed"}

        result = _call("handle_exit_write_mode", worker, golden_repos_dir)

        assert result["success"] is False
        assert "indexing failed" in json.dumps(result)
        assert worker.write_lock_manager.is_locked(_ALIAS) is False


class TestMarkerPermissions:
    def test_marker_is_private_to_the_service_user(
        self, golden_repos_dir: Path, make_store
    ) -> None:
        previous_umask = os.umask(0o022)
        try:
            worker = _Worker(golden_repos_dir, make_store())
            assert _call("handle_enter_write_mode", worker, golden_repos_dir)["success"]
        finally:
            os.umask(previous_umask)
        marker_dir = golden_repos_dir / ".write_mode"
        assert stat.S_IMODE(marker_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE((marker_dir / f"{_ALIAS}.json").stat().st_mode) == 0o600
        assert _call("handle_exit_write_mode", worker, golden_repos_dir)["success"]

    def test_existing_marker_directory_is_made_private(
        self, golden_repos_dir: Path, make_store
    ) -> None:
        marker_dir = golden_repos_dir / ".write_mode"
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker_dir.chmod(0o755)
        worker = _Worker(golden_repos_dir, make_store())
        assert _call("handle_enter_write_mode", worker, golden_repos_dir)["success"]
        assert stat.S_IMODE(marker_dir.stat().st_mode) == 0o700
        assert _call("handle_exit_write_mode", worker, golden_repos_dir)["success"]


_AGE_SQL = (
    "UPDATE golden_repo_alias_locks SET last_renewed_at = "
    "strftime('%Y-%m-%d %H:%M:%f', 'now', '-50 minutes')"
)
_ELAPSED_SQL = (
    "SELECT (julianday('now') - julianday(last_renewed_at)) * 86400.0 "
    "FROM golden_repo_alias_locks"
)


class TestFileOperationRenewsTheLease:
    def _lock_db(self, tmp_path: Path) -> sqlite3.Connection:
        return sqlite3.connect(str(tmp_path / "alias_locks" / f"{_ALIAS}.db"))

    def _file_operation_on(self, worker: _Worker, golden: Path) -> None:
        from types import SimpleNamespace

        from code_indexer.server.mcp.handlers import files

        app = SimpleNamespace(state=SimpleNamespace(golden_repos_dir=str(golden)))
        with (
            patch.object(files, "_get_app_refresh_scheduler", return_value=worker),
            patch.object(files, "_get_golden_repos_dir", return_value=str(golden)),
            patch.object(files._utils, "_lazy_module_attr_or_none", return_value=app),
        ):
            # create/edit/delete run this gate before writing
            files._write_mode_refusal(_REPO)

    def test_file_operation_under_write_mode_renews_the_lease(
        self, golden_repos_dir: Path, tmp_path: Path
    ) -> None:
        store = SqliteAliasLockStore(tmp_path / "alias_locks")
        assert _call(
            "handle_enter_write_mode",
            _Worker(golden_repos_dir, store),
            golden_repos_dir,
        )["success"]
        with self._lock_db(tmp_path) as conn:
            conn.execute(_AGE_SQL)

        self._file_operation_on(_Worker(golden_repos_dir, store), golden_repos_dir)

        with self._lock_db(tmp_path) as conn:
            (elapsed,) = conn.execute(_ELAPSED_SQL).fetchone()
        assert elapsed < 60

    def test_file_operation_without_write_mode_renews_nothing(
        self, golden_repos_dir: Path, tmp_path: Path
    ) -> None:
        store = SqliteAliasLockStore(tmp_path / "alias_locks")
        worker = _Worker(golden_repos_dir, store)
        assert worker.write_lock_manager.acquire(_ALIAS, owner_name="refresh_scheduler")
        with self._lock_db(tmp_path) as conn:
            conn.execute(_AGE_SQL)

        self._file_operation_on(worker, golden_repos_dir)

        with self._lock_db(tmp_path) as conn:
            (elapsed,) = conn.execute(_ELAPSED_SQL).fetchone()
        assert elapsed > 2900


def _create_file(worker: _Worker, golden: Path, file_path: str) -> Dict[str, Any]:
    from types import SimpleNamespace

    from code_indexer.server.mcp.handlers import files

    app = SimpleNamespace(state=SimpleNamespace(golden_repos_dir=str(golden)))
    user = User(
        username="example-admin",
        role=UserRole.ADMIN,
        password_hash="unused",
        created_at=datetime.now(),
    )
    from code_indexer.server.services.cidx_meta_access.file_doors import UNRESTRICTED

    params = {"repository_alias": _REPO, "file_path": file_path, "content": "x\n"}
    with (
        # ^ why access judging is a separate concern from write-mode lock ownership
        patch.object(files, "_judge", return_value=UNRESTRICTED),
        patch.object(files, "_get_app_refresh_scheduler", return_value=worker),
        patch.object(files, "_get_golden_repos_dir", return_value=str(golden)),
        patch.object(files._utils, "_lazy_module_attr_or_none", return_value=app),
    ):
        response = files.handle_create_file(params, user)
    return cast(Dict[str, Any], json.loads(response["content"][0]["text"]))


class TestFileWriteRequiresLockOwnership:
    def test_write_proceeds_while_the_lock_is_owned(
        self, golden_repos_dir: Path, make_store
    ) -> None:
        worker = _Worker(golden_repos_dir, make_store())
        assert _call("handle_enter_write_mode", worker, golden_repos_dir)["success"]

        result = _create_file(worker, golden_repos_dir, "owned.md")

        assert result["success"] is True, result
        assert (golden_repos_dir / _ALIAS / "owned.md").is_file()

    def test_write_after_another_writer_took_the_lock_is_refused(
        self, golden_repos_dir: Path, make_store
    ) -> None:
        worker = _Worker(golden_repos_dir, make_store())
        assert _call("handle_enter_write_mode", worker, golden_repos_dir)["success"]
        other = _Worker(golden_repos_dir, make_store()).write_lock_manager
        token = _marker_token(golden_repos_dir)
        assert other.release(_ALIAS, owner_name=_WRITE_MODE_OWNER, owner_token=token)
        assert other.acquire(_ALIAS, owner_name="refresh_scheduler")

        result = _create_file(worker, golden_repos_dir, "late.md")

        assert result["success"] is False
        assert "enter_write_mode" in json.dumps(result)
        assert not (golden_repos_dir / _ALIAS / "late.md").exists()
        assert other.release(_ALIAS, owner_name="refresh_scheduler")

    def test_write_after_the_lease_lapsed_is_refused(
        self, golden_repos_dir: Path, make_store
    ) -> None:
        store = make_store()
        assert _call(
            "handle_enter_write_mode",
            _Worker(golden_repos_dir, store),
            golden_repos_dir,
        )["success"]
        if isinstance(store, SqliteAliasLockStore):
            with closing(sqlite3.connect(str(store._lock_dir / f"{_ALIAS}.db"))) as db:
                with db:
                    db.execute(
                        "UPDATE golden_repo_alias_locks SET last_renewed_at = "
                        "strftime('%Y-%m-%d %H:%M:%f', 'now', '-2 hours')"
                    )
        else:
            import psycopg

            with psycopg.connect(store._dsn) as pg:
                pg.execute(
                    "UPDATE golden_repo_alias_locks SET last_renewed_at = "
                    "now() - interval '2 hours' WHERE lock_key = %s",
                    (_ALIAS,),
                )

        result = _create_file(
            _Worker(golden_repos_dir, store), golden_repos_dir, "l.md"
        )

        assert result["success"] is False
        assert "enter_write_mode" in json.dumps(result)
        assert not (golden_repos_dir / _ALIAS / "l.md").exists()

    def test_write_is_refused_when_the_store_cannot_confirm_ownership(
        self, golden_repos_dir: Path, make_store
    ) -> None:
        assert _call(
            "handle_enter_write_mode",
            _Worker(golden_repos_dir, make_store()),
            golden_repos_dir,
        )["success"]
        original = make_store()

        class RenewFails(type(original)):  # type: ignore[misc]
            def renew_owned(self, *args: Any, **kwargs: Any) -> Any:
                raise sqlite3.OperationalError("disk I/O error")

        source = (
            original._lock_dir
            if isinstance(original, SqliteAliasLockStore)
            else original._dsn
        )
        worker = _Worker(golden_repos_dir, RenewFails(source))
        result = _create_file(worker, golden_repos_dir, "x.md")

        assert result["success"] is False
        assert not (golden_repos_dir / _ALIAS / "x.md").exists()
