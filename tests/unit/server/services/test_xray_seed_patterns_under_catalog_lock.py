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
from typing import Any, Dict, List
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
from tests.utils.catalog_lock_schedulers import (
    CATALOG_LOCK_KINDS,
    catalog_lock_scheduler,
)

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


SEED_SCAN = {"driver_regex": r"catch", "pattern_name": "catch-rethrow"}
REPOS = [{"alias": "example-repo-global", "path": Path("/nonexistent")}]


@pytest.fixture(params=CATALOG_LOCK_KINDS)
def batch_scheduler(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    triggers: List[str],
    monkeypatch: pytest.MonkeyPatch,
) -> RefreshScheduler:
    from code_indexer.server.mcp.handlers.xray import _infra

    real = catalog_lock_scheduler(str(request.param), tmp_path)
    meta = Path(real.golden_repos_dir) / CATALOG
    _git(meta, "init", "-q")
    _git(meta, "config", "user.email", "test@example.com")
    _git(meta, "config", "user.name", "Test")
    (meta / "README.md").write_text("cidx-meta\n")
    _git(meta, "add", "README.md")
    _git(meta, "commit", "-q", "-m", "initial")
    monkeypatch.setattr(_infra, "_seeds_ensured", False)
    return real


class _RecordingJobManager:
    def __init__(self) -> None:
        self.funcs: List[Any] = []

    def submit_job(self, **kwargs: Any) -> str:
        self.funcs.append(kwargs["func"])
        return "job-1"


def test_the_batch_door_hands_the_app_scheduler_to_its_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timezone

    from code_indexer.server.auth.user_manager import User, UserRole
    from code_indexer.server.mcp.handlers import _utils, xray_batch

    scheduler = object()
    jobs = _RecordingJobManager()
    handed: List[Any] = []
    access = MagicMock()
    access.is_admin_user.return_value = True
    monkeypatch.setattr(_utils, "_get_app_refresh_scheduler", lambda: scheduler)
    monkeypatch.setattr(xray_batch, "_get_access_filtering_service", lambda: access)
    monkeypatch.setattr(xray_batch, "_resolve_repo_path", lambda alias: "/repos/x")
    monkeypatch.setattr(xray_batch, "_get_arm_and_grm", lambda: (None, None))
    monkeypatch.setattr(xray_batch, "_get_background_job_manager", lambda: jobs)
    monkeypatch.setattr(xray_batch, "_get_cidx_meta_path", lambda: tmp_path)

    def _spy_job(**kwargs: Any) -> Dict[str, Any]:
        handed.append(kwargs.get("refresh_scheduler"))
        return {}

    monkeypatch.setattr(xray_batch, "_run_xray_batch_job", _spy_job)
    user = User(
        username="example-user",
        password_hash="x",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    xray_batch.handle_xray_search_batch(
        {"repository_alias": "example-repo-global", "scans": [SEED_SCAN]}, user
    )
    jobs.funcs[0](lambda *a, **k: None)

    assert handed == [scheduler], "the batch job runs without the catalog lock"


def test_the_batch_job_refuses_seeds_under_write_mode(
    batch_scheduler: RefreshScheduler,
) -> None:
    from code_indexer.server.mcp.handlers import xray_batch
    from code_indexer.server.mcp.handlers.xray import _infra

    meta = Path(batch_scheduler.golden_repos_dir) / CATALOG
    head = _git(meta, "rev-parse", "HEAD")
    locks = batch_scheduler.write_lock_manager
    assert locks.acquire(CATALOG, owner_name="mcp_write_mode")
    try:
        resolved = xray_batch._resolve_batch_evaluators(
            REPOS, [SEED_SCAN], meta, batch_scheduler
        )
    finally:
        assert locks.release(CATALOG, owner_name="mcp_write_mode")

    code, error = resolved[("example-repo-global", 0)]
    assert code == "" and error is not None
    assert error.get("error") == "catalog_locked", error
    assert not [s for s in SEEDS if (meta / s).exists()], "seeds written"
    assert _git(meta, "rev-parse", "HEAD") == head, "seeds committed"
    assert _infra._seeds_ensured is False, "a refused seed write was latched"


def test_the_batch_job_piggybacks_seeds_on_a_refresh(
    batch_scheduler: RefreshScheduler,
) -> None:
    from code_indexer.server.mcp.handlers import xray_batch
    from code_indexer.server.mcp.handlers.xray import _infra

    meta = Path(batch_scheduler.golden_repos_dir) / CATALOG
    head = _git(meta, "rev-parse", "HEAD")
    locks = batch_scheduler.write_lock_manager
    assert locks.acquire(CATALOG, owner_name="refresh_scheduler")
    try:
        resolved = xray_batch._resolve_batch_evaluators(
            REPOS, [SEED_SCAN], meta, batch_scheduler
        )
    finally:
        assert locks.release(CATALOG, owner_name="refresh_scheduler")

    code, error = resolved[("example-repo-global", 0)]
    assert error is None and "evaluate_node" in code
    assert all((meta / s).exists() for s in SEEDS)
    assert _git(meta, "rev-parse", "HEAD") == head, "committed under a refresh"
    assert _infra._seeds_ensured is True


def test_a_seed_created_concurrently_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.global_repos import catalog_staged_write

    meta = tmp_path / "golden-repos" / CATALOG
    meta.mkdir(parents=True)
    _git(meta, "init", "-q")
    peer = meta / SEEDS[0]
    real_sweep = catalog_staged_write._sweep_stale_staging

    def sweep_then_peer_publishes(staging_dir: Path) -> None:
        real_sweep(staging_dir)
        if not peer.exists():
            peer.parent.mkdir(parents=True, exist_ok=True)
            peer.write_text("name: catch-rethrow\ndescription: peer\n")

    monkeypatch.setattr(
        catalog_staged_write, "_sweep_stale_staging", sweep_then_peer_publishes
    )

    assert XrayPatternService(meta).ensure_seed_patterns() is True

    assert peer.read_text() == "name: catch-rethrow\ndescription: peer\n"
    assert "evaluate_node" in (meta / SEEDS[1]).read_text()
