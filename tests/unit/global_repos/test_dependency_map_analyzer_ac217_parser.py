"""
Unit tests for Story #217 (AC6, AC8, AC13): Deterministic table parser.

Tests cover:
- AC6: _build_cross_domain_graph replaced with deterministic markdown table parser
- AC8: _index.md output table includes Type and Why columns
- AC13: Table parsing edge cases (empty tables, malformed rows, sentinel text)

TDD RED PHASE: Tests written before production code changes exist.
"""

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Test fixtures - structured domain file content
# ─────────────────────────────────────────────────────────────────────────────

DOMAIN_A_STRUCTURED = """\
---
domain: domain-a
---

# Domain Analysis: domain-a

## Overview
Domain A overview.

## Cross-Domain Connections

### Outgoing Dependencies

| This Repo | Depends On | Target Domain | Type | Why | Evidence |
|---|---|---|---|---|---|
| repo-a-one | repo-b-alpha | domain-b | Service integration | repo-a-one calls REST API | api_client.py line 42 |

### Incoming Dependencies

| External Repo | Depends On | Source Domain | Type | Why | Evidence |
|---|---|---|---|---|---|
| repo-b-beta | repo-a-one | domain-b | Code-level | repo-b-beta imports repo-a-one utils | imports in utils.py |
"""

DOMAIN_B_STRUCTURED = """\
---
domain: domain-b
---

# Domain Analysis: domain-b

## Overview
Domain B overview.

## Cross-Domain Connections

### Outgoing Dependencies

No verified cross-domain dependencies.

### Incoming Dependencies

| External Repo | Depends On | Source Domain | Type | Why | Evidence |
|---|---|---|---|---|---|
| repo-a-one | repo-b-alpha | domain-a | Service integration | domain-a calls domain-b REST API | api_client.py |
"""

DOMAIN_SENTINEL_ONLY = """\
---
domain: domain-c
---

# Domain Analysis: domain-c

## Cross-Domain Connections

No verified cross-domain dependencies.
"""


def _import_analyzer():
    from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer
    return DependencyMapAnalyzer


