"""Projection at fleet scale: 900 repositories, 50 domains.

Real files and real IndexRegenerator output; a static registry rows source.
Neutral names only.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

from code_indexer.server.services.cidx_meta_access import (
    AliasUniverse,
    MetaVisibility,
    load_depmap_model,
    project_lines,
    projection_cache_stats,
)
from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator

REPOS = [f"repo-{n:04d}" for n in range(1, 901)]
DOMAINS = 50
PER_DOMAIN = 18


def _build(tmp_path: Path) -> Any:
    dep_map = tmp_path / "dependency-map"
    dep_map.mkdir()
    domains: List[Dict[str, Any]] = []
    for d in range(DOMAINS):
        members = REPOS[d * PER_DOMAIN : (d + 1) * PER_DOMAIN]
        name = f"domain-{d:02d}"
        domains.append(
            {"name": name, "description": f"Domain {d}", "participating_repos": members}
        )
        frontmatter = "\n".join(
            ["---", f"domain: {name}", "participating_repos:"]
            + [f"  - {m}" for m in members]
            + ["---", "", f"# Domain Analysis: {name}", ""]
            + [f"{m} works with {members[0]}." for m in members]
        )
        (dep_map / f"{name}.md").write_text(frontmatter + "\n", encoding="utf-8")
    (dep_map / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")
    IndexRegenerator().regenerate(dep_map)

    return dep_map, _StaticRegistry(REPOS)


class _StaticRegistry:
    """The registry's list_repos() rows for 900 repositories, without 900
    individual inserts."""

    def __init__(self, repos: List[str]) -> None:
        self._rows = {
            f"{r}-global": {
                "repo_name": r,
                "repo_url": None,
                "index_path": f"/golden/{r}",
            }
            for r in repos
        }

    def list_repos(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._rows)


def test_index_projection_at_fleet_scale(tmp_path: Path) -> None:
    dep_map, registry = _build(tmp_path)
    index_lines = (dep_map / "_index.md").read_text(encoding="utf-8").splitlines()
    domain_lines = (dep_map / "domain-00.md").read_text(encoding="utf-8").splitlines()
    assert sum(1 for line in index_lines if line.startswith("| repo-")) == 900

    vis = MetaVisibility.restricted(REPOS[:10])
    model = load_depmap_model(dep_map)
    universe = AliasUniverse(registry).snapshot()

    before = projection_cache_stats()
    started = time.monotonic()
    projected = project_lines(
        index_lines, "dependency-map/_index.md", vis, model, universe
    )
    elapsed = time.monotonic() - started
    project_lines(domain_lines, "dependency-map/domain-00.md", vis, model, universe)
    after = projection_cache_stats()

    assert elapsed < 1.0
    assert after.view_builds == before.view_builds + 1
    assert len(projected) == len(index_lines)
    kept_rows = [
        line for line in projected if line is not None and line.startswith("| repo-")
    ]
    assert len(kept_rows) == 10
    text = "\n".join(line for line in projected if line is not None)
    assert "repos_analyzed_count: 10" in text
    assert "domains_count: 1" in text
    assert "| domain-00 | Domain 0 | 10 |" in text
    assert "repo-0011" not in text
    assert "domain-01" not in text
