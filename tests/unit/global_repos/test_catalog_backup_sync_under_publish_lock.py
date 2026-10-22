"""The catalog backup's git write sequence (bootstrap, stage-all, commit,
push) runs only while the refresh holds the lock it publishes with: an
external writer that takes the catalog lock after the refresh checked it
never has its in-progress files committed by the backup.

Real components: the real RefreshScheduler and lock coordinator, a real
git-backed ``local://cidx-meta`` bootstrapped by the real backup bootstrap
against a local bare origin, the real backup sync. Ordering between the
refresh and the external writer uses a thread and events, never sleeps.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import patch

import pytest

from code_indexer.server.utils.config_manager import (
    CidxMetaBackupConfig,
    ServerConfig,
)
from tests.utils.fatal_chunk_store_fixtures import cidx_child_command, child_env
from tests.utils.golden_repo_metadata_stores import golden_repo_metadata_store
from tests.utils.refresh_fatal_store_harness import Harness, build_harness

pytestmark = pytest.mark.timeout(300)

META_ALIAS = "cidx-meta-global"
CATALOG = "cidx-meta"
WAIT_SECONDS = 60.0


@pytest.fixture
def metadata(tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store("sqlite", tmp_path) as backend:
        yield backend


def _use_lock_store(harness: Harness, kind: str, tmp_path: Path) -> None:
    """Install the real lock coordinator over the given mechanism."""
    if kind == "file":
        return
    from code_indexer.global_repos.alias_lock_coordinator import (
        AliasLockCoordinator,
    )

    store: Any
    if kind == "sqlite":
        from code_indexer.server.services.alias_lock_store.sqlite_store import (
            SqliteAliasLockStore,
        )

        store = SqliteAliasLockStore(tmp_path / "alias_locks")
    else:
        dsn = os.environ.get("TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("TEST_POSTGRES_DSN not set")
        from code_indexer.server.services.alias_lock_store.postgres_store import (
            PostgresAliasLockStore,
        )
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        MigrationRunner(dsn).run()
        store = PostgresAliasLockStore(dsn)
        assert not store.is_held(CATALOG), "a previous test left the lock held"
    harness.scheduler.write_lock_manager = AliasLockCoordinator(
        golden_repos_dir=Path(harness.scheduler.golden_repos_dir),
        db_backed_enabled_getter=lambda: True,
        store_resolver=lambda: store,
    )


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _backed_up_catalog(harness: Harness, origin: Path) -> Path:
    from code_indexer.server.services.cidx_meta_backup.bootstrap import (
        CidxMetaBackupBootstrap,
    )

    meta = harness.source.parent / CATALOG
    meta.mkdir()
    (meta / "example-repo.md").write_text("# example-repo\n")
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    CidxMetaBackupBootstrap().bootstrap(str(meta), str(origin))
    subprocess.run(
        cidx_child_command(["init"]),
        cwd=meta,
        env=child_env(),
        check=True,
        capture_output=True,
        timeout=180,
    )
    harness.registry.register_global_repo(
        CATALOG,
        META_ALIAS,
        "local://cidx-meta",
        index_path=str(meta),
        allow_reserved=True,
    )
    harness.scheduler.alias_manager.create_alias(
        META_ALIAS, str(meta), repo_name=CATALOG
    )
    return meta


@contextlib.contextmanager
def _backup_enabled(tmp_path: Path, origin: Path) -> Iterator[None]:
    config = ServerConfig(server_dir=str(tmp_path / "server"))
    config.cidx_meta_backup_config = CidxMetaBackupConfig(
        enabled=True, remote_url=str(origin)
    )
    config_service = SimpleNamespace(
        get_config=lambda: config,
        sync_repo_extensions_if_drifted=lambda repo_path: None,
    )
    with patch(
        "code_indexer.global_repos.refresh_scheduler.get_config_service",
        return_value=config_service,
    ):
        yield


@pytest.mark.parametrize("lock_kind", ["file", "sqlite", "postgres"])
def test_writer_locking_after_the_check_is_never_committed_by_the_backup(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch, lock_kind: str
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    _use_lock_store(harness, lock_kind, tmp_path)
    scheduler = harness.scheduler
    origin = tmp_path / "cidx-meta-backup.git"
    meta = _backed_up_catalog(harness, origin)
    head_before = _git_out(meta, "rev-parse", "HEAD")
    origin_before = _git_out(origin, "rev-parse", "HEAD")

    checked = threading.Event()
    writer_holds = threading.Event()
    real_check = scheduler.is_write_locked

    def _check_then_let_the_writer_in(alias: str) -> bool:
        held = real_check(alias)
        if alias == CATALOG and not checked.is_set():
            checked.set()
            assert writer_holds.wait(WAIT_SECONDS)
        return held

    def _external_writer() -> None:
        assert checked.wait(WAIT_SECONDS)
        assert scheduler.write_lock_manager.acquire(
            CATALOG, owner_name="mcp_write_mode"
        )
        (meta / "operator-draft.md").write_text("half-written by the operator\n")
        writer_holds.set()

    monkeypatch.setattr(scheduler, "is_write_locked", _check_then_let_the_writer_in)
    writer = threading.Thread(target=_external_writer)
    writer.start()
    try:
        with _backup_enabled(tmp_path, origin):
            result = scheduler._execute_refresh(META_ALIAS)
        writer.join(WAIT_SECONDS)
        assert not writer.is_alive()

        assert result["message"] == "Skipped, write lock held", result
        assert _git_out(meta, "rev-parse", "HEAD") == head_before, (
            "the backup committed while another owner held the catalog lock"
        )
        assert _git_out(origin, "rev-parse", "HEAD") == origin_before
        assert "operator-draft.md" in _git_out(meta, "status", "--porcelain")
    finally:
        writer_holds.set()
        checked.set()
        scheduler.write_lock_manager.release(CATALOG, owner_name="mcp_write_mode")
