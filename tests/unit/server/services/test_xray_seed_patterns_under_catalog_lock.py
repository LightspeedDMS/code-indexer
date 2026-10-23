"""The X-Ray seed patterns are written and committed under the catalog
write lock, as an X-Ray pattern writer (the cooperative owner a normal
pattern store uses), so their git add/commit never collides with the
backup's stage-all. Piggyback rules apply: under a cooperative holder the
seeds are written and left for that holder's refresh to commit; under an
exclusive holder nothing is written, a WARNING says so, and the call
reports False so the next xray_search retries -- never a silent loss.

Real components: the real RefreshScheduler with its file lock, a real
git-initialized cidx-meta, the real X-Ray pattern service.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos import meta_description_hook
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.xray_pattern_service import (
    XRAY_PATTERN_LOCK_OWNER_PREFIX,
    XrayPatternService,
)
from tests.fixtures.refresh_scheduler_stores import real_metadata_store

CATALOG = "cidx-meta"
SEEDS = [
    "xray-patterns/__any__/catch-rethrow.yaml",
    "xray-patterns/__any__/deep-nesting.yaml",
]


class _Debouncer:
    def __init__(self) -> None:
        self.signals = 0

    def signal_dirty(self) -> None:
        self.signals += 1

    def note_direct_write(self) -> int:
        return 1

    def track_submission(self, job_id: str, seq: int) -> None:
        pass


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    path = tmp_path / "golden-repos" / CATALOG
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("cidx-meta\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "initial")
    return path


@pytest.fixture
def triggers(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    requested: List[str] = []
    monkeypatch.setattr(meta_description_hook, "_debouncer", _Debouncer())
    monkeypatch.setattr(
        RefreshScheduler,
        "trigger_refresh_for_repo",
        lambda self, alias, *a, **k: requested.append(alias),
    )
    return requested


@pytest.fixture
def scheduler(tmp_path: Path, catalog: Path) -> RefreshScheduler:
    config_source = MagicMock()
    config_source.get_global_refresh_interval.return_value = 3600
    return RefreshScheduler(
        golden_repos_dir=str(catalog.parent),
        config_source=config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=MagicMock(),
        golden_repo_metadata_backend=real_metadata_store(tmp_path / "server-data"),
    )


def test_seeds_are_retried_after_an_exclusive_holder_refuses_them(
    catalog: Path,
    scheduler: RefreshScheduler,
    triggers: List[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = XrayPatternService(catalog, refresh_scheduler=scheduler)
    locks = scheduler.write_lock_manager
    assert locks.acquire(CATALOG, owner_name="mcp_write_mode")
    try:
        with caplog.at_level(logging.WARNING):
            assert service.ensure_seed_patterns() is False
        assert not [s for s in SEEDS if (catalog / s).exists()]
        assert any("seed" in r.getMessage() for r in caplog.records), caplog.text
    finally:
        assert locks.release(CATALOG, owner_name="mcp_write_mode")

    assert service.ensure_seed_patterns() is True
    assert set(SEEDS) <= set(_git(catalog, "ls-files").splitlines())


def test_seed_commit_runs_under_the_xray_writer_owner(
    catalog: Path,
    scheduler: RefreshScheduler,
    triggers: List[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = XrayPatternService(catalog, refresh_scheduler=scheduler)
    holders: List[Any] = []
    real_commit = XrayPatternService._git_commit

    def _observed(self: Any, files: List[Path], message: str) -> None:
        info = scheduler.write_lock_manager.get_lock_info(CATALOG)
        holders.append(info["owner"] if info else None)
        real_commit(self, files, message)

    monkeypatch.setattr(XrayPatternService, "_git_commit", _observed)

    assert service.ensure_seed_patterns() is True

    assert len(holders) == 1, holders
    assert str(holders[0]).startswith(XRAY_PATTERN_LOCK_OWNER_PREFIX), holders
    assert set(SEEDS) <= set(_git(catalog, "ls-files").splitlines())
    assert scheduler.write_lock_manager.get_lock_info(CATALOG) is None
    assert triggers == ["cidx-meta-global"], "no catalog refresh was requested"


def test_seeds_piggyback_on_a_cooperative_holder_without_committing(
    catalog: Path, scheduler: RefreshScheduler, triggers: List[str]
) -> None:
    service = XrayPatternService(catalog, refresh_scheduler=scheduler)
    head = _git(catalog, "rev-parse", "HEAD")
    locks = scheduler.write_lock_manager
    assert locks.acquire(CATALOG, owner_name="refresh_scheduler")
    try:
        assert service.ensure_seed_patterns() is True
        assert all((catalog / s).exists() for s in SEEDS)
        assert _git(catalog, "rev-parse", "HEAD") == head, "committed under a refresh"
        assert triggers == [], "a refresh requested now would skip on the held lock"
    finally:
        assert locks.release(CATALOG, owner_name="refresh_scheduler")


PATTERN_YAML = """\
name: my-pattern
description: "A test pattern"
language: java
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }
"""


def test_xray_search_retries_seeds_until_they_are_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.mcp.handlers.xray import _infra

    meta = tmp_path / CATALOG
    (meta / "xray-patterns" / "__any__").mkdir(parents=True)
    (meta / "xray-patterns" / "__any__" / "my-pattern.yaml").write_text(PATTERN_YAML)
    outcomes = [False, True]
    attempts: List[bool] = []

    def _seeds(self: Any) -> bool:
        attempts.append(True)
        return outcomes.pop(0)

    monkeypatch.setattr(XrayPatternService, "ensure_seed_patterns", _seeds)
    monkeypatch.setattr(_infra, "_seeds_ensured", False)

    def _search() -> None:
        code, _error = _infra._resolve_evaluator_code(
            {"pattern_name": "my-pattern"}, "example-repo-global", cidx_meta_path=meta
        )
        assert "evaluate_node" in code

    _search()
    assert _infra._seeds_ensured is False, "a refused seed write was latched as done"
    _search()
    assert _infra._seeds_ensured is True
    _search()
    assert len(attempts) == 2
