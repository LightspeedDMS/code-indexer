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
from typing import Any, Callable, Dict, Iterator, Optional
from unittest.mock import patch

import pytest

from code_indexer.server.utils.config_manager import (
    CidxMetaBackupConfig,
    ServerConfig,
)
from tests.utils.fatal_chunk_store_fixtures import cidx_child_command, child_env
from tests.utils.golden_repo_metadata_stores import golden_repo_metadata_store
from tests.utils.refresh_fatal_store_harness import (
    Harness,
    RecordingJobManager,
    build_harness,
)

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


def _backed_up_catalog(
    harness: Harness, origin: Path, repo_url: Optional[str] = "local://cidx-meta"
) -> Path:
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
        repo_url,
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


PATTERN_YAML = """\
name: test-pattern
description: "A test pattern"
language: java
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }
"""
BRANCHES = pytest.mark.parametrize(
    "repo_url", ["local://cidx-meta", None], ids=["local", "legacy"]
)
LOCK_KINDS = pytest.mark.parametrize("lock_kind", ["file", "sqlite", "postgres"])


class _Debouncer:
    def signal_dirty(self) -> None:
        pass

    def note_direct_write(self) -> int:
        return 1

    def track_submission(self, job_id: str, seq: int) -> None:
        pass


def _memory_writer(scheduler: Any, meta: Path, tmp_path: Path) -> Callable[[], str]:
    """A real memory create; returns "" when written, else the refusal."""
    from code_indexer.server.services.memory_file_lock_manager import (
        MemoryFileLockManager,
    )
    from code_indexer.server.services.memory_rate_limiter import (
        MemoryRateLimiter,
        RateLimitConfig,
    )
    from code_indexer.server.services.memory_store_service import (
        ConflictError,
        MemoryStoreConfig,
        MemoryStoreService,
    )

    service = MemoryStoreService(
        config=MemoryStoreConfig(memories_dir=meta / "memories"),
        lock_manager=MemoryFileLockManager(tmp_path / "memory-locks"),
        refresh_scheduler=scheduler,
        refresh_debouncer=_Debouncer(),
        rate_limiter=MemoryRateLimiter(
            RateLimitConfig(capacity=100, refill_per_second=100.0)
        ),
        hostname="testhost",
    )
    payload = {
        "type": "architectural-fact",
        "scope": "global",
        "summary": "example fact",
        "evidence": [{"commit": "abc123"}],
    }

    def _attempt() -> str:
        try:
            service.create_memory(payload, username="carol")
        except ConflictError as refused:
            return str(refused)
        return ""

    return _attempt


def _xray_writer(scheduler: Any, meta: Path, tmp_path: Path) -> Callable[[], str]:
    """A real X-Ray pattern store; returns "" when written, else the refusal."""
    from code_indexer.server.services.xray_pattern_service import XrayPatternService

    service = XrayPatternService(meta, refresh_scheduler=scheduler)

    def _attempt() -> str:
        result: Dict[str, Any] = service.store_xray_pattern_audited(
            scope="__any__",
            pattern_yaml=PATTERN_YAML,
            overwrite=False,
            actor="carol",
            may_replace_global=True,
        )
        return "" if result.get("success") else str(result.get("message"))

    return _attempt


WRITERS = {"memory": (_memory_writer, "memories"), "xray": (_xray_writer, "xray-")}


