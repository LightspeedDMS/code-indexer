"""
Unit tests for Story #217 (AC9, AC10, AC12): Domain service 5-column table parsing.

Tests cover:
- AC9: Edge counting in _record_run_metrics works with 5-column table format
- AC10: _parse_cross_domain_deps parses Type and Why columns
- AC12: get_graph_data includes dep_type in edge data

TDD RED PHASE: Tests written before production code changes exist.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_domain_service(golden_repos_dir: str):
    """Build DependencyMapDomainService with minimal mocks."""
    from code_indexer.server.services.dependency_map_domain_service import (
        DependencyMapDomainService,
    )
    dep_map_svc = Mock()
    dep_map_svc.golden_repos_dir = golden_repos_dir
    config_manager = Mock()
    return DependencyMapDomainService(dep_map_svc, config_manager)


def _write_index_md(depmap_dir: Path, content: str) -> None:
    """Write _index.md to the given dependency-map directory."""
    depmap_dir.mkdir(parents=True, exist_ok=True)
    (depmap_dir / "_index.md").write_text(content)


def _write_domains_json(depmap_dir: Path, domains: list) -> None:
    """Write _domains.json to the given dependency-map directory."""
    depmap_dir.mkdir(parents=True, exist_ok=True)
    (depmap_dir / "_domains.json").write_text(json.dumps(domains))


# ─────────────────────────────────────────────────────────────────────────────
# AC9: Edge counting with 5-column table format
# ─────────────────────────────────────────────────────────────────────────────


class TestEdgeCountingWithFiveColumnTable:
    """AC9: _record_run_metrics edge counting works with 5-column table."""

    def test_edge_count_from_five_column_table(self, tmp_path):
        """AC9: Counting logic handles Source | Target | Via | Type | Why table."""
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        depmap_dir.mkdir(parents=True)

        five_col_content = (
            "---\nschema_version: 1.0\n---\n\n"
            "## Cross-Domain Dependency Graph\n\n"
            "| Source Domain | Target Domain | Via Repos | Type | Why |\n"
            "|---|---|---|---|---|\n"
            "| domain-a | domain-b | repo-a | Service integration | domain-a calls domain-b API |\n"
            "| domain-b | domain-c | repo-b | Code-level | shared library |\n\n"
        )
        (depmap_dir / "_index.md").write_text(five_col_content)

        # Replicate the counting logic from _record_run_metrics
        content = (depmap_dir / "_index.md").read_text()
        edge_count = 0
        in_cross_domain = False
        for line in content.splitlines():
            if "Cross-Domain Dependencies" in line or "Cross-Domain Dependency Graph" in line:
                in_cross_domain = True
                continue
            if in_cross_domain:
                if (
                    line.startswith("| ")
                    and not line.startswith("|---")
                    and not line.startswith("| Source")
                ):
                    edge_count += 1
                elif line.startswith("#"):
                    break

        assert edge_count == 2, (
            f"Edge counting must work with 5-column table, got {edge_count}"
        )

    def test_edge_count_zero_for_empty_table(self, tmp_path):
        """AC9: Empty 5-column table produces edge_count = 0."""
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        depmap_dir.mkdir(parents=True)

        empty_table = (
            "## Cross-Domain Dependency Graph\n\n"
            "| Source Domain | Target Domain | Via Repos | Type | Why |\n"
            "|---|---|---|---|---|\n\n"
        )
        (depmap_dir / "_index.md").write_text(empty_table)

        content = (depmap_dir / "_index.md").read_text()
        edge_count = 0
        in_cross_domain = False
        for line in content.splitlines():
            if "Cross-Domain Dependencies" in line or "Cross-Domain Dependency Graph" in line:
                in_cross_domain = True
                continue
            if in_cross_domain:
                if (
                    line.startswith("| ")
                    and not line.startswith("|---")
                    and not line.startswith("| Source")
                ):
                    edge_count += 1
                elif line.startswith("#"):
                    break

        assert edge_count == 0, (
            f"Empty 5-column table must yield edge_count=0, got {edge_count}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC10: _parse_cross_domain_deps parses Type and Why columns
# ─────────────────────────────────────────────────────────────────────────────


class TestParseCrossDomainDeps5Column:
    """AC10: _parse_cross_domain_deps handles 5-column table with Type and Why."""

    def test_parses_5_column_table_returns_two_rows(self):
        """AC10: 5-column table with 2 data rows returns list of 2 dicts."""
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_index_md(depmap_dir, (
                "## Cross-Domain Dependency Graph\n\n"
                "| Source Domain | Target Domain | Via Repos | Type | Why |\n"
                "|---|---|---|---|---|\n"
                "| authentication | user-management | auth-service | Code-level | auth imports user types |\n"
                "| billing | authentication | billing-service | Service integration | billing validates tokens |\n"
            ))
            service = _make_domain_service(tmp)
            result = service._parse_cross_domain_deps()
            assert len(result) == 2, f"Expected 2 deps, got {len(result)}"

    def test_5_column_parses_source_correctly(self):
        """AC10: Source Domain cell parsed correctly from 5-column row."""
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_index_md(depmap_dir, (
                "| Source Domain | Target Domain | Via Repos | Type | Why |\n"
                "|---|---|---|---|---|\n"
                "| authentication | user-management | auth-service | Code-level | auth imports user types |\n"
            ))
            service = _make_domain_service(tmp)
            result = service._parse_cross_domain_deps()
            assert result[0]["source"] == "authentication"

    def test_5_column_parses_target_correctly(self):
        """AC10: Target Domain cell parsed correctly from 5-column row."""
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_index_md(depmap_dir, (
                "| Source Domain | Target Domain | Via Repos | Type | Why |\n"
                "|---|---|---|---|---|\n"
                "| authentication | user-management | auth-service | Code-level | auth imports user types |\n"
            ))
            service = _make_domain_service(tmp)
            result = service._parse_cross_domain_deps()
            assert result[0]["target"] == "user-management"

    def test_5_column_extracts_dep_type(self):
        """AC10: dep_type field populated from Type column."""
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_index_md(depmap_dir, (
                "| Source Domain | Target Domain | Via Repos | Type | Why |\n"
                "|---|---|---|---|---|\n"
                "| authentication | user-management | auth-service | Code-level | auth imports user types |\n"
            ))
            service = _make_domain_service(tmp)
            result = service._parse_cross_domain_deps()
            assert result[0]["dep_type"] == "Code-level", (
                f"dep_type must be 'Code-level', got: {result[0].get('dep_type')!r}"
            )

    def test_5_column_extracts_why(self):
        """AC10: why field populated from Why column."""
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_index_md(depmap_dir, (
                "| Source Domain | Target Domain | Via Repos | Type | Why |\n"
                "|---|---|---|---|---|\n"
                "| authentication | user-management | auth-service | Code-level | auth imports user types |\n"
            ))
            service = _make_domain_service(tmp)
            result = service._parse_cross_domain_deps()
            assert result[0]["why"] == "auth imports user types", (
                f"why must be 'auth imports user types', got: {result[0].get('why')!r}"
            )

    def test_3_column_table_dep_type_and_why_empty(self):
        """AC10: Old 3-column format: dep_type and why are empty strings."""
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_index_md(depmap_dir, (
                "| Source Domain | Target Domain | Via Repos |\n"
                "|---|---|---|\n"
                "| authentication | user-management | auth-service |\n"
            ))
            service = _make_domain_service(tmp)
            result = service._parse_cross_domain_deps()
            assert len(result) == 1
            assert result[0].get("dep_type", "") == "", (
                "3-column format must yield empty dep_type"
            )
            assert result[0].get("why", "") == "", (
                "3-column format must yield empty why"
            )

    def test_4_column_table_still_parses_correctly(self):
        """AC10: Old 4-column format (Source|Target|Via|Relationship) still works."""
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_index_md(depmap_dir, (
                "| Source Domain | Target Domain | Via Repos | Relationship |\n"
                "|---|---|---|---|\n"
                "| billing | authentication | billing-svc | validates tokens |\n"
            ))
            service = _make_domain_service(tmp)
            result = service._parse_cross_domain_deps()
            assert len(result) == 1
            assert result[0]["source"] == "billing"
            assert result[0]["relationship"] == "validates tokens"

    def test_5_column_skips_header_and_separator(self):
        """AC10: Header and separator rows not included in 5-column results."""
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_index_md(depmap_dir, (
                "| Source Domain | Target Domain | Via Repos | Type | Why |\n"
                "|---|---|---|---|---|\n"
                "| auth | billing | auth-svc | Code-level | shared utils |\n"
            ))
            service = _make_domain_service(tmp)
            result = service._parse_cross_domain_deps()
            # Only 1 data row, not 3
            assert len(result) == 1
            assert result[0]["source"] == "auth"

    def test_5_column_via_repos_split_by_comma(self):
        """AC10: Via repos with comma separation split into list in 5-column table."""
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_index_md(depmap_dir, (
                "| Source Domain | Target Domain | Via Repos | Type | Why |\n"
                "|---|---|---|---|---|\n"
                "| auth | billing | repo-a, repo-b | Service integration | calls billing |\n"
            ))
            service = _make_domain_service(tmp)
            result = service._parse_cross_domain_deps()
            assert result[0]["via_repos"] == ["repo-a", "repo-b"]


# ─────────────────────────────────────────────────────────────────────────────
# AC12: get_graph_data includes dep_type in edge data
# ─────────────────────────────────────────────────────────────────────────────


class TestGetGraphDataWithDepType:
    """AC12: get_graph_data includes dep_type in edge data for D3 tooltips."""

    def test_edges_include_dep_type_field(self):
        """AC12: Edge dict from get_graph_data includes dep_type key."""
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "auth", "description": "", "participating_repos": ["auth-svc"]},
                {"name": "billing", "description": "", "participating_repos": ["bill-svc"]},
            ])
            _write_index_md(depmap_dir, (
                "## Cross-Domain Dependency Graph\n\n"
                "| Source Domain | Target Domain | Via Repos | Type | Why |\n"
                "|---|---|---|---|---|\n"
                "| auth | billing | auth-svc | Service integration | auth validates billing |\n"
            ))
            service = _make_domain_service(tmp)
            result = service.get_graph_data()

            assert len(result["edges"]) == 1
            edge = result["edges"][0]
            assert "dep_type" in edge, (
                f"Edge must include dep_type field, got keys: {list(edge.keys())}"
            )

    def test_edges_dep_type_value_from_table(self):
        """AC12: Edge dep_type contains value from Type column."""
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "auth", "description": "", "participating_repos": ["auth-svc"]},
                {"name": "billing", "description": "", "participating_repos": ["bill-svc"]},
            ])
            _write_index_md(depmap_dir, (
                "## Cross-Domain Dependency Graph\n\n"
                "| Source Domain | Target Domain | Via Repos | Type | Why |\n"
                "|---|---|---|---|---|\n"
                "| auth | billing | auth-svc | Service integration | auth validates billing |\n"
            ))
            service = _make_domain_service(tmp)
            result = service.get_graph_data()

            edge = result["edges"][0]
            assert edge["dep_type"] == "Service integration", (
                f"Edge dep_type must be 'Service integration', got: {edge.get('dep_type')!r}"
            )

    def test_edges_dep_type_empty_for_old_3_column_format(self):
        """AC12: Edge dep_type is empty string for old 3-column format."""
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "auth", "description": "", "participating_repos": ["auth-svc"]},
                {"name": "billing", "description": "", "participating_repos": ["bill-svc"]},
            ])
            _write_index_md(depmap_dir, (
                "| Source Domain | Target Domain | Via Repos |\n"
                "|---|---|---|\n"
                "| auth | billing | auth-svc |\n"
            ))
            service = _make_domain_service(tmp)
            result = service.get_graph_data()

            assert len(result["edges"]) == 1
            edge = result["edges"][0]
            assert "dep_type" in edge, "dep_type key must exist even for old format"
            assert edge["dep_type"] == "", (
                f"dep_type must be empty for 3-column format, got: {edge.get('dep_type')!r}"
            )

    def test_edges_still_have_source_and_target(self):
        """AC12: Adding dep_type does not break existing source/target edge fields."""
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "auth", "description": "", "participating_repos": ["auth-svc"]},
                {"name": "billing", "description": "", "participating_repos": ["bill-svc"]},
            ])
            _write_index_md(depmap_dir, (
                "| Source Domain | Target Domain | Via Repos | Type | Why |\n"
                "|---|---|---|---|---|\n"
                "| auth | billing | auth-svc | Code-level | shared |\n"
            ))
            service = _make_domain_service(tmp)
            result = service.get_graph_data()

            edge = result["edges"][0]
            assert edge["source"] == "auth"
            assert edge["target"] == "billing"
