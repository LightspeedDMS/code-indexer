"""A catalog writer piggybacks on a held cidx-meta write lock only when the
holder is a cooperative catalog writer whose write is published by a later
refresh (the refresh itself, another memory or X-Ray pattern write, the
dependency map, the description lifecycle writers, the meta-directory
updater). Any other holder -- write mode, a restore, reconciliation,
anything unknown -- holds it exclusively: the memory and X-Ray pattern
writers refuse with a retryable error naming only a neutral category (never
the holder's raw owner name, which can carry another user's username) and
write nothing.

Real components: the real RefreshScheduler and its lock coordinator over
the file lock, the SQLite alias-lock store and (with TEST_POSTGRES_DSN)
the PostgreSQL store; the real memory and X-Ray pattern services.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterator
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos import meta_description_hook
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.memory_file_lock_manager import MemoryFileLockManager
from code_indexer.server.services.memory_rate_limiter import (
    MemoryRateLimiter,
    RateLimitConfig,
)
from code_indexer.server.services.memory_store_service import (
    ConflictError,
    MemoryStoreConfig,
    MemoryStoreService,
)
from code_indexer.server.services.xray_pattern_service import XrayPatternService
from tests.fixtures.refresh_scheduler_stores import real_metadata_store

CATALOG = "cidx-meta"
#: Holders whose catalog write is published by a later refresh.
COOPERATIVE_HOLDERS = [
    "refresh_scheduler",
    "dependency_map_service",
    "memory_store@testhost/alice",  # a concurrent memory create
    "xray-pattern:store:other-pattern",
    "lifecycle_writer",
    "lifecycle_batch_runner",
    "meta_directory_updater",
]
#: Holders expecting exclusive access, with the category a refusal names.
EXCLUSIVE_HOLDERS = {
    "mcp_write_mode": "write mode",
    "catalog_backup": "another exclusive operation",
    "reconciliation": "another exclusive operation",
    "restore@testhost/bob": "another exclusive operation",
}
PATTERN_YAML = """\
name: test-pattern
description: "A test pattern"
language: java
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }
"""


class _CountingDebouncer:
    def __init__(self) -> None:
        self.signals = 0

    def signal_dirty(self) -> None:
        self.signals += 1

    def note_direct_write(self) -> int:
        return 1

    def track_submission(self, job_id: str, seq: int) -> None:
        raise AssertionError("a piggybacked write submits no refresh")


def _store_resolver(kind: str, tmp_path: Path) -> Any:
    if kind == "sqlite":
        from code_indexer.server.services.alias_lock_store.sqlite_store import (
            SqliteAliasLockStore,
        )

        store: Any = SqliteAliasLockStore(tmp_path / "alias_locks")
        return lambda: store
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
    pg_store = PostgresAliasLockStore(dsn)
    assert not pg_store.is_held(CATALOG), "a previous test left the lock held"
    return lambda: pg_store


@pytest.fixture(params=["file", "sqlite", "postgres"])
def scheduler(request: pytest.FixtureRequest, tmp_path: Path) -> RefreshScheduler:
    golden = tmp_path / "golden-repos"
    (golden / CATALOG).mkdir(parents=True)
    db_kwargs: Dict[str, Any] = {}
    if request.param != "file":
        db_kwargs = {
            "alias_lock_db_backed_enabled_getter": lambda: True,
            "alias_lock_store_resolver": _store_resolver(request.param, tmp_path),
        }
    config_source = MagicMock()
    config_source.get_global_refresh_interval.return_value = 3600
    return RefreshScheduler(
        golden_repos_dir=str(golden),
        config_source=config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=MagicMock(),
        golden_repo_metadata_backend=real_metadata_store(tmp_path / "server-data"),
        **db_kwargs,
    )


@pytest.fixture
def debouncer(monkeypatch: pytest.MonkeyPatch) -> _CountingDebouncer:
    owed = _CountingDebouncer()
    monkeypatch.setattr(meta_description_hook, "_debouncer", owed)
    return owed


@pytest.fixture(params=COOPERATIVE_HOLDERS + list(EXCLUSIVE_HOLDERS))
def holder(
    request: pytest.FixtureRequest, scheduler: RefreshScheduler
) -> Iterator[str]:
    owner = str(request.param)
    assert scheduler.write_lock_manager.acquire(CATALOG, owner_name=owner)
    try:
        yield owner
    finally:
        assert scheduler.write_lock_manager.release(CATALOG, owner_name=owner)


def _assert_neutral_refusal(message: str, holder: str) -> None:
    assert EXCLUSIVE_HOLDERS[holder] in message, message
    assert holder not in message, message
    assert "bob" not in message, message


def _memory_service(
    tmp_path: Path, scheduler: RefreshScheduler, debouncer: _CountingDebouncer
) -> MemoryStoreService:
    return MemoryStoreService(
        config=MemoryStoreConfig(memories_dir=tmp_path / "memories"),
        lock_manager=MemoryFileLockManager(tmp_path / "memory-locks"),
        refresh_scheduler=scheduler,
        refresh_debouncer=debouncer,
        rate_limiter=MemoryRateLimiter(
            RateLimitConfig(capacity=100, refill_per_second=100.0)
        ),
        hostname="testhost",
    )


def test_memory_create_respects_the_holder(
    tmp_path: Path,
    scheduler: RefreshScheduler,
    debouncer: _CountingDebouncer,
    holder: str,
) -> None:
    service = _memory_service(tmp_path, scheduler, debouncer)
    payload = {
        "type": "architectural-fact",
        "scope": "global",
        "summary": "example fact",
        "evidence": [{"commit": "abc123"}],
    }
    memories = tmp_path / "memories"

    if holder in EXCLUSIVE_HOLDERS:
        with pytest.raises(ConflictError) as refused:
            service.create_memory(payload, username="carol")
        # MCP returns str(exc) as its message, REST as the 423 detail.
        _assert_neutral_refusal(str(refused.value), holder)
        assert not memories.exists() or not any(memories.iterdir())
        assert debouncer.signals == 0
    else:
        created = service.create_memory(payload, username="carol")
        assert (memories / f"{created['id']}.md").exists()
        assert debouncer.signals == 1, "the piggybacked write was not owed"


def test_xray_pattern_store_respects_the_holder(
    tmp_path: Path,
    scheduler: RefreshScheduler,
    debouncer: _CountingDebouncer,
    holder: str,
) -> None:
    catalog = Path(scheduler.golden_repos_dir) / CATALOG
    service = XrayPatternService(catalog, refresh_scheduler=scheduler)
    target = catalog / "xray-patterns" / "__any__" / "test-pattern.yaml"

    result = service.store_xray_pattern_audited(
        scope="__any__",
        pattern_yaml=PATTERN_YAML,
        overwrite=False,
        actor="carol",
        may_replace_global=True,
    )

    if holder in EXCLUSIVE_HOLDERS:
        assert result.get("error") == "catalog_locked", result
        _assert_neutral_refusal(str(result.get("message")), holder)
        assert not target.exists()
        assert debouncer.signals == 0
    else:
        assert result.get("success") is True, result
        assert target.exists()
        assert debouncer.signals == 1, "the piggybacked write was not owed"


def test_a_refused_memory_write_keeps_the_rate_limit_token(
    tmp_path: Path, scheduler: RefreshScheduler, debouncer: _CountingDebouncer
) -> None:
    service = MemoryStoreService(
        config=MemoryStoreConfig(memories_dir=tmp_path / "memories"),
        lock_manager=MemoryFileLockManager(tmp_path / "memory-locks"),
        refresh_scheduler=scheduler,
        refresh_debouncer=debouncer,
        rate_limiter=MemoryRateLimiter(
            RateLimitConfig(capacity=1, refill_per_second=0.0001)
        ),
        hostname="testhost",
    )
    payload = {
        "type": "architectural-fact",
        "scope": "global",
        "summary": "example fact",
        "evidence": [{"commit": "abc123"}],
    }
    locks = scheduler.write_lock_manager
    assert locks.acquire(CATALOG, owner_name="mcp_write_mode")
    try:
        with pytest.raises(ConflictError):
            service.create_memory(payload, username="carol")
    finally:
        assert locks.release(CATALOG, owner_name="mcp_write_mode")

    created = service.create_memory(payload, username="carol")
    assert (tmp_path / "memories" / f"{created['id']}.md").exists()
