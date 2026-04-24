"""
Story #889 AC4 — hub tool reuses _aggregate_graph() helper.

No duplicate parsing logic. Single source of truth for aggregation.

Verified by:
1. _aggregate_graph importable from dep_map_parser_graph.
2. _aggregate_graph returns (edge_data, anomalies) — edge_data is non-empty with (src,tgt) keys.
3. Hub handler calls _aggregate_graph (positive) and does NOT call DepMapMCPParser (negative).
"""

import json
from pathlib import Path
from unittest.mock import patch

from tests.unit.server.mcp.test_dep_map_889_fixtures import (
    _call_hub,
    _make_hub_graph,
    _write_domain_md,
)


class TestAC4AggregateGraphReuse:
    """AC4: _aggregate_graph is the single aggregation source used by the hub handler."""

    def test_aggregate_graph_importable(self) -> None:
        """_aggregate_graph must be importable from dep_map_parser_graph."""
        from code_indexer.server.services.dep_map_parser_graph import _aggregate_graph

        assert callable(_aggregate_graph)

    def test_aggregate_graph_returns_non_empty_tuple_keyed_dict(
        self, tmp_path: Path
    ) -> None:
        """_aggregate_graph returns (edge_data, anomalies).

        edge_data must be non-empty and contain the expected (src,tgt) key.
        """
        from code_indexer.server.services.dep_map_parser_graph import _aggregate_graph

        dep_map_dir = tmp_path / "dependency-map"
        dep_map_dir.mkdir()
        domains = [
            {"name": "a", "description": "d", "participating_repos": []},
            {"name": "b", "description": "d", "participating_repos": []},
        ]
        (dep_map_dir / "_domains.json").write_text(
            json.dumps(domains), encoding="utf-8"
        )
        _write_domain_md(
            dep_map_dir,
            "a",
            outgoing=[
                {
                    "this_repo": "r1",
                    "depends_on": "r2",
                    "target_domain": "b",
                    "dep_type": "Code-level",
                },
            ],
        )
        _write_domain_md(dep_map_dir, "b")

        edge_data, anomalies = _aggregate_graph(dep_map_dir)
        assert isinstance(edge_data, dict)
        assert isinstance(anomalies, list)
        assert edge_data, "edge_data must not be empty for a graph with one edge"
        assert ("a", "b") in edge_data, (
            f"Expected ('a','b') in edge_data keys, got {list(edge_data.keys())}"
        )

    def test_hub_handler_calls_aggregate_graph_not_mcp_parser(
        self, tmp_path: Path
    ) -> None:
        """Hub handler calls _aggregate_graph (positive) and NOT DepMapMCPParser (negative).

        DepMapMCPParser is never imported at module level in depmap.py — it is only
        used via local import inside _resolve_parser (which non-hub handlers call).
        This test confirms:
        1. _aggregate_graph IS called by the hub handler (patched at depmap import path).
        2. DepMapMCPParser is NOT used — patched at its source module to raise if touched.
           Since the hub handler does not import or call it, the patch is never triggered.
        """
        root = _make_hub_graph(tmp_path)

        from code_indexer.server.services.dep_map_parser_graph import (
            _aggregate_graph as real_fn,
        )

        aggregate_calls = []

        def recording_aggregate(dep_map_dir):
            aggregate_calls.append(dep_map_dir)
            return real_fn(dep_map_dir)

        with patch(
            "code_indexer.server.mcp.handlers.depmap._aggregate_graph",
            side_effect=recording_aggregate,
        ):
            with patch(
                "code_indexer.server.services.dep_map_mcp_parser.DepMapMCPParser",
                side_effect=AssertionError(
                    "AC4 violated: hub handler must not instantiate DepMapMCPParser"
                ),
            ):
                data = _call_hub({}, root)

        assert data["success"] is True, (
            f"Hub handler failed when _aggregate_graph was patched: {data}"
        )
        assert len(aggregate_calls) >= 1, (
            "AC4: hub handler did not call _aggregate_graph"
        )
