"""Tests for Story #260: Enhanced Dependency Graph Bubble Sizing - Backend dep counts.

TDD: Tests written BEFORE implementation (RED phase).

Acceptance Criteria covered:
- AC1: get_graph_data() returns incoming_dep_count and outgoing_dep_count per node
- AC2: Counts are accurate: they match the actual visible edges
- AC3: Edges to/from hidden domains (filtered by accessible_repos) do NOT count
- AC4: Nodes with zero edges have count 0
"""
import json
from pathlib import Path
from unittest.mock import Mock


def _make_dep_map_service(golden_repos_dir: str):
    """Build a mock DependencyMapService with cidx_meta_read_path property."""
    svc = Mock()
    svc.golden_repos_dir = golden_repos_dir
    svc.cidx_meta_read_path = Path(golden_repos_dir) / "cidx-meta"
    return svc


def _make_config_manager():
    """Build a mock config_manager (not used directly but required for constructor)."""
    return Mock()


def _import_service():
    from code_indexer.server.services.dependency_map_domain_service import (
        DependencyMapDomainService,
    )
    return DependencyMapDomainService


def _write_domains_json(depmap_dir: Path, domains: list) -> None:
    """Write _domains.json to the given dependency-map directory."""
    depmap_dir.mkdir(parents=True, exist_ok=True)
    (depmap_dir / "_domains.json").write_text(json.dumps(domains))


def _write_index_md(depmap_dir: Path, content: str) -> None:
    """Write _index.md to the given dependency-map directory."""
    depmap_dir.mkdir(parents=True, exist_ok=True)
    (depmap_dir / "_index.md").write_text(content)


# ─────────────────────────────────────────────────────────────────────────────
# AC1: get_graph_data() returns incoming_dep_count and outgoing_dep_count fields
# ─────────────────────────────────────────────────────────────────────────────


class TestGraphDataDepCountFields:
    """AC1: get_graph_data() returns incoming_dep_count and outgoing_dep_count per node."""

    def test_nodes_have_incoming_dep_count_field(self, tmp_path):
        """AC1: Every node must have an incoming_dep_count field."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "auth", "description": "Auth domain", "participating_repos": ["repo1"]},
        ])
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        nodes = result.get("nodes", [])
        assert len(nodes) > 0, "Expected at least one node"
        assert "incoming_dep_count" in nodes[0], (
            f"Node missing 'incoming_dep_count' field. Got keys: {list(nodes[0].keys())}"
        )

    def test_nodes_have_outgoing_dep_count_field(self, tmp_path):
        """AC1: Every node must have an outgoing_dep_count field."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "auth", "description": "Auth domain", "participating_repos": ["repo1"]},
        ])
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        nodes = result.get("nodes", [])
        assert len(nodes) > 0, "Expected at least one node"
        assert "outgoing_dep_count" in nodes[0], (
            f"Node missing 'outgoing_dep_count' field. Got keys: {list(nodes[0].keys())}"
        )

    def test_zero_dep_node_has_count_zero(self, tmp_path):
        """AC4: Isolated node with no edges has both counts equal to zero."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "isolated", "description": "No deps", "participating_repos": ["repo1"]},
        ])
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        nodes = result.get("nodes", [])
        assert len(nodes) == 1
        node = nodes[0]
        assert node["incoming_dep_count"] == 0, (
            f"Isolated node should have incoming_dep_count=0, got {node['incoming_dep_count']}"
        )
        assert node["outgoing_dep_count"] == 0, (
            f"Isolated node should have outgoing_dep_count=0, got {node['outgoing_dep_count']}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC2: Accurate counts matching visible edges
# ─────────────────────────────────────────────────────────────────────────────


class TestGraphDataDepCountAccuracy:
    """AC2: Counts accurately reflect the actual visible edges in the graph."""

    def test_outgoing_count_matches_edges_from_node(self, tmp_path):
        """AC2: outgoing_dep_count equals the number of edges where this node is source."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "auth", "description": "", "participating_repos": ["auth-svc"]},
            {"name": "billing", "description": "", "participating_repos": ["bill-svc"]},
            {"name": "infra", "description": "", "participating_repos": ["infra-svc"]},
        ])
        _write_index_md(depmap_dir, """
## Cross-Domain Dependencies

| Source Domain | Target Domain | Via Repos |
|---|---|---|
| auth | billing | auth-svc |
| auth | infra | auth-svc |
""")
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        nodes_by_id = {n["id"]: n for n in result["nodes"]}
        # auth has 2 outgoing edges
        assert nodes_by_id["auth"]["outgoing_dep_count"] == 2, (
            f"Expected outgoing=2 for auth, got {nodes_by_id['auth']['outgoing_dep_count']}"
        )
        assert nodes_by_id["auth"]["incoming_dep_count"] == 0
        # billing and infra each have 1 incoming edge
        assert nodes_by_id["billing"]["incoming_dep_count"] == 1
        assert nodes_by_id["billing"]["outgoing_dep_count"] == 0
        assert nodes_by_id["infra"]["incoming_dep_count"] == 1
        assert nodes_by_id["infra"]["outgoing_dep_count"] == 0

    def test_incoming_count_matches_edges_to_node(self, tmp_path):
        """AC2: incoming_dep_count equals the number of edges where this node is target."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "shared", "description": "", "participating_repos": ["shared-svc"]},
            {"name": "auth", "description": "", "participating_repos": ["auth-svc"]},
            {"name": "billing", "description": "", "participating_repos": ["bill-svc"]},
        ])
        _write_index_md(depmap_dir, """
