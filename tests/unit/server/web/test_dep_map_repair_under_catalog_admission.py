"""The dependency-map repair (the Web repair route and the scheduled
auto-repair both run ``_repair_execute``) writes cidx-meta only under
catalog admission as the dependency map: under an exclusive holder (write
mode) it refuses before writing anything; with the catalog free it holds the
cidx-meta write lock as the dependency map while it rewrites the map, then
releases it and requests the catalog refresh.

Real components: the real repair executor, health detector and index
regenerator over a real dependency-map directory, the real RefreshScheduler
and its lock (file lock, SQLite and, with TEST_POSTGRES_DSN, PostgreSQL
alias-lock stores). The executor builder is replaced only to skip the
Claude dispatcher and server config it would otherwise wire.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, List

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.dep_map_health_detector import DepMapHealthDetector
from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator
from code_indexer.server.services.dep_map_repair_executor import DepMapRepairExecutor
from code_indexer.server.web import dependency_map_routes
from tests.unit.server.services.test_dep_map_repair_json_backfill import (
    _make_domain_md,
    _make_domains_json,
)
from tests.utils.catalog_lock_schedulers import (
    CATALOG,
    CATALOG_LOCK_KINDS,
    catalog_lock_scheduler,
)

DOMAINS = ["example-domain-a", "example-domain-b"]


@pytest.fixture(params=CATALOG_LOCK_KINDS)
def scheduler(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[RefreshScheduler]:
    real = catalog_lock_scheduler(str(request.param), tmp_path)
    triggered: List[str] = []

    def _trigger(self: Any, alias: str, *a: Any, **k: Any) -> str:
        triggered.append(alias)
        return "job-1"

    monkeypatch.setattr(RefreshScheduler, "trigger_refresh_for_repo", _trigger)
    monkeypatch.setattr(
        dependency_map_routes,
        "_build_repair_executor",
        lambda *a: DepMapRepairExecutor(
            health_detector=DepMapHealthDetector(),
            index_regenerator=IndexRegenerator(),
        ),
    )
    monkeypatch.setattr(dependency_map_routes, "_get_known_repo_names", lambda: None)
    real.triggered = triggered  # type: ignore[attr-defined]
    yield real


def _dependency_map(scheduler: RefreshScheduler) -> Path:
    output_dir = Path(scheduler.golden_repos_dir) / CATALOG / "dependency-map"
    output_dir.mkdir(parents=True)
    for name in DOMAINS:
        _make_domain_md(output_dir, name, description=f"About {name}")
    _make_domains_json(output_dir, DOMAINS, with_metadata=True)
    return output_dir


def _repair(scheduler: RefreshScheduler, output_dir: Path) -> bool:
    service = SimpleNamespace(_refresh_scheduler=scheduler, _job_tracker=None)
    return bool(dependency_map_routes._repair_execute(service, output_dir, None))


def test_repair_under_write_mode_writes_nothing(scheduler: RefreshScheduler) -> None:
    output_dir = _dependency_map(scheduler)
    locks = scheduler.write_lock_manager
    assert locks.acquire(CATALOG, owner_name="mcp_write_mode")
    try:
        assert _repair(scheduler, output_dir) is False
        assert not (output_dir / "_index.md").exists(), "repaired under write mode"
        info = locks.get_lock_info(CATALOG)
        assert info and info["owner"] == "mcp_write_mode"
    finally:
        assert locks.release(CATALOG, owner_name="mcp_write_mode")


def test_repair_holds_the_catalog_lock_as_the_dependency_map(
    scheduler: RefreshScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = _dependency_map(scheduler)
    holders: List[Any] = []
    real_regenerate = IndexRegenerator.regenerate

    def _observed(self: IndexRegenerator, out: Path) -> Path:
        info = scheduler.write_lock_manager.get_lock_info(CATALOG)
        holders.append(info["owner"] if info else None)
        return real_regenerate(self, out)

    monkeypatch.setattr(IndexRegenerator, "regenerate", _observed)

    assert _repair(scheduler, output_dir) is True

    assert (output_dir / "_index.md").exists()
    assert holders and set(holders) == {"dependency_map_service"}, (
        f"the map was rewritten without the catalog lock: {holders}"
    )
    assert scheduler.write_lock_manager.get_lock_info(CATALOG) is None
    assert scheduler.triggered == ["cidx-meta-global"], "no catalog refresh"  # type: ignore[attr-defined]


def test_repair_under_a_cooperative_holder_writes_nothing(
    scheduler: RefreshScheduler, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    output_dir = _dependency_map(scheduler)
    locks = scheduler.write_lock_manager
    assert locks.acquire(CATALOG, owner_name="refresh_scheduler")
    try:
        with caplog.at_level(logging.ERROR):
            assert _repair(scheduler, output_dir) is False
        assert not (output_dir / "_index.md").exists(), "piggybacked a repair"
        assert "busy" in caplog.text, caplog.text
        assert scheduler.triggered == []  # type: ignore[attr-defined]
        info = locks.get_lock_info(CATALOG)
        assert info and info["owner"] == "refresh_scheduler"
    finally:
        assert locks.release(CATALOG, owner_name="refresh_scheduler")


def test_index_is_put_in_place_whole_at_the_catalog_mode(tmp_path: Path) -> None:
    output_dir = tmp_path / CATALOG / "dependency-map"
    output_dir.mkdir(parents=True)
    for name in DOMAINS:
        _make_domain_md(output_dir, name, description=f"About {name}")
    _make_domains_json(output_dir, DOMAINS, with_metadata=True)
    index = output_dir / "_index.md"
    index.write_text("old index\n")
    reader_view = tmp_path / "reader-view"
    os.link(index, reader_view)
    previous_umask = os.umask(0o077)
    try:
        IndexRegenerator().regenerate(output_dir)
    finally:
        os.umask(previous_umask)

    assert "example-domain-a" in index.read_text()
    assert reader_view.read_text() == "old index\n", "rewritten in place"
    assert stat.S_IMODE(index.stat().st_mode) == 0o644
    assert sorted(p.name for p in output_dir.iterdir() if p.name.startswith(".")) == []