# ─────────────────────────────────────────────────────────────────────────────
# AC6: _build_cross_domain_graph uses deterministic table parser
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildCrossDomainGraphStructuredParser:
    """AC6: _build_cross_domain_graph parses structured tables, not prose."""

    def test_parses_outgoing_table_builds_edge(self, tmp_path):
        """AC6: Outgoing Dependencies table produces graph edges."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "domain-a.md").write_text(DOMAIN_A_STRUCTURED)
        (staging_dir / "domain-b.md").write_text(DOMAIN_B_STRUCTURED)

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a-one"]},
            {"name": "domain-b", "participating_repos": ["repo-b-alpha", "repo-b-beta"]},
        ]

        DependencyMapAnalyzer = _import_analyzer()
        result = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        assert "## Cross-Domain Dependency Graph" in result
        assert "domain-a" in result
        assert "domain-b" in result

    def test_sentinel_only_domain_produces_no_edges(self, tmp_path):
        """AC6: Sentinel text 'No verified cross-domain dependencies.' means no edges."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "domain-c.md").write_text(DOMAIN_SENTINEL_ONLY)
        (staging_dir / "domain-a.md").write_text(
            "# Domain Analysis: domain-a\n\n"
            "## Cross-Domain Connections\n\nNo verified cross-domain dependencies.\n"
        )

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a"]},
            {"name": "domain-c", "participating_repos": ["repo-c"]},
        ]

        DependencyMapAnalyzer = _import_analyzer()
        result = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        assert result == "", "Sentinel-only domains must produce no edges"

    def test_empty_table_produces_no_edges(self, tmp_path):
        """AC6: Table with header and separator but no data rows produces no edges."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        domain_content = (
            "# Domain Analysis: domain-a\n\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
        )
        (staging_dir / "domain-a.md").write_text(domain_content)

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a"]},
            {"name": "domain-b", "participating_repos": ["repo-b"]},
        ]

        DependencyMapAnalyzer = _import_analyzer()
        result = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        assert result == "", "Empty structured tables must produce no edges"

    def test_negation_prose_does_not_suppress_table_edge(self, tmp_path):
        """AC6: Old negation phrases in prose don't suppress explicitly declared table edges."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        domain_content = (
            "# Domain Analysis: domain-a\n\n"
            "## Cross-Domain Connections\n\n"
            "Note: there are no dependencies in most contexts.\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "| repo-a | repo-b | domain-b | Service integration | A calls B API | client.py |\n\n"
            "### Incoming Dependencies\n\n"
            "No verified cross-domain dependencies.\n"
        )
        (staging_dir / "domain-a.md").write_text(domain_content)
        (staging_dir / "domain-b.md").write_text(
            "# Domain Analysis: domain-b\n\n"
            "## Cross-Domain Connections\n\nNo verified cross-domain dependencies.\n"
        )

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a"]},
            {"name": "domain-b", "participating_repos": ["repo-b"]},
        ]

        DependencyMapAnalyzer = _import_analyzer()
        result = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        # Table row is explicitly declared — must produce edge regardless of prose
        assert "## Cross-Domain Dependency Graph" in result, (
            "Negation phrases in prose must NOT suppress explicitly declared table edges"
        )

    def test_missing_domain_file_skipped_gracefully(self, tmp_path):
        """AC6: Domain without .md file is skipped without error."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        # Only one of two domains has a file

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a"]},
            {"name": "domain-b", "participating_repos": ["repo-b"]},
        ]

        DependencyMapAnalyzer = _import_analyzer()
        result = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)
        assert result == "", "Missing domain files must be handled gracefully"

    def test_no_cross_domain_section_produces_no_edges(self, tmp_path):
        """AC6: Domain file without Cross-Domain Connections section produces no edges."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "domain-a.md").write_text(
            "# Domain Analysis: domain-a\n\n"
            "## Overview\nNo cross-domain connections section.\n\n"
            "## Repository Roles\nVarious repos.\n"
        )

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a"]},
        ]

        DependencyMapAnalyzer = _import_analyzer()
        result = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)
        assert result == "", "Missing Cross-Domain Connections section must produce no edges"

    def test_malformed_row_skipped_valid_row_produces_edge(self, tmp_path):
        """AC6 + AC13: Malformed row skipped, valid row still produces edge."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        domain_content = (
            "# Domain Analysis: domain-a\n\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "| bad-row | only-three | cells |\n"
            "| repo-a | repo-b | domain-b | Service integration | A calls B | client.py |\n"
        )
        (staging_dir / "domain-a.md").write_text(domain_content)
        (staging_dir / "domain-b.md").write_text(
            "# Domain Analysis: domain-b\n\n"
            "## Cross-Domain Connections\n\nNo verified cross-domain dependencies.\n"
        )

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a"]},
            {"name": "domain-b", "participating_repos": ["repo-b"]},
        ]

        DependencyMapAnalyzer = _import_analyzer()
        result = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        # Valid row should produce an edge
        assert "## Cross-Domain Dependency Graph" in result, (
            "Valid rows must produce edges even when mixed with malformed rows"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC8: _index.md output table includes Type and Why columns
# ─────────────────────────────────────────────────────────────────────────────


class TestOutputTableHasTypeAndWhy:
    """AC8: _build_cross_domain_graph output includes Type and Why columns."""

    def test_output_table_header_has_type_column(self, tmp_path):
        """AC8: Output table header includes 'Type' column."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "domain-a.md").write_text(DOMAIN_A_STRUCTURED)
        (staging_dir / "domain-b.md").write_text(DOMAIN_B_STRUCTURED)

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a-one"]},
            {"name": "domain-b", "participating_repos": ["repo-b-alpha", "repo-b-beta"]},
        ]

        DependencyMapAnalyzer = _import_analyzer()
        result = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        assert "Type" in result, "_build_cross_domain_graph output must include 'Type' column"

    def test_output_table_header_has_why_column(self, tmp_path):
        """AC8: Output table header includes 'Why' column."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "domain-a.md").write_text(DOMAIN_A_STRUCTURED)
        (staging_dir / "domain-b.md").write_text(DOMAIN_B_STRUCTURED)

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a-one"]},
            {"name": "domain-b", "participating_repos": ["repo-b-alpha", "repo-b-beta"]},
        ]

        DependencyMapAnalyzer = _import_analyzer()
        result = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        assert "Why" in result, "_build_cross_domain_graph output must include 'Why' column"

    def test_type_value_from_table_appears_in_output(self, tmp_path):
        """AC8: Type value declared in structured table appears in _index.md output."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "domain-a.md").write_text(DOMAIN_A_STRUCTURED)
        (staging_dir / "domain-b.md").write_text(DOMAIN_B_STRUCTURED)

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a-one"]},
            {"name": "domain-b", "participating_repos": ["repo-b-alpha", "repo-b-beta"]},
        ]

        DependencyMapAnalyzer = _import_analyzer()
        result = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        # "Service integration" from the domain-a outgoing table must appear
        assert "Service integration" in result, (
            "Type value from structured table must appear in _index.md output"
        )

    def test_why_value_from_table_appears_in_output(self, tmp_path):
        """AC8: Why value declared in structured table appears in _index.md output."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "domain-a.md").write_text(DOMAIN_A_STRUCTURED)
        (staging_dir / "domain-b.md").write_text(DOMAIN_B_STRUCTURED)

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a-one"]},
            {"name": "domain-b", "participating_repos": ["repo-b-alpha", "repo-b-beta"]},
        ]

        DependencyMapAnalyzer = _import_analyzer()
        result = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        # "repo-a-one calls REST API" from Why column must appear in output
        assert "repo-a-one calls REST API" in result, (
            "Why value from structured table must appear in _index.md output"
        )

    def test_output_table_format_has_five_columns(self, tmp_path):
        """AC8: Output table row has 5 columns: Source Domain | Target Domain | Via Repos | Type | Why."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "domain-a.md").write_text(DOMAIN_A_STRUCTURED)
        (staging_dir / "domain-b.md").write_text(DOMAIN_B_STRUCTURED)

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a-one"]},
            {"name": "domain-b", "participating_repos": ["repo-b-alpha", "repo-b-beta"]},
        ]

        DependencyMapAnalyzer = _import_analyzer()
        result = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        # Find a data row and count columns
        for line in result.splitlines():
            line = line.strip()
            if (
                line.startswith("|")
                and line.endswith("|")
                and "---" not in line
                and "Source Domain" not in line
            ):
                cells = [c.strip() for c in line.split("|") if c.strip()]
                assert len(cells) == 5, (
                    f"Output table data row must have 5 columns, got {len(cells)}: {cells}"
                )
                break


