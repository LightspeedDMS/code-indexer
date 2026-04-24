"""
Story #889 — AC1, AC2: Graph filter params on depmap_get_cross_domain_graph.

AC1: depmap_get_cross_domain_graph accepts optional filter params:
     source_domain (str | list[str]), target_domain (str | list[str]), min_count (int).
     Omitted filters → current full-graph behavior (backward-compat).

AC2: Multiple filters narrow conjunctively. Every returned edge matches all
     provided filters.

Invariant (MESSI rule 15, AC2):
     assert all(matches_filter(edge) for edge in returned_edges)
     — stripped under python -O.
"""

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Test helpers — mirror the 888 fixture pattern
# ---------------------------------------------------------------------------


def _make_user() -> MagicMock:
    user = MagicMock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    return user


def _make_app_state(read_path: Path) -> MagicMock:
    state = MagicMock()
    state.dependency_map_service.cidx_meta_read_path = read_path
    return state


def _parse_response(result: Any) -> Dict[str, Any]:
    return json.loads(result["content"][0]["text"])


def _call_graph(params: dict, root: Path) -> Dict[str, Any]:
    from code_indexer.server.mcp.handlers.depmap import (
        depmap_get_cross_domain_graph_handler,
    )

    state = _make_app_state(root)
    with patch(
        "code_indexer.server.mcp.handlers.depmap._utils.app_module.app.state",
        state,
    ):
        result = depmap_get_cross_domain_graph_handler(params, _make_user())
    return _parse_response(result)


def _write_domain_md(
    dep_map_dir: Path,
    domain: str,
    outgoing: List[Dict[str, str]] = None,
    incoming: List[Dict[str, str]] = None,
) -> None:
    """Write a minimal domain markdown file."""
    out_rows = "".join(
        f"| {r['this_repo']} | {r['depends_on']} | {r['target_domain']} "
        f"| {r.get('dep_type', 'Code-level')} | why | ev |\n"
        for r in (outgoing or [])
    )
    in_rows = "".join(
        f"| {r['external_repo']} | {r['depends_on']} | {r['source_domain']} "
        "| Code-level | why | ev |\n"
        for r in (incoming or [])
    )
    content = (
        f"---\nname: {domain}\n---\n"
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        f"{out_rows}"
        "\n### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        f"{in_rows}"
    )
    (dep_map_dir / f"{domain}.md").write_text(content, encoding="utf-8")