## Cross-Domain Dependencies

| Source Domain | Target Domain | Via Repos |
|---|---|---|
| auth | shared | auth-svc |
| billing | shared | bill-svc |
""")
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        nodes_by_id = {n["id"]: n for n in result["nodes"]}
        # shared has 2 incoming edges (from auth and billing)
        assert nodes_by_id["shared"]["incoming_dep_count"] == 2, (
            f"Expected incoming=2 for shared, got {nodes_by_id['shared']['incoming_dep_count']}"
        )
        assert nodes_by_id["shared"]["outgoing_dep_count"] == 0

    def test_bidirectional_edges_counted_correctly(self, tmp_path):
        """AC2: Bidirectional dependency (A->B and B->A) counts both nodes correctly."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "auth", "description": "", "participating_repos": ["auth-svc"]},
            {"name": "billing", "description": "", "participating_repos": ["bill-svc"]},
        ])
        _write_index_md(depmap_dir, """
## Cross-Domain Dependencies

| Source Domain | Target Domain | Via Repos |
|---|---|---|
| auth | billing | auth-svc |
| billing | auth | bill-svc |
""")
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        nodes_by_id = {n["id"]: n for n in result["nodes"]}
        # Each node has 1 outgoing and 1 incoming
        assert nodes_by_id["auth"]["outgoing_dep_count"] == 1
        assert nodes_by_id["auth"]["incoming_dep_count"] == 1
        assert nodes_by_id["billing"]["outgoing_dep_count"] == 1
        assert nodes_by_id["billing"]["incoming_dep_count"] == 1

    def test_sum_invariant_total_incoming_equals_total_outgoing(self, tmp_path):
        """AC2: Sum of all incoming counts equals sum of all outgoing counts (edge conservation)."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "auth", "description": "", "participating_repos": ["auth-svc"]},
            {"name": "billing", "description": "", "participating_repos": ["bill-svc"]},
            {"name": "infra", "description": "", "participating_repos": ["infra-svc"]},
            {"name": "shared", "description": "", "participating_repos": ["shared-svc"]},
        ])
        _write_index_md(depmap_dir, """
## Cross-Domain Dependencies