# ─────────────────────────────────────────────────────────────────────────────
# AC13: Additional edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestTableParsingEdgeCases:
    """AC13: Edge cases in table parsing."""

    def test_sentinel_text_in_outgoing_section_no_edges(self, tmp_path):
        """AC13: Sentinel text in Outgoing section means no outgoing edges."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "domain-a.md").write_text(
            "# Domain Analysis: domain-a\n\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "No verified cross-domain dependencies.\n\n"
            "### Incoming Dependencies\n\n"
            "No verified cross-domain dependencies.\n"
        )

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a"]},
            {"name": "domain-b", "participating_repos": ["repo-b"]},
        ]

        DependencyMapAnalyzer = _import_analyzer()
        result = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)
        assert result == "", "Sentinel text in structured sections must produce no edges"

    def test_domain_with_only_incoming_table_produces_no_self_edge(self, tmp_path):
        """AC13: Incoming table in domain-b does not create duplicate edge if outgoing already counts."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        # domain-a has outgoing dep on domain-b
        (staging_dir / "domain-a.md").write_text(DOMAIN_A_STRUCTURED)
        # domain-b has no outgoing deps, only incoming (from domain-a)
        (staging_dir / "domain-b.md").write_text(DOMAIN_B_STRUCTURED)

        domain_list = [
            {"name": "domain-a", "participating_repos": ["repo-a-one"]},
            {"name": "domain-b", "participating_repos": ["repo-b-alpha", "repo-b-beta"]},
        ]

        DependencyMapAnalyzer = _import_analyzer()
        result = DependencyMapAnalyzer._build_cross_domain_graph(staging_dir, domain_list)

        # Count data rows in output table to ensure no duplicates
        data_rows = []
        for line in result.splitlines():
            line = line.strip()
            if (
                line.startswith("|")
                and line.endswith("|")
                and "---" not in line
                and "Source Domain" not in line
            ):
                data_rows.append(line)

        # There should be exactly 1 edge: domain-a -> domain-b
        # Not 2 (outgoing from domain-a + incoming to domain-b)
        assert len(data_rows) == 1, (
            f"Expected 1 edge (domain-a -> domain-b), got {len(data_rows)}: {data_rows}"
        )
