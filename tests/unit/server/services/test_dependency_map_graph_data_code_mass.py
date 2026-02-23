"""Tests for Story #273: Log-Compressed Code Mass Bubble Sizing - Backend total_file_count.

TDD: Tests written BEFORE implementation (RED phase).

Acceptance Criteria covered:
- AC1: get_graph_data() returns total_file_count per node
- AC2: File counts are correctly aggregated from journal repo_sizes
- AC3: Missing journal or repo_sizes degrades gracefully to zero
- AC4: Access-filtered repos exclude their file counts from domain totals
- AC6: Domains with zero files have unchanged bubble sizing (total_file_count=0)
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


def _write_journal_json(depmap_dir: Path, journal_data: dict) -> None:
    """Write _journal.json to the given dependency-map directory."""
    depmap_dir.mkdir(parents=True, exist_ok=True)
    (depmap_dir / "_journal.json").write_text(json.dumps(journal_data))


# ─────────────────────────────────────────────────────────────────────────────
# AC1: get_graph_data() returns total_file_count field in each node
# ─────────────────────────────────────────────────────────────────────────────


class TestGraphDataTotalFileCountField:
    """AC1: get_graph_data() returns total_file_count per node."""

    def test_nodes_have_total_file_count_field(self, tmp_path):
        """AC1: Every node must have a total_file_count field."""
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
        assert "total_file_count" in nodes[0], (
            f"Node missing 'total_file_count' field. Got keys: {list(nodes[0].keys())}"
        )

    def test_total_file_count_is_integer(self, tmp_path):
        """AC1: total_file_count must be an integer, not None or string."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "auth", "description": "", "participating_repos": ["repo1"]},
            {"name": "billing", "description": "", "participating_repos": ["repo2"]},
        ])
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        for node in result["nodes"]:
            assert isinstance(node["total_file_count"], int), (
                f"total_file_count must be int, got {type(node['total_file_count'])} "
                f"for node {node['id']}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# AC2: File counts are correctly aggregated from journal repo_sizes
# ─────────────────────────────────────────────────────────────────────────────


class TestGraphDataFileCountAggregation:
    """AC2: File counts are correctly aggregated from journal repo_sizes."""

    def test_file_count_aggregated_from_journal(self, tmp_path):
        """AC2: total_file_count equals sum of file_count for repos in domain."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {
                "name": "Infrastructure",
                "description": "",
                "participating_repos": ["repo-a", "repo-b"],
            },
        ])
        _write_journal_json(depmap_dir, {
            "repo_sizes": {
                "repo-a": {"file_count": 500},
                "repo-b": {"file_count": 1500},
            }
        })
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        nodes_by_id = {n["id"]: n for n in result["nodes"]}
        assert nodes_by_id["Infrastructure"]["total_file_count"] == 2000, (
            f"Expected total_file_count=2000, got "
            f"{nodes_by_id['Infrastructure']['total_file_count']}"
        )

    def test_single_repo_domain_uses_its_file_count(self, tmp_path):
        """AC2: Single-repo domain gets the repo's file_count directly."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "auth", "description": "", "participating_repos": ["auth-svc"]},
        ])
        _write_journal_json(depmap_dir, {
            "repo_sizes": {
                "auth-svc": {"file_count": 300},
            }
        })
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        nodes = result["nodes"]
        assert nodes[0]["total_file_count"] == 300, (
            f"Expected total_file_count=300, got {nodes[0]['total_file_count']}"
        )

    def test_repo_missing_from_journal_contributes_zero(self, tmp_path):
        """AC2: Repo in domain but not in journal contributes 0 to the total."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {
                "name": "mixed",
                "description": "",
                "participating_repos": ["known-repo", "unknown-repo"],
            },
        ])
        _write_journal_json(depmap_dir, {
            "repo_sizes": {
                "known-repo": {"file_count": 1000},
                # "unknown-repo" intentionally absent
            }
        })
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        nodes = result["nodes"]
        assert nodes[0]["total_file_count"] == 1000, (
            f"Expected total_file_count=1000 (unknown repo contributes 0), "
            f"got {nodes[0]['total_file_count']}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC3: Missing journal degrades gracefully to zero
# ─────────────────────────────────────────────────────────────────────────────


class TestGraphDataMissingJournal:
    """AC3: Missing journal or repo_sizes degrades gracefully to zero."""

    def test_no_journal_file_gives_total_file_count_zero(self, tmp_path):
        """AC3: When no _journal.json exists, total_file_count is 0 for all nodes."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "auth", "description": "", "participating_repos": ["repo1"]},
            {"name": "billing", "description": "", "participating_repos": ["repo2"]},
        ])
        # No _journal.json written
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        nodes = result["nodes"]
        assert len(nodes) == 2, "Expected 2 nodes"
        for node in nodes:
            assert node["total_file_count"] == 0, (
                f"Node {node['id']} should have total_file_count=0 when no journal, "
                f"got {node['total_file_count']}"
            )

    def test_journal_without_repo_sizes_gives_total_file_count_zero(self, tmp_path):
        """AC3: Journal exists but lacks 'repo_sizes' key - total_file_count is 0."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "auth", "description": "", "participating_repos": ["repo1"]},
        ])
        # Journal exists but without repo_sizes key
        _write_journal_json(depmap_dir, {"analysis_date": "2026-02-23"})
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        nodes = result["nodes"]
        assert nodes[0]["total_file_count"] == 0, (
            f"Expected total_file_count=0 when repo_sizes absent from journal, "
            f"got {nodes[0]['total_file_count']}"
        )

    def test_corrupt_journal_gives_total_file_count_zero(self, tmp_path):
        """AC3: Corrupt (invalid JSON) journal gives total_file_count=0, no exception."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "auth", "description": "", "participating_repos": ["repo1"]},
        ])
        # Write corrupt JSON
        depmap_dir.mkdir(parents=True, exist_ok=True)
        (depmap_dir / "_journal.json").write_text("{ this is not valid JSON }")
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        # Must not raise
        result = service.get_graph_data()
        nodes = result["nodes"]
        assert nodes[0]["total_file_count"] == 0, (
            f"Expected total_file_count=0 when journal is corrupt, "
            f"got {nodes[0]['total_file_count']}"
        )

    def test_missing_journal_does_not_affect_other_node_fields(self, tmp_path):
        """AC3: Other node fields (repo_count, dep counts) remain correct without journal."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "auth", "description": "Auth domain", "participating_repos": ["a", "b"]},
        ])
        # No _journal.json
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        node = result["nodes"][0]
        assert node["id"] == "auth"
        assert node["name"] == "auth"
        assert node["description"] == "Auth domain"
        assert node["repo_count"] == 2
        assert node["incoming_dep_count"] == 0
        assert node["outgoing_dep_count"] == 0
        assert node["total_file_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# AC4: Access-filtered repos exclude their file counts from domain totals
# ─────────────────────────────────────────────────────────────────────────────


class TestGraphDataAccessFilteredFileCounts:
    """AC4: Access-filtered repos exclude their file counts from domain totals."""

    def test_non_admin_gets_only_accessible_repo_counts(self, tmp_path):
        """AC4: Non-admin user gets only file counts from their accessible repos."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {
                "name": "platform",
                "description": "",
                "participating_repos": ["repo-a", "repo-b"],
            },
        ])
        _write_journal_json(depmap_dir, {
            "repo_sizes": {
                "repo-a": {"file_count": 300},
                "repo-b": {"file_count": 5000},
            }
        })
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        # Non-admin can only access repo-a
        result = service.get_graph_data(accessible_repos={"repo-a"})
        nodes = result["nodes"]
        assert len(nodes) == 1, "Expected 1 node (repo-a accessible, domain is visible)"
        assert nodes[0]["total_file_count"] == 300, (
            f"Expected total_file_count=300 (only repo-a accessible), "
            f"got {nodes[0]['total_file_count']}"
        )

    def test_admin_gets_all_repo_counts(self, tmp_path):
        """AC4: Admin (accessible_repos=None) gets sum of ALL repo file counts."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {
                "name": "platform",
                "description": "",
                "participating_repos": ["repo-a", "repo-b"],
            },
        ])
        _write_journal_json(depmap_dir, {
            "repo_sizes": {
                "repo-a": {"file_count": 300},
                "repo-b": {"file_count": 5000},
            }
        })
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        # Admin (None = all repos)
        result = service.get_graph_data(accessible_repos=None)
        nodes = result["nodes"]
        assert nodes[0]["total_file_count"] == 5300, (
            f"Expected total_file_count=5300 (admin sees all), "
            f"got {nodes[0]['total_file_count']}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC6: Domains with zero files have total_file_count=0 (no NaN, no negatives)
# ─────────────────────────────────────────────────────────────────────────────


class TestGraphDataZeroFileCount:
    """AC6: Domains with zero files produce total_file_count=0, no NaN or negatives."""

    def test_domain_with_zero_file_count_in_journal(self, tmp_path):
        """AC6: Repo with explicit file_count=0 in journal gives total_file_count=0."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "empty-domain", "description": "", "participating_repos": ["empty-repo"]},
        ])
        _write_journal_json(depmap_dir, {
            "repo_sizes": {
                "empty-repo": {"file_count": 0},
            }
        })
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        nodes = result["nodes"]
        assert nodes[0]["total_file_count"] == 0, (
            f"Expected total_file_count=0, got {nodes[0]['total_file_count']}"
        )
        # Verify it's a non-negative integer (not NaN, not negative)
        count = nodes[0]["total_file_count"]
        assert isinstance(count, int) and count >= 0, (
            f"total_file_count must be non-negative integer, got {count!r}"
        )

    def test_existing_fields_still_present_with_journal(self, tmp_path):
        """Regression: Adding total_file_count must not remove existing node fields."""
        Service = _import_service()
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        _write_domains_json(depmap_dir, [
            {"name": "auth", "description": "Auth domain", "participating_repos": ["a", "b"]},
        ])
        _write_journal_json(depmap_dir, {
            "repo_sizes": {
                "a": {"file_count": 100},
                "b": {"file_count": 200},
            }
        })
        dep_map_svc = _make_dep_map_service(str(tmp_path))
        service = Service(dep_map_svc, _make_config_manager())
        result = service.get_graph_data()
        node = result["nodes"][0]
        # All existing fields must still be present
        assert node["id"] == "auth"
        assert node["name"] == "auth"
        assert node["description"] == "Auth domain"
        assert node["repo_count"] == 2
        assert "incoming_dep_count" in node
        assert "outgoing_dep_count" in node
        # New field
        assert node["total_file_count"] == 300
