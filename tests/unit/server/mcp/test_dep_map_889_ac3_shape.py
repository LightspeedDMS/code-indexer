"""
Story #889 AC3 — depmap_get_hub_domains shape and ranking.

Covers:
- _compute_hub_domains signature defaults: top_n=5, by="total_degree"
- Hub entry shape: {domain, in_degree, out_degree, total}
- total == in_degree + out_degree invariant
- Ranking descending by each metric (out_degree, in_degree, total_degree)
- top_n limiting and empty-graph behavior
- Correct top hub for all three metrics in the fixture graph:
    out_degree  -> alpha (out=3)
    in_degree   -> delta (in=3)
    total_degree -> delta (total=4)
"""

import inspect
from pathlib import Path

from tests.unit.server.mcp.test_dep_map_889_fixtures import (
    _call_hub,
    _make_empty_graph,
    _make_hub_graph,
)


class TestAC3SignatureDefaults:
    """_compute_hub_domains exposes inspectable defaults: top_n=5, by='total_degree'."""

    def test_compute_hub_domains_importable(self) -> None:
        from code_indexer.server.mcp.handlers.depmap import _compute_hub_domains

        assert callable(_compute_hub_domains)

    def test_top_n_default_is_5(self) -> None:
        from code_indexer.server.mcp.handlers.depmap import _compute_hub_domains

        sig = inspect.signature(_compute_hub_domains)
        param = sig.parameters.get("top_n")
        assert param is not None, "_compute_hub_domains must have 'top_n' param"
        assert param.default == 5, f"top_n default expected 5, got {param.default!r}"

    def test_by_default_is_total_degree(self) -> None:
        from code_indexer.server.mcp.handlers.depmap import _compute_hub_domains

        sig = inspect.signature(_compute_hub_domains)
        param = sig.parameters.get("by")
        assert param is not None, "_compute_hub_domains must have 'by' param"
        assert param.default == "total_degree", (
            f"by default expected 'total_degree', got {param.default!r}"
        )

    def test_default_by_equals_explicit_total_degree(self, tmp_path: Path) -> None:
        root = _make_hub_graph(tmp_path)
        default_data = _call_hub({}, root)
        explicit_data = _call_hub({"by": "total_degree"}, root)
        assert default_data["hubs"] == explicit_data["hubs"]


class TestAC3HubEntryShape:
    """Each hub entry has domain, in_degree, out_degree, total fields with correct types."""

    def test_success_and_resolution_ok(self, tmp_path: Path) -> None:
        root = _make_hub_graph(tmp_path)
        data = _call_hub({}, root)
        assert data["success"] is True
        assert data["resolution"] == "ok"
        assert "hubs" in data

    def test_each_entry_has_four_fields(self, tmp_path: Path) -> None:
        root = _make_hub_graph(tmp_path)
        data = _call_hub({}, root)
        for entry in data["hubs"]:
            for field in ("domain", "in_degree", "out_degree", "total"):
                assert field in entry, f"Missing {field!r} in {entry}"

    def test_degree_fields_are_non_negative_integers(self, tmp_path: Path) -> None:
        root = _make_hub_graph(tmp_path)
        data = _call_hub({}, root)
        for entry in data["hubs"]:
            assert isinstance(entry["in_degree"], int) and entry["in_degree"] >= 0
            assert isinstance(entry["out_degree"], int) and entry["out_degree"] >= 0
            assert isinstance(entry["total"], int) and entry["total"] >= 0

    def test_total_equals_in_plus_out(self, tmp_path: Path) -> None:
        root = _make_hub_graph(tmp_path)
        data = _call_hub({}, root)
        for entry in data["hubs"]:
            assert entry["total"] == entry["in_degree"] + entry["out_degree"], (
                f"total={entry['total']} != in={entry['in_degree']} + out={entry['out_degree']}"
                f" for {entry['domain']!r}"
            )


class TestAC3Ranking:
    """Ranking descending by each metric; top_n; empty graph; correct top hub per metric."""

    def test_total_degree_sorted_descending(self, tmp_path: Path) -> None:
        root = _make_hub_graph(tmp_path)
        data = _call_hub({"by": "total_degree"}, root)
        totals = [e["total"] for e in data["hubs"]]
        assert totals == sorted(totals, reverse=True)

    def test_out_degree_sorted_descending(self, tmp_path: Path) -> None:
        root = _make_hub_graph(tmp_path)
        data = _call_hub({"by": "out_degree"}, root)
        outs = [e["out_degree"] for e in data["hubs"]]
        assert outs == sorted(outs, reverse=True)

    def test_in_degree_sorted_descending(self, tmp_path: Path) -> None:
        root = _make_hub_graph(tmp_path)
        data = _call_hub({"by": "in_degree"}, root)
        ins = [e["in_degree"] for e in data["hubs"]]
        assert ins == sorted(ins, reverse=True)

    def test_top_n_limits_to_3(self, tmp_path: Path) -> None:
        root = _make_hub_graph(tmp_path)
        data = _call_hub({"top_n": 3}, root)
        assert len(data["hubs"]) <= 3

    def test_top_n_1_returns_one(self, tmp_path: Path) -> None:
        root = _make_hub_graph(tmp_path)
        data = _call_hub({"top_n": 1}, root)
        assert len(data["hubs"]) == 1

    def test_empty_graph_returns_empty_hubs(self, tmp_path: Path) -> None:
        root = _make_empty_graph(tmp_path)
        data = _call_hub({}, root)
        assert data["success"] is True
        assert data["hubs"] == []

    def test_top_out_degree_hub_is_alpha(self, tmp_path: Path) -> None:
        """alpha has out_degree=3 — highest in the fixture."""
        root = _make_hub_graph(tmp_path)
        data = _call_hub({"top_n": 1, "by": "out_degree"}, root)
        assert data["hubs"][0]["domain"] == "alpha"

    def test_top_in_degree_hub_is_delta(self, tmp_path: Path) -> None:
        """delta has in_degree=3 — highest in the fixture."""
        root = _make_hub_graph(tmp_path)
        data = _call_hub({"top_n": 1, "by": "in_degree"}, root)
        assert data["hubs"][0]["domain"] == "delta"

    def test_top_total_degree_hub_is_delta(self, tmp_path: Path) -> None:
        """delta has total=4 (in=3, out=1) — highest total in the fixture."""
        root = _make_hub_graph(tmp_path)
        data = _call_hub({"top_n": 1, "by": "total_degree"}, root)
        assert data["hubs"][0]["domain"] == "delta", (
            f"Expected delta as top total_degree hub, got {data['hubs'][0]['domain']!r}"
        )