@pytest.mark.parametrize("writer", sorted(WRITERS))
@LOCK_KINDS
@BRANCHES
def test_catalog_writer_is_refused_while_the_backup_sync_runs(
    tmp_path: Path,
    metadata: Any,
    monkeypatch: pytest.MonkeyPatch,
    lock_kind: str,
    repo_url: Optional[str],
    writer: str,
) -> None:
    from code_indexer.server.services.cidx_meta_backup.sync import (
        CidxMetaBackupSync,
    )

    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    _use_lock_store(harness, lock_kind, tmp_path)
    scheduler = harness.scheduler
    origin = tmp_path / "cidx-meta-backup.git"
    meta = _backed_up_catalog(harness, origin, repo_url)
    make_writer, written_prefix = WRITERS[writer]
    attempt = make_writer(scheduler, meta, tmp_path)
    refusals = []
    real_sync = CidxMetaBackupSync.sync

    def _sync_with_a_writer_arriving(self: Any) -> Any:
        refusals.append(attempt())
        return real_sync(self)

    monkeypatch.setattr(CidxMetaBackupSync, "sync", _sync_with_a_writer_arriving)
    with _backup_enabled(tmp_path, origin):
        scheduler._execute_refresh(META_ALIAS)

    assert len(refusals) == 1, "the backup sync did not run"
    assert "another exclusive operation" in refusals[0], (
        "a catalog writer piggybacked on the backup's lock"
    )
    backed_up = _git_out(origin, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert not [name for name in backed_up if name.startswith(written_prefix)]
    tracked = _git_out(meta, "ls-files").splitlines()
    assert not [name for name in tracked if name.startswith(written_prefix)]
    assert not [p for p in meta.iterdir() if p.name.startswith(written_prefix)]
    assert not scheduler.write_lock_manager.get_lock_info(CATALOG)


@LOCK_KINDS
@BRANCHES
def test_backup_lock_is_released_when_the_sync_raises(
    tmp_path: Path,
    metadata: Any,
    monkeypatch: pytest.MonkeyPatch,
    lock_kind: str,
    repo_url: Optional[str],
) -> None:
    from code_indexer.server.services.cidx_meta_backup.sync import (
        CidxMetaBackupSync,
    )

    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    _use_lock_store(harness, lock_kind, tmp_path)
    scheduler = harness.scheduler
    origin = tmp_path / "cidx-meta-backup.git"
    _backed_up_catalog(harness, origin, repo_url)
    held_during_sync = []

    def _failing_sync(self: Any) -> Any:
        held_during_sync.append(scheduler.write_lock_manager.get_lock_info(CATALOG))
        raise RuntimeError("backup sync failed")

    monkeypatch.setattr(CidxMetaBackupSync, "sync", _failing_sync)
    with _backup_enabled(tmp_path, origin):
        try:
            scheduler._execute_refresh(META_ALIAS)
        except RuntimeError as raised:
            assert "backup sync failed" in str(raised)

    assert held_during_sync and held_during_sync[0], "the sync ran without the lock"
    assert not scheduler.write_lock_manager.get_lock_info(CATALOG)
    assert scheduler.write_lock_manager.acquire(CATALOG, owner_name="mcp_write_mode")
    assert scheduler.write_lock_manager.release(CATALOG, owner_name="mcp_write_mode")


@LOCK_KINDS
def test_xray_pattern_store_leaves_the_backup_push_to_the_refresh(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch, lock_kind: str
) -> None:
    from code_indexer.global_repos import refresh_scheduler as scheduler_module
    from code_indexer.server.services import xray_pattern_service

    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    _use_lock_store(harness, lock_kind, tmp_path)
    scheduler = harness.scheduler
    origin = tmp_path / "cidx-meta-backup.git"
    meta = _backed_up_catalog(harness, origin)
    origin_before = _git_out(origin, "rev-parse", "HEAD")
    pattern = "xray-patterns/__any__/test-pattern.yaml"
    # records the refresh the store requests instead of running it in process
    jobs = RecordingJobManager()
    # the attribute is typed as the real BackgroundJobManager
    scheduler.background_job_manager = jobs  # type: ignore[assignment]

    with _backup_enabled(tmp_path, origin):
        # the pattern store must see the same (enabled) backup config
        monkeypatch.setattr(
            xray_pattern_service,
            "get_config_service",
            scheduler_module.get_config_service,
            raising=False,
        )
        assert _xray_writer(scheduler, meta, tmp_path)() == ""
        assert len(jobs.submitted) == 1, "the store requested no catalog refresh"
        assert pattern in _git_out(meta, "ls-files").splitlines()
        assert _git_out(origin, "rev-parse", "HEAD") == origin_before, (
            "the pattern store ran a backup push of its own"
        )

        scheduler._execute_refresh(META_ALIAS)

    assert _git_out(origin, "rev-parse", "HEAD") == _git_out(meta, "rev-parse", "HEAD")
    assert pattern in _git_out(origin, "ls-tree", "-r", "--name-only", "HEAD")