def _make_multi_edge_graph(tmp_path: Path) -> Path:
    """Three domains with two directed edges:
       alpha -> beta  (count=1, dep_type=Code-level)
       alpha -> gamma (count=1, dep_type=Build)
    beta has an incoming from alpha; gamma has an incoming from alpha.
    """
    import json

    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    domains = [
        {"name": "alpha", "description": "d", "participating_repos": []},
        {"name": "beta", "description": "d", "participating_repos": []},
        {"name": "gamma", "description": "d", "participating_repos": []},
    ]
    (dep_map_dir / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")

    _write_domain_md(
        dep_map_dir,
        "alpha",
        outgoing=[
            {
                "this_repo": "r1",
                "depends_on": "r2",
                "target_domain": "beta",
                "dep_type": "Code-level",
            },
            {
                "this_repo": "r1",
                "depends_on": "r3",
                "target_domain": "gamma",
                "dep_type": "Build",
            },
        ],
    )
    _write_domain_md(
        dep_map_dir,
        "beta",
        incoming=[
            {"external_repo": "r1", "depends_on": "r2", "source_domain": "alpha"}
        ],
    )
    _write_domain_md(
        dep_map_dir,
        "gamma",
        incoming=[
            {"external_repo": "r1", "depends_on": "r3", "source_domain": "alpha"}
        ],
    )
    return tmp_path


def _make_multi_count_graph(tmp_path: Path) -> Path:
    """Two domains: alpha -> beta with 3 outgoing rows (count=3).
    plus: gamma -> delta with 1 outgoing row (count=1).
    """
    import json

    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    domains = [
        {"name": "alpha", "description": "d", "participating_repos": []},
        {"name": "beta", "description": "d", "participating_repos": []},
        {"name": "gamma", "description": "d", "participating_repos": []},
        {"name": "delta", "description": "d", "participating_repos": []},
    ]
    (dep_map_dir / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")

    _write_domain_md(
        dep_map_dir,
        "alpha",
        outgoing=[
            {
                "this_repo": "r1",
                "depends_on": "r2",
                "target_domain": "beta",
                "dep_type": "Code-level",
            },
            {
                "this_repo": "r1",
                "depends_on": "r3",
                "target_domain": "beta",
                "dep_type": "Build",
            },
            {
                "this_repo": "r1",
                "depends_on": "r4",
                "target_domain": "beta",
                "dep_type": "Config",
            },
        ],
    )
    _write_domain_md(
        dep_map_dir,
        "beta",
        incoming=[
            {"external_repo": "r1", "depends_on": "r2", "source_domain": "alpha"},
            {"external_repo": "r1", "depends_on": "r3", "source_domain": "alpha"},
            {"external_repo": "r1", "depends_on": "r4", "source_domain": "alpha"},
        ],
    )
    _write_domain_md(
        dep_map_dir,
        "gamma",
        outgoing=[
            {
                "this_repo": "r5",
                "depends_on": "r6",
                "target_domain": "delta",
                "dep_type": "Code-level",
            },
        ],
    )
    _write_domain_md(
        dep_map_dir,
        "delta",
        incoming=[
            {"external_repo": "r5", "depends_on": "r6", "source_domain": "gamma"}
        ],
    )
    return tmp_path


# ---------------------------------------------------------------------------
# AC1: filter params are accepted (backward-compat: no params → full graph)
# ---------------------------------------------------------------------------


class TestAC1FilterParamsAccepted:
    """AC1: Optional filter params accepted; omitting all → full-graph (backward-compat)."""

    def test_no_params_returns_full_graph(self, tmp_path: Path) -> None:
        """Calling with empty params returns all edges (backward-compat)."""
        root = _make_multi_edge_graph(tmp_path)
        data = _call_graph({}, root)
        assert data["success"] is True
        assert data["resolution"] == "ok"
        assert len(data["edges"]) == 2

    def test_source_domain_string_param_accepted(self, tmp_path: Path) -> None:
        """source_domain as str is accepted without error."""
        root = _make_multi_edge_graph(tmp_path)
        data = _call_graph({"source_domain": "alpha"}, root)
        assert data["success"] is True
        assert data["resolution"] == "ok"

    def test_target_domain_string_param_accepted(self, tmp_path: Path) -> None:
        """target_domain as str is accepted without error."""
        root = _make_multi_edge_graph(tmp_path)
        data = _call_graph({"target_domain": "beta"}, root)
        assert data["success"] is True
        assert data["resolution"] == "ok"

    def test_min_count_int_param_accepted(self, tmp_path: Path) -> None:
        """min_count as int is accepted without error."""
        root = _make_multi_edge_graph(tmp_path)
        data = _call_graph({"min_count": 1}, root)
        assert data["success"] is True
        assert data["resolution"] == "ok"

    def test_source_domain_list_param_accepted(self, tmp_path: Path) -> None:
        """source_domain as list[str] is accepted without error."""
        root = _make_multi_edge_graph(tmp_path)
        data = _call_graph({"source_domain": ["alpha", "beta"]}, root)
        assert data["success"] is True
        assert data["resolution"] == "ok"

    def test_target_domain_list_param_accepted(self, tmp_path: Path) -> None:
        """target_domain as list[str] is accepted without error."""
        root = _make_multi_edge_graph(tmp_path)
        data = _call_graph({"target_domain": ["beta", "gamma"]}, root)
        assert data["success"] is True
        assert data["resolution"] == "ok"

    def test_all_filters_omitted_returns_same_as_baseline(self, tmp_path: Path) -> None:
        """Calling with None filter values returns same as no params (backward-compat)."""
        root = _make_multi_edge_graph(tmp_path)
        no_params = _call_graph({}, root)
        with_nones = _call_graph(
            {"source_domain": None, "target_domain": None, "min_count": None}, root
        )
        # Edge counts must be identical
        assert len(no_params["edges"]) == len(with_nones["edges"])


# ---------------------------------------------------------------------------
# AC1: source_domain filter narrows correctly
# ---------------------------------------------------------------------------


class TestAC1SourceDomainFilter:
    """AC1: source_domain filter returns only edges from matching source."""

    def test_source_domain_str_filters_edges(self, tmp_path: Path) -> None:
        """source_domain='alpha' returns only edges where source_domain='alpha'."""
        root = _make_multi_edge_graph(tmp_path)
        data = _call_graph({"source_domain": "alpha"}, root)
        assert data["success"] is True
        for edge in data["edges"]:
            assert edge["source_domain"] == "alpha", (
                f"Expected source_domain='alpha', got {edge['source_domain']!r}"
            )

    def test_source_domain_list_filters_edges(self, tmp_path: Path) -> None:
        """source_domain=['alpha'] returns only edges from alpha."""
        root = _make_multi_edge_graph(tmp_path)
        data = _call_graph({"source_domain": ["alpha"]}, root)
        assert all(e["source_domain"] == "alpha" for e in data["edges"])

    def test_source_domain_no_match_returns_empty(self, tmp_path: Path) -> None:
        """source_domain='nonexistent' returns empty edges list."""
        root = _make_multi_edge_graph(tmp_path)
        data = _call_graph({"source_domain": "nonexistent"}, root)
        assert data["success"] is True
        assert data["edges"] == []

    def test_source_domain_list_multi_returns_union(self, tmp_path: Path) -> None:
        """source_domain=['alpha', 'beta'] returns edges from alpha OR beta."""
        root = _make_multi_count_graph(tmp_path)
        data = _call_graph({"source_domain": ["alpha", "gamma"]}, root)
        sources = {e["source_domain"] for e in data["edges"]}
        assert sources.issubset({"alpha", "gamma"}), (
            f"Expected sources subset of {{'alpha','gamma'}}, got {sources}"
        )
        # Both alpha->beta and gamma->delta should appear
        assert len(data["edges"]) == 2


# ---------------------------------------------------------------------------
# AC1: target_domain filter narrows correctly
# ---------------------------------------------------------------------------


class TestAC1TargetDomainFilter:
    """AC1: target_domain filter returns only edges to matching target."""

    def test_target_domain_str_filters_edges(self, tmp_path: Path) -> None:
        """target_domain='beta' returns only edges where target_domain='beta'."""
        root = _make_multi_edge_graph(tmp_path)
        data = _call_graph({"target_domain": "beta"}, root)
        assert data["success"] is True
        for edge in data["edges"]:
            assert edge["target_domain"] == "beta"

    def test_target_domain_list_filters_edges(self, tmp_path: Path) -> None:
        """target_domain=['beta', 'gamma'] returns edges to beta OR gamma."""
        root = _make_multi_edge_graph(tmp_path)
        data = _call_graph({"target_domain": ["beta", "gamma"]}, root)
        targets = {e["target_domain"] for e in data["edges"]}
        assert targets.issubset({"beta", "gamma"})

    def test_target_domain_no_match_returns_empty(self, tmp_path: Path) -> None:
        """target_domain='nonexistent' returns empty edges."""
        root = _make_multi_edge_graph(tmp_path)
        data = _call_graph({"target_domain": "nonexistent"}, root)
        assert data["edges"] == []


# ---------------------------------------------------------------------------
# AC1: min_count filter narrows correctly
# ---------------------------------------------------------------------------


class TestAC1MinCountFilter:
    """AC1: min_count filter returns only edges with dependency_count >= min_count."""

    def test_min_count_1_returns_all(self, tmp_path: Path) -> None:
        """min_count=1 returns all edges (all have count >= 1)."""
        root = _make_multi_count_graph(tmp_path)
        all_data = _call_graph({}, root)
        filtered = _call_graph({"min_count": 1}, root)
        assert len(filtered["edges"]) == len(all_data["edges"])

    def test_min_count_3_filters_low_count_edges(self, tmp_path: Path) -> None:
        """min_count=3 returns only edges with dependency_count >= 3."""
        root = _make_multi_count_graph(tmp_path)
        data = _call_graph({"min_count": 3}, root)
        assert data["success"] is True
        for edge in data["edges"]:
            assert edge["dependency_count"] >= 3, (
                f"Edge {edge['source_domain']}→{edge['target_domain']} has "
                f"count={edge['dependency_count']} < min_count=3"
            )
        # alpha->beta has count=3; gamma->delta has count=1
        assert len(data["edges"]) == 1
        assert data["edges"][0]["source_domain"] == "alpha"
        assert data["edges"][0]["target_domain"] == "beta"

    def test_min_count_99_returns_empty(self, tmp_path: Path) -> None:
        """min_count=99 returns empty list when no edge has count >= 99."""
        root = _make_multi_count_graph(tmp_path)
        data = _call_graph({"min_count": 99}, root)
        assert data["edges"] == []


# ---------------------------------------------------------------------------
# AC2: AND-composition — all filters narrow conjunctively
# ---------------------------------------------------------------------------


class TestAC2ANDComposition:
    """AC2: Multiple filters narrow conjunctively. Every edge matches all filters."""

    def test_source_and_target_and_filters(self, tmp_path: Path) -> None:
        """source_domain AND target_domain must both match."""
        root = _make_multi_edge_graph(tmp_path)
        data = _call_graph({"source_domain": "alpha", "target_domain": "beta"}, root)
        assert data["success"] is True
        for edge in data["edges"]:
            assert edge["source_domain"] == "alpha"
            assert edge["target_domain"] == "beta"
        # Only alpha->beta matches (alpha->gamma is excluded)
        assert len(data["edges"]) == 1

    def test_source_and_min_count_filters(self, tmp_path: Path) -> None:
        """source_domain AND min_count must both match."""
        root = _make_multi_count_graph(tmp_path)
        # alpha->beta has count=3; gamma->delta has count=1
        data = _call_graph({"source_domain": "alpha", "min_count": 2}, root)
        assert data["success"] is True
        assert len(data["edges"]) == 1
        assert data["edges"][0]["source_domain"] == "alpha"

    def test_target_and_min_count_filters(self, tmp_path: Path) -> None:
        """target_domain AND min_count must both match."""
        root = _make_multi_count_graph(tmp_path)
        # beta receives count=3 from alpha; delta receives count=1 from gamma
        data = _call_graph({"target_domain": "beta", "min_count": 2}, root)
        assert data["success"] is True
        for edge in data["edges"]:
            assert edge["target_domain"] == "beta"
            assert edge["dependency_count"] >= 2

    def test_all_three_filters_conjunctive(self, tmp_path: Path) -> None:
        """source_domain AND target_domain AND min_count all narrow the result."""
        root = _make_multi_count_graph(tmp_path)
        data = _call_graph(
            {"source_domain": "alpha", "target_domain": "beta", "min_count": 3},
            root,
        )
        assert data["success"] is True
        assert len(data["edges"]) == 1
        e = data["edges"][0]
        assert e["source_domain"] == "alpha"
        assert e["target_domain"] == "beta"
        assert e["dependency_count"] >= 3

    def test_contradictory_filters_return_empty(self, tmp_path: Path) -> None:
        """Contradictory filters (no edge can satisfy both) return empty edges."""
        root = _make_multi_edge_graph(tmp_path)
        # alpha->beta exists but with source=alpha; asking source=beta AND target=alpha
        # (no such edge)
        data = _call_graph({"source_domain": "beta", "target_domain": "alpha"}, root)
        assert data["success"] is True
        assert data["edges"] == []

    def test_invariant_all_returned_edges_match_all_filters(
        self, tmp_path: Path
    ) -> None:
        """Invariant: every returned edge matches ALL provided filters."""
        root = _make_multi_count_graph(tmp_path)
        source_filter = "alpha"
        min_count_filter = 2
        data = _call_graph(
            {"source_domain": source_filter, "min_count": min_count_filter}, root
        )
        for edge in data["edges"]:
            assert edge["source_domain"] == source_filter, (
                f"AC2 invariant: edge source {edge['source_domain']!r} "
                f"!= filter {source_filter!r}"
            )
            assert edge["dependency_count"] >= min_count_filter, (
                f"AC2 invariant: edge count {edge['dependency_count']} "
                f"< min_count {min_count_filter}"
            )