| Source Domain | Target Domain | Via Repos |
|---|---|---|
| auth | shared | auth-svc |
| billing | shared | bill-svc |
| auth | infra | auth-svc |
""")
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        nodes = result["nodes"]
        total_incoming = sum(n["incoming_dep_count"] for n in nodes)
        total_outgoing = sum(n["outgoing_dep_count"] for n in nodes)
        assert total_incoming == total_outgoing, (
            f"Sum invariant violated: total_incoming={total_incoming}, "
            f"total_outgoing={total_outgoing}. Each edge contributes 1 to each sum."
        )
        # 3 edges total means each sum should be 3
        assert total_incoming == 3


# ─────────────────────────────────────────────────────────────────────────────
# AC3: Hidden domain edges are NOT counted
# ─────────────────────────────────────────────────────────────────────────────


class TestGraphDataDepCountFiltering:
    """AC3: Edges to/from hidden domains (filtered by accessible_repos) do NOT count."""

    def test_hidden_domain_edge_not_counted_in_visible_node(self, tmp_path):
        """AC3: Edge from hidden domain to visible domain does not add to visible node's count."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "auth", "description": "", "participating_repos": ["auth-svc"]},
            {"name": "billing", "description": "", "participating_repos": ["bill-svc"]},
            {"name": "infra", "description": "", "participating_repos": ["infra-svc"]},
        ])
        _write_index_md(depmap_dir, """
## Cross-Domain Dependencies

| Source Domain | Target Domain | Via Repos |
|---|---|---|
| auth | billing | auth-svc |
| infra | billing | infra-svc |
""")
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        # Non-admin can only see auth-svc and bill-svc (infra is hidden)
        result = service.get_graph_data(accessible_repos={"auth-svc", "bill-svc"})
        # Only auth and billing are visible
        nodes_by_id = {n["id"]: n for n in result["nodes"]}
        assert "infra" not in nodes_by_id, "infra should be hidden"
        # billing should only count the edge from auth (not the hidden edge from infra)
        assert nodes_by_id["billing"]["incoming_dep_count"] == 1, (
            f"billing should have incoming=1 (only from auth, not from hidden infra). "
            f"Got {nodes_by_id['billing']['incoming_dep_count']}"
        )

    def test_multi_repo_domain_with_zero_visible_edges_has_count_zero(self, tmp_path):
        """AC4: Multi-repo domain with no visible edges has both counts equal to zero."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "shared", "description": "", "participating_repos": ["svc-a", "svc-b", "svc-c"]},
        ])
        # No _index.md — no deps at all
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        nodes = result["nodes"]
        assert len(nodes) == 1
        assert nodes[0]["incoming_dep_count"] == 0
        assert nodes[0]["outgoing_dep_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestGraphDataDepCountEdgeCases:
    """Edge cases for dep count computation."""

    def test_empty_graph_no_nodes(self, tmp_path):
        """Empty domains.json returns no nodes and no crash."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [])
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        assert result["nodes"] == []
        assert result["edges"] == []

    def test_node_dep_counts_are_integers(self, tmp_path):
        """AC1: Dep count fields are integers, not None or strings."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "auth", "description": "", "participating_repos": ["auth-svc"]},
            {"name": "billing", "description": "", "participating_repos": ["bill-svc"]},
        ])
        _write_index_md(depmap_dir, """
## Cross-Domain Dependencies

| Source Domain | Target Domain | Via Repos |
|---|---|---|
| auth | billing | auth-svc |
""")
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        for node in result["nodes"]:
            assert isinstance(node["incoming_dep_count"], int), (
                f"incoming_dep_count must be int, got {type(node['incoming_dep_count'])}"
            )
            assert isinstance(node["outgoing_dep_count"], int), (
                f"outgoing_dep_count must be int, got {type(node['outgoing_dep_count'])}"
            )

    def test_existing_fields_still_present(self, tmp_path):
        """Regression: Adding new fields must not remove existing id/name/description/repo_count."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "auth", "description": "Auth domain", "participating_repos": ["a", "b"]},
        ])
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        node = result["nodes"][0]
        # All existing fields must still be present
        assert node["id"] == "auth"
        assert node["name"] == "auth"
        assert node["description"] == "Auth domain"
        assert node["repo_count"] == 2
        # New fields must also be present
        assert "incoming_dep_count" in node
        assert "outgoing_dep_count" in node
