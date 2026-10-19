"""Caches of the projection engine: model, projected documents, universe.

Real directories (a live one and one under a .versioned snapshot layout)
and a real SQLite registry. Neutral names only.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List

import pytest

from code_indexer.server.services.cidx_meta_access import (
    AliasUniverse,
    MetaVisibility,
    load_depmap_model,
    project_lines,
    projection_cache_stats,
    resolve_meta_visibility,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    CANARY,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
    read_lines,
)


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path)


def _retarget_ledger(dep_map_dir: Path) -> None:
    ledger = dep_map_dir / "ledger.md"
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace(
            f"  - {ALPHA}\n", f"  - {CANARY}\n", 1
        ),
        encoding="utf-8",
    )


def test_unchanged_live_directory_reuses_the_model(fx: CidxMetaFixture) -> None:
    assert load_depmap_model(fx.dep_map_dir) is load_depmap_model(fx.dep_map_dir)


def test_live_directory_edit_yields_a_fresh_model(fx: CidxMetaFixture) -> None:
    before = load_depmap_model(fx.dep_map_dir)
    _retarget_ledger(fx.dep_map_dir)
    after = load_depmap_model(fx.dep_map_dir)
    assert after is not before
    assert before.participants["ledger"] == frozenset({ALPHA})
    assert after.participants["ledger"] == frozenset({CANARY})


def test_immutable_snapshot_is_parsed_once_per_process(
    fx: CidxMetaFixture, tmp_path: Path
) -> None:
    snapshot = tmp_path / "golden" / ".versioned" / "cidx-meta" / "v_1700000000"
    snapshot_dep_map = snapshot / "dependency-map"
    shutil.copytree(fx.dep_map_dir, snapshot_dep_map)

    first = load_depmap_model(snapshot_dep_map)
    _retarget_ledger(snapshot_dep_map)
    second = load_depmap_model(snapshot_dep_map)

    assert second is first
    assert second.participants["ledger"] == frozenset({ALPHA})


def test_equal_visible_sets_share_projections_and_different_never(
    fx: CidxMetaFixture,
) -> None:
    lines = read_lines(fx.root / f"{ALPHA}.md") + [f"Unique to {fx.root}."]
    model = load_depmap_model(fx.dep_map_dir)
    universe = AliasUniverse(fx.registry).snapshot()
    user_vis = resolve_meta_visibility(RESTRICTED_USER, fx.access_service)
    same_set = MetaVisibility.restricted([f"{ALPHA}-global"])
    other_set = MetaVisibility.restricted([CANARY])

    start = projection_cache_stats()
    first = project_lines(lines, f"{ALPHA}.md", user_vis, model, universe)
    shared = project_lines(lines, f"{ALPHA}.md", same_set, model, universe)
    after_shared = projection_cache_stats()
    other = project_lines(lines, f"{ALPHA}.md", other_set, model, universe)
    end = projection_cache_stats()

    assert shared == first
    assert after_shared.projections_computed == start.projections_computed + 1
    assert after_shared.projection_hits == start.projection_hits + 1
    assert end.projections_computed == after_shared.projections_computed + 1
    assert other != first


class _CountingRegistry:
    """Delegates to the real registry and counts its reads."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self.reads = 0

    def list_repos(self) -> Dict[str, Dict[str, Any]]:
        self.reads += 1
        result: Dict[str, Dict[str, Any]] = self._registry.list_repos()
        return result


def test_alias_universe_is_read_once_per_ttl(fx: CidxMetaFixture) -> None:
    now: List[float] = [1000.0]
    registry = _CountingRegistry(fx.registry)
    universe = AliasUniverse(registry, ttl_seconds=60, clock=lambda: now[0])

    first = universe.snapshot()
    now[0] += 59
    assert universe.snapshot() is first
    assert registry.reads == 1
    assert CANARY in first.names and "cidx-meta" in first.names

    now[0] += 2
    unchanged = universe.snapshot()
    assert registry.reads == 2
    assert unchanged.generation == first.generation

    fx.registry.register_repo(
        alias_name="delta-sample-global",
        repo_name="delta-sample",
        repo_url=None,
        index_path="/golden/delta-sample",
    )
    now[0] += 61
    changed = universe.snapshot()
    assert changed.generation == first.generation + 1
    assert "delta-sample" in changed.names
    assert changed.digest != first.digest


def test_alias_universe_rejects_a_non_positive_ttl(fx: CidxMetaFixture) -> None:
    with pytest.raises(ValueError):
        AliasUniverse(fx.registry, ttl_seconds=0)
