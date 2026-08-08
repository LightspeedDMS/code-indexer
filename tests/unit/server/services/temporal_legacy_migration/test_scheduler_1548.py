"""Issue #1548 blockers 4/9: scheduler locking + result reporting."""

import json
from pathlib import Path
from typing import Dict, List

import pytest

from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.services.temporal_legacy_migration.scheduler import (
    TemporalLegacyMigrationScheduler,
)
from code_indexer.server.utils.config_manager import TemporalLegacyMigrationConfig


_FAKE_DEFAULT_TTL_SECONDS = 3600


class _FakeWriteLockManager:
    def __init__(self):
        self.locked = set()
        self.acquire_calls: List[str] = []
        self.release_calls: List[str] = []

    def acquire(self, alias, *, owner_name, ttl_seconds=_FAKE_DEFAULT_TTL_SECONDS):
        self.acquire_calls.append(alias)
        if alias in self.locked:
            return False
        self.locked.add(alias)
        return True

    def release(self, alias, *, owner_name):
        self.release_calls.append(alias)
        self.locked.discard(alias)
        return True

    def renew(self, alias, *, owner_name, ttl_seconds=_FAKE_DEFAULT_TTL_SECONDS):
        return alias in self.locked


class _FakeRefreshScheduler:
    def __init__(self, *, refresh_in_progress_aliases=frozenset()):
        self.write_lock_manager = _FakeWriteLockManager()
        self._refresh_in_progress_aliases = refresh_in_progress_aliases
        self.held_during_check: List[bool] = []

    def check_refresh_not_in_progress(self, alias):
        # Records whether the write lock was already held at the point the
        # migration's own TOCTOU check runs -- proves lock-then-check
        # ordering, not merely that both happened at some point.
        self.held_during_check.append(alias in self.write_lock_manager.locked)
        if alias in self._refresh_in_progress_aliases:
            raise DuplicateJobError("global_repo_refresh", alias, "job-1")

    def release_write_lock(self, alias, *, owner_name):
        self.write_lock_manager.release(alias, owner_name=owner_name)


class _FakeConfigService:
    def __init__(self, settings: TemporalLegacyMigrationConfig):
        self._config = type("Cfg", (), {"temporal_legacy_migration_config": settings})()

    def get_config(self):
        return self._config


class _FakeGoldenRepoManager:
    def __init__(self, paths: Dict[str, Path]):
        self._paths = paths

    def list_golden_repos(self) -> List[Dict[str, str]]:
        return [{"alias": alias} for alias in self._paths]

    def get_actual_repo_path(self, alias: str) -> str:
        return str(self._paths[alias])


def _write_shard(legacy_index_dir: Path, name: str, point_id: str) -> None:
    shard = legacy_index_dir / name
    shard.mkdir(parents=True)
    (shard / f"vector_{point_id}.json").write_text(
        json.dumps({"id": point_id, "vector": [1.0]})
    )


def test_run_once_migrates_under_the_write_lock_and_returns_summary(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    (repo / ".code-indexer" / "index").mkdir(parents=True)
    _write_shard(
        repo / ".code-indexer" / "index",
        "code-indexer-temporal-e-2026Q1",
        "p1",
    )
    manager = _FakeGoldenRepoManager({"demo": repo})
    settings = TemporalLegacyMigrationConfig(relocation_enabled=True)
    refresh_scheduler = _FakeRefreshScheduler()
    scheduler = TemporalLegacyMigrationScheduler(
        golden_repo_manager=manager,
        config_service=_FakeConfigService(settings),
        refresh_scheduler=refresh_scheduler,
    )

    result = scheduler.run_once()

    assert result["published"] == 1
    assert result["failed"] == 0
    assert result["per_repo"]["demo"]["published"] == 1
    # Lock lifecycle: acquired, held while the in-progress check ran, and
    # released afterward -- never left dangling.
    assert refresh_scheduler.write_lock_manager.acquire_calls == ["demo"]
    assert refresh_scheduler.held_during_check == [True]
    assert refresh_scheduler.write_lock_manager.release_calls == ["demo"]
    assert "demo" not in refresh_scheduler.write_lock_manager.locked


def test_run_once_skips_repo_with_refresh_in_progress(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".code-indexer" / "index").mkdir(parents=True)
    _write_shard(
        repo / ".code-indexer" / "index",
        "code-indexer-temporal-e-2026Q1",
        "p1",
    )
    manager = _FakeGoldenRepoManager({"demo": repo})
    settings = TemporalLegacyMigrationConfig(relocation_enabled=True)
    scheduler = TemporalLegacyMigrationScheduler(
        golden_repo_manager=manager,
        config_service=_FakeConfigService(settings),
        refresh_scheduler=_FakeRefreshScheduler(refresh_in_progress_aliases={"demo"}),
    )

    result = scheduler.run_once()

    assert result["published"] == 0
    assert result["per_repo"] == {}


def test_run_once_is_a_noop_when_both_gates_disabled_even_called_directly(
    tmp_path: Path,
):
    """Issue #1548 review finding 4: run_once() must enforce the config
    gate ITSELF, not rely solely on _loop()'s pre-check before calling
    trigger_now() -- a direct caller (CLI, test, future admin trigger)
    bypasses that outer wrapper entirely.
    """
    repo = tmp_path / "repo"
    (repo / ".code-indexer" / "index").mkdir(parents=True)
    _write_shard(
        repo / ".code-indexer" / "index",
        "code-indexer-temporal-e-2026Q1",
        "p1",
    )
    manager = _FakeGoldenRepoManager({"demo": repo})
    settings = TemporalLegacyMigrationConfig()  # both gates default False
    refresh_scheduler = _FakeRefreshScheduler()
    scheduler = TemporalLegacyMigrationScheduler(
        golden_repo_manager=manager,
        config_service=_FakeConfigService(settings),
        refresh_scheduler=refresh_scheduler,
    )

    result = scheduler.run_once()

    assert result["published"] == 0
    assert result["failed"] == 0
    assert result["per_repo"] == {}
    # No candidate was ever discovered/processed -- the write lock was
    # never even touched.
    assert refresh_scheduler.write_lock_manager.acquire_calls == []


def test_constructor_requires_refresh_scheduler():
    with pytest.raises(ValueError):
        TemporalLegacyMigrationScheduler(
            golden_repo_manager=_FakeGoldenRepoManager({}),
            config_service=_FakeConfigService(TemporalLegacyMigrationConfig()),
            refresh_scheduler=None,
        )
