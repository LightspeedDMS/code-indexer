"""
Unit tests for DependencyMapDomainService (Story #214).

Tests the service that provides domain explorer data including:
- AC1: Domain list loading from _domains.json
- AC2: Cross-domain dependency parsing from _index.md
- AC3: Domain detail assembly (description, repos, deps, markdown)
- AC5: Access filtering (admin vs non-admin)
- AC4: Markdown rendering with YAML frontmatter stripping

TDD RED PHASE: Tests written before production code exists.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_dep_map_service(golden_repos_dir: str):
    """Build a mock DependencyMapService with a golden_repos_dir property."""
    svc = Mock()
    svc.golden_repos_dir = golden_repos_dir
    svc.cidx_meta_read_path = Path(golden_repos_dir) / "cidx-meta"
    return svc


def _make_config_manager():
    """Build a mock config_manager (not used directly but needed for constructor)."""
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


def _write_domain_md(depmap_dir: Path, domain_name: str, content: str) -> None:
    """Write a domain .md file to the given dependency-map directory."""
    depmap_dir.mkdir(parents=True, exist_ok=True)
    (depmap_dir / f"{domain_name}.md").write_text(content)


# ─────────────────────────────────────────────────────────────────────────────
# AC1: Domain list loading from _domains.json
# ─────────────────────────────────────────────────────────────────────────────


class TestGetDomainList:
    """AC1: get_domain_list() returns structured domain data from _domains.json."""

    def test_returns_empty_when_domains_json_missing(self):
        """AC1: Missing _domains.json returns empty domain list."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_list()
            assert result["domains"] == []
            assert result["total_count"] == 0

    def test_returns_domain_list_from_domains_json(self):
        """AC1: Populated _domains.json returns correct domain list."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {
                    "name": "authentication",
                    "description": "Handles user auth",
                    "participating_repos": ["auth-service", "api-gateway"],
                },
                {
                    "name": "billing",
                    "description": "Payment processing",
                    "participating_repos": ["billing-service"],
                },
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_list()
            assert result["total_count"] == 2
            names = [d["name"] for d in result["domains"]]
            assert "authentication" in names
            assert "billing" in names

    def test_domain_has_required_fields(self):
        """AC1: Each domain dict has name, description, repo_count, participating_repos, last_analyzed."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {
                    "name": "authentication",
                    "description": "Handles user auth",
                    "participating_repos": ["auth-service", "api-gateway"],
                },
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_list()
            domain = result["domains"][0]
            assert "name" in domain
            assert "description" in domain
            assert "repo_count" in domain
            assert "participating_repos" in domain
            assert "last_analyzed" in domain

    def test_repo_count_matches_participating_repos(self):
        """AC1: repo_count equals len(participating_repos)."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {
                    "name": "authentication",
                    "description": "Handles user auth",
                    "participating_repos": ["auth-service", "api-gateway", "user-db"],
                },
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_list()
            domain = result["domains"][0]
            assert domain["repo_count"] == 3

    def test_domains_sorted_alphabetically(self):
        """AC2: Domains returned in alphabetical order by name."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "zebra", "description": "", "participating_repos": []},
                {"name": "alpha", "description": "", "participating_repos": []},
                {"name": "middle", "description": "", "participating_repos": []},
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_list()
            names = [d["name"] for d in result["domains"]]
            assert names == sorted(names)

    def test_returns_empty_on_invalid_json(self):
        """AC1: Invalid _domains.json returns empty domain list (graceful)."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            depmap_dir.mkdir(parents=True, exist_ok=True)
            (depmap_dir / "_domains.json").write_text("not valid json {{{{")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_list()
            assert result["domains"] == []
            assert result["total_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# AC5: Access filtering for domain list
# ─────────────────────────────────────────────────────────────────────────────


class TestGetDomainListAccessFiltering:
    """AC5: Access filtering - non-admin sees only domains with accessible repos."""

    def test_admin_sees_all_domains(self):
        """AC5: accessible_repos=None (admin) sees all domains."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "auth", "description": "", "participating_repos": ["auth-service"]},
                {"name": "billing", "description": "", "participating_repos": ["billing-service"]},
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_list(accessible_repos=None)
            assert result["total_count"] == 2

    def test_non_admin_only_sees_accessible_domains(self):
        """AC5: accessible_repos=set filters to domains with at least one accessible repo."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "auth", "description": "", "participating_repos": ["auth-service"]},
                {"name": "billing", "description": "", "participating_repos": ["billing-service"]},
                {"name": "shared", "description": "", "participating_repos": ["auth-service", "billing-service"]},
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            # User can only access auth-service
            result = service.get_domain_list(accessible_repos={"auth-service"})
            names = [d["name"] for d in result["domains"]]
            assert "auth" in names
            assert "shared" in names
            assert "billing" not in names

    def test_non_admin_filtered_repo_lists(self):
        """AC5: Domain's participating_repos list is filtered to accessible only."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {
                    "name": "shared",
                    "description": "",
                    "participating_repos": ["auth-service", "billing-service"],
                },
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_list(accessible_repos={"auth-service"})
            domain = result["domains"][0]
            assert domain["participating_repos"] == ["auth-service"]
            assert domain["repo_count"] == 1

    def test_non_admin_empty_accessible_set_sees_no_domains(self):
        """AC5: accessible_repos=empty set means no domains visible."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "auth", "description": "", "participating_repos": ["auth-service"]},
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_list(accessible_repos=set())
            assert result["domains"] == []
            assert result["total_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# AC2: Cross-domain dependency parsing from _index.md
# ─────────────────────────────────────────────────────────────────────────────


class TestParseCrossDomainDeps:
    """AC2: _parse_cross_domain_deps() correctly parses markdown table from _index.md."""

    def test_returns_empty_when_index_md_missing(self):
        """AC2: Missing _index.md returns empty list."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service._parse_cross_domain_deps()
            assert result == []

    def test_parses_cross_domain_table(self):
        """AC2: Correctly extracts dependency rows from _index.md markdown table."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_index_md(depmap_dir, """
# Dependency Map Index

## Cross-Domain Dependencies

| Source Domain | Target Domain | Via Repos | Relationship |
|---|---|---|---|
| authentication | user-management | auth-service | imports shared types |
| billing | authentication | billing-service, auth-service | validates tokens |
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service._parse_cross_domain_deps()
            assert len(result) == 2
            assert result[0]["source"] == "authentication"
            assert result[0]["target"] == "user-management"
            assert result[0]["via_repos"] == ["auth-service"]
            assert result[0]["relationship"] == "imports shared types"

    def test_parses_multiple_via_repos(self):
        """AC2: Via repos with comma separation are split into a list."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_index_md(depmap_dir, """
## Cross-Domain Dependencies

| Source Domain | Target Domain | Via Repos | Relationship |
|---|---|---|---|
| billing | authentication | billing-service, auth-service | validates tokens |
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service._parse_cross_domain_deps()
            assert len(result) == 1
            assert result[0]["via_repos"] == ["billing-service", "auth-service"]

    def test_skips_header_and_separator_rows(self):
        """AC2: Header row and separator row (---) are not included in results."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_index_md(depmap_dir, """
| Source Domain | Target Domain | Via Repos | Relationship |
|---|---|---|---|
| authentication | billing | auth-service | depends on |
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service._parse_cross_domain_deps()
            # Only 1 data row - not 3 (header + separator + data)
            assert len(result) == 1
            assert result[0]["source"] == "authentication"

    def test_returns_empty_on_no_table_in_index(self):
        """AC2: _index.md without table returns empty list."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_index_md(depmap_dir, """
# Dependency Map Index

No cross-domain dependencies defined yet.
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service._parse_cross_domain_deps()
            assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# AC3: Domain detail assembly
# ─────────────────────────────────────────────────────────────────────────────


class TestGetDomainDetail:
    """AC3: get_domain_detail() returns assembled domain detail dict."""

    def test_returns_none_for_unknown_domain(self):
        """AC3: Domain not in _domains.json returns None."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_detail("nonexistent")
            assert result is None

    def test_returns_detail_for_known_domain(self):
        """AC3: Known domain returns detail dict with required fields."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {
                    "name": "authentication",
                    "description": "Handles user auth",
                    "participating_repos": ["auth-service", "api-gateway"],
                },
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_detail("authentication")
            assert result is not None
            assert result["name"] == "authentication"
            assert result["description"] == "Handles user auth"
            assert "repos" in result
            assert "outgoing_deps" in result
            assert "incoming_deps" in result
            assert "full_documentation_html" in result
            assert "last_analyzed" in result

    def test_detail_repos_from_participating_repos(self):
        """AC3: repos field comes from participating_repos in _domains.json."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {
                    "name": "authentication",
                    "description": "Handles user auth",
                    "participating_repos": ["auth-service", "api-gateway"],
                },
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_detail("authentication")
            assert set(result["repos"]) == {"auth-service", "api-gateway"}

    def test_detail_outgoing_deps(self):
        """AC3: outgoing_deps lists domains that authentication depends on."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "authentication", "description": "", "participating_repos": ["auth-service"]},
                {"name": "user-management", "description": "", "participating_repos": ["user-svc"]},
            ])
            _write_index_md(depmap_dir, """
## Cross-Domain Dependencies

| Source Domain | Target Domain | Via Repos | Relationship |
|---|---|---|---|
| authentication | user-management | auth-service | imports shared types |
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_detail("authentication")
            assert len(result["outgoing_deps"]) == 1
            assert result["outgoing_deps"][0]["target"] == "user-management"

    def test_detail_incoming_deps(self):
        """AC3: incoming_deps lists domains that depend on authentication."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "authentication", "description": "", "participating_repos": ["auth-service"]},
                {"name": "billing", "description": "", "participating_repos": ["billing-service"]},
            ])
            _write_index_md(depmap_dir, """
## Cross-Domain Dependencies

| Source Domain | Target Domain | Via Repos | Relationship |
|---|---|---|---|
| billing | authentication | billing-service | validates tokens |
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_detail("authentication")
            assert len(result["incoming_deps"]) == 1
            assert result["incoming_deps"][0]["source"] == "billing"

    def test_detail_no_documentation_when_md_missing(self):
        """AC3: full_documentation_html is None when domain .md file is missing."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "authentication", "description": "", "participating_repos": []},
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_detail("authentication")
            assert result["full_documentation_html"] is None

    def test_detail_last_analyzed_none_when_md_missing(self):
        """AC3: last_analyzed is None when domain .md file is missing."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "authentication", "description": "", "participating_repos": []},
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_detail("authentication")
            assert result["last_analyzed"] is None


# ─────────────────────────────────────────────────────────────────────────────
# AC4: Markdown rendering with YAML frontmatter stripping
# ─────────────────────────────────────────────────────────────────────────────


class TestRenderDomainMarkdown:
    """AC4: _render_domain_markdown() strips frontmatter and returns HTML."""

    def test_returns_none_when_file_missing(self):
        """AC4: Missing .md file returns None."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service._render_domain_markdown("nonexistent")
            assert result is None

    def test_renders_markdown_to_html(self):
        """AC4: Valid .md file returns HTML string."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domain_md(depmap_dir, "authentication", """
# Authentication Domain

This domain handles user authentication.
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service._render_domain_markdown("authentication")
            assert result is not None
            assert "<h1>" in result or "<h2>" in result or "Authentication" in result

    def test_strips_yaml_frontmatter(self):
        """AC4: YAML frontmatter (---...---) is stripped before rendering."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domain_md(depmap_dir, "authentication", """---
domain: authentication
last_analyzed: 2026-02-15T10:30:00
---
# Authentication Domain

This domain handles user authentication.
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service._render_domain_markdown("authentication")
            assert result is not None
            # Frontmatter should not appear in rendered HTML
            assert "last_analyzed" not in result
            assert "domain: authentication" not in result
            # Content should appear
            assert "Authentication Domain" in result

    def test_renders_without_frontmatter(self):
        """AC4: Markdown without frontmatter renders normally."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domain_md(depmap_dir, "billing", """
# Billing Domain

Handles payment processing.

## Services
- billing-service
- payment-gateway
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service._render_domain_markdown("billing")
            assert result is not None
            assert "Billing Domain" in result
            assert "<li>" in result


# ─────────────────────────────────────────────────────────────────────────────
# AC3/AC4: last_analyzed from frontmatter
# ─────────────────────────────────────────────────────────────────────────────


class TestGetDomainLastAnalyzed:
    """AC3: last_analyzed is extracted from domain .md YAML frontmatter."""

    def test_extracts_last_analyzed_from_frontmatter(self):
        """AC3: last_analyzed extracted from YAML frontmatter in domain .md."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domain_md(depmap_dir, "authentication", """---
domain: authentication
last_analyzed: 2026-02-15T10:30:00
---
# Authentication Domain
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service._get_domain_last_analyzed("authentication")
            assert result == "2026-02-15T10:30:00"

    def test_returns_none_when_no_frontmatter(self):
        """AC3: Domain .md without frontmatter returns None for last_analyzed."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domain_md(depmap_dir, "billing", """
# Billing Domain

No frontmatter here.
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service._get_domain_last_analyzed("billing")
            assert result is None

    def test_returns_none_when_file_missing(self):
        """AC3: Missing domain .md returns None for last_analyzed."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service._get_domain_last_analyzed("nonexistent")
            assert result is None

    def test_detail_includes_last_analyzed_from_md(self):
        """AC3: get_domain_detail() includes last_analyzed from domain .md frontmatter."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "authentication", "description": "Auth domain", "participating_repos": []},
            ])
            _write_domain_md(depmap_dir, "authentication", """---
domain: authentication
last_analyzed: 2026-02-15T10:30:00
---
# Auth
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_detail("authentication")
            assert result["last_analyzed"] == "2026-02-15T10:30:00"


# ─────────────────────────────────────────────────────────────────────────────
# AC5: Access filtering for domain detail
# ─────────────────────────────────────────────────────────────────────────────


class TestGetDomainDetailAccessFiltering:
    """AC5: get_domain_detail() applies access filtering for non-admin."""

    def test_admin_sees_all_repos_in_detail(self):
        """AC5: accessible_repos=None (admin) sees all contributing repos."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {
                    "name": "shared",
                    "description": "",
                    "participating_repos": ["auth-service", "billing-service"],
                },
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_detail("shared", accessible_repos=None)
            assert set(result["repos"]) == {"auth-service", "billing-service"}

    def test_non_admin_sees_filtered_repos_in_detail(self):
        """AC5: accessible_repos=set filters repos in domain detail."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {
                    "name": "shared",
                    "description": "",
                    "participating_repos": ["auth-service", "billing-service"],
                },
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_detail("shared", accessible_repos={"auth-service"})
            assert result["repos"] == ["auth-service"]

    def test_non_admin_cross_deps_only_visible_domains(self):
        """AC5: Cross-domain deps filter to only visible domains for non-admin."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "authentication", "description": "", "participating_repos": ["auth-service"]},
                {"name": "billing", "description": "", "participating_repos": ["billing-service"]},
                {"name": "user-management", "description": "", "participating_repos": ["user-svc"]},
            ])
            _write_index_md(depmap_dir, """
## Cross-Domain Dependencies

| Source Domain | Target Domain | Via Repos | Relationship |
|---|---|---|---|
| authentication | user-management | auth-service | imports types |
| authentication | billing | auth-service | validates |
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            # User can only see authentication (auth-service) - billing and user-management hidden
            result = service.get_domain_detail(
                "authentication",
                accessible_repos={"auth-service"},
            )
            # user-management and billing are not accessible domains, so no outgoing deps
            assert len(result["outgoing_deps"]) == 0

    def test_full_documentation_included_in_detail(self):
        """AC4: get_domain_detail() includes rendered markdown documentation."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "authentication", "description": "Auth", "participating_repos": []},
            ])
            _write_domain_md(depmap_dir, "authentication", """---
domain: authentication
last_analyzed: 2026-02-15T10:30:00
---
# Authentication Domain

Handles all user authentication flows.
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_detail("authentication")
            assert result["full_documentation_html"] is not None
            assert "Authentication Domain" in result["full_documentation_html"]


# ─────────────────────────────────────────────────────────────────────────────
# C1: Path traversal defense - domain name validation
# ─────────────────────────────────────────────────────────────────────────────


class TestDomainNameValidation:
    """C1: Domain name validation prevents path traversal."""

    def test_rejects_path_traversal_dotdot(self):
        """C1: Domain name with '..' is rejected."""
        Service = _import_service()
        assert Service._validate_domain_name("..") is False

    def test_rejects_path_with_slash(self):
        """C1: Domain name with '/' is rejected."""
        Service = _import_service()
        assert Service._validate_domain_name("../../etc/passwd") is False

    def test_rejects_path_with_backslash(self):
        """C1: Domain name with backslash is rejected."""
        Service = _import_service()
        assert Service._validate_domain_name("..\\..\\etc") is False

    def test_rejects_empty_name(self):
        """C1: Empty domain name is rejected."""
        Service = _import_service()
        assert Service._validate_domain_name("") is False

    def test_accepts_valid_domain_name(self):
        """C1: Normal domain names are accepted."""
        Service = _import_service()
        assert Service._validate_domain_name("authentication") is True
        assert Service._validate_domain_name("user-management") is True
        assert Service._validate_domain_name("billing_v2") is True

    def test_detail_returns_none_for_traversal_name(self):
        """C1: get_domain_detail rejects path traversal domain names."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_domain_detail("../../etc/passwd")
            assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# W1: HTML sanitization for markdown rendering
# ─────────────────────────────────────────────────────────────────────────────


class TestMarkdownSanitization:
    """W1: Rendered markdown is sanitized to prevent XSS."""

    def test_script_tags_stripped(self):
        """W1: Script tags are removed from rendered markdown."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domain_md(depmap_dir, "evil", """# Normal heading

<script>alert('xss')</script>

Some normal text.
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service._render_domain_markdown("evil")
            assert result is not None
            assert "<script" not in result.lower()
            assert "alert" not in result
            assert "Normal heading" in result

    def test_event_handlers_stripped(self):
        """W1: on* event handlers are removed."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domain_md(depmap_dir, "evil2", """# Heading

<div onmouseover="alert('xss')">hover me</div>
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service._render_domain_markdown("evil2")
            assert result is not None
            assert "onmouseover" not in result.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Story #215 AC7: Graph data generation and filtering
# ─────────────────────────────────────────────────────────────────────────────


class TestGetGraphData:
    """Story #215 AC7: get_graph_data() returns nodes and edges for D3.js graph."""

    def test_returns_empty_when_no_domains(self):
        """AC7: No domains returns empty nodes and edges."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_graph_data()
            assert result["nodes"] == []
            assert result["edges"] == []

    def test_returns_nodes_from_domains(self):
        """AC7: Nodes generated from _domains.json entries."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "auth", "description": "Authentication domain", "participating_repos": ["auth-svc"]},
                {"name": "billing", "description": "Billing domain", "participating_repos": ["bill-svc"]},
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_graph_data()
            assert len(result["nodes"]) == 2
            node_ids = {n["id"] for n in result["nodes"]}
            assert node_ids == {"auth", "billing"}

    def test_node_has_required_fields(self):
        """AC7: Each node has id, name, description, repo_count."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "auth", "description": "Auth domain", "participating_repos": ["a", "b"]},
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_graph_data()
            node = result["nodes"][0]
            assert node["id"] == "auth"
            assert node["name"] == "auth"
            assert node["description"] == "Auth domain"
            assert node["repo_count"] == 2

    def test_description_truncated_to_100_chars(self):
        """AC7: Node description is truncated to 100 characters."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            long_desc = "x" * 200
            _write_domains_json(depmap_dir, [
                {"name": "auth", "description": long_desc, "participating_repos": ["a"]},
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_graph_data()
            assert len(result["nodes"][0]["description"]) == 100

    def test_returns_edges_from_cross_deps(self):
        """AC7: Edges generated from _index.md cross-domain dependencies."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "auth", "description": "", "participating_repos": ["auth-svc"]},
                {"name": "billing", "description": "", "participating_repos": ["bill-svc"]},
            ])
            _write_index_md(depmap_dir, """
## Cross-Domain Dependencies

| Source Domain | Target Domain | Via Repos | Relationship |
|---|---|---|---|
| auth | billing | auth-svc | validates payments |
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_graph_data()
            assert len(result["edges"]) == 1
            edge = result["edges"][0]
            assert edge["source"] == "auth"
            assert edge["target"] == "billing"
            assert edge["relationship"] == "validates payments"

    def test_edges_filtered_to_visible_domains(self):
        """AC7: Edges only include connections between visible domains."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "auth", "description": "", "participating_repos": ["auth-svc"]},
                {"name": "billing", "description": "", "participating_repos": ["bill-svc"]},
                {"name": "infra", "description": "", "participating_repos": ["infra-svc"]},
            ])
            _write_index_md(depmap_dir, """
## Cross-Domain Dependencies

| Source Domain | Target Domain | Via Repos | Relationship |
|---|---|---|---|
| auth | billing | auth-svc | validates |
| auth | infra | auth-svc | uses infra |
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            # Non-admin can only see auth-svc
            result = service.get_graph_data(accessible_repos={"auth-svc"})
            # Only "auth" domain is visible (billing and infra have no accessible repos)
            assert len(result["nodes"]) == 1
            assert result["nodes"][0]["id"] == "auth"
            # No edges because targets are not visible
            assert len(result["edges"]) == 0

    def test_admin_sees_all_nodes_and_edges(self):
        """AC7: Admin (accessible_repos=None) sees everything."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "auth", "description": "", "participating_repos": ["auth-svc"]},
                {"name": "billing", "description": "", "participating_repos": ["bill-svc"]},
            ])
            _write_index_md(depmap_dir, """
## Cross-Domain Dependencies

| Source Domain | Target Domain | Via Repos | Relationship |
|---|---|---|---|
| auth | billing | auth-svc | validates |
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_graph_data(accessible_repos=None)
            assert len(result["nodes"]) == 2
            assert len(result["edges"]) == 1

    def test_domains_without_deps_return_nodes_no_edges(self):
        """AC7: Domains with no cross-domain deps return nodes but empty edges."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "auth", "description": "", "participating_repos": ["auth-svc"]},
                {"name": "billing", "description": "", "participating_repos": ["bill-svc"]},
            ])
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_graph_data()
            assert len(result["nodes"]) == 2
            assert len(result["edges"]) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Story #216 AC1: 3-column cross-domain table parsing
# _build_cross_domain_graph generates 3-column tables (Source | Target | Via Repos)
# but _parse_cross_domain_deps only handles 4-column tables. Fix: support 3-col.
# ─────────────────────────────────────────────────────────────────────────────


class TestParseCrossDomainDeps3Column:
    """AC1: _parse_cross_domain_deps handles 3-column tables generated by _build_cross_domain_graph."""

    def test_parses_3_column_table_without_relationship(self):
        """AC1: 3-column table (Source | Target | Via Repos) is parsed correctly."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_index_md(depmap_dir, """
## Cross-Domain Dependency Graph

Directed connections between domains.

| Source Domain | Target Domain | Via Repos |
|---|---|---|
| authentication | user-management | auth-service |
| billing | authentication | billing-service, auth-service |
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service._parse_cross_domain_deps()
            assert len(result) == 2
            assert result[0]["source"] == "authentication"
            assert result[0]["target"] == "user-management"
            assert result[0]["via_repos"] == ["auth-service"]
            # relationship is empty string for 3-column tables
            assert result[0]["relationship"] == ""

    def test_3_column_table_parses_multiple_via_repos(self):
        """AC1: Via repos with comma separation in 3-column table are split into list."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_index_md(depmap_dir, """
## Cross-Domain Dependency Graph

| Source Domain | Target Domain | Via Repos |
|---|---|---|
| billing | authentication | billing-service, auth-service |
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service._parse_cross_domain_deps()
            assert len(result) == 1
            assert result[0]["via_repos"] == ["billing-service", "auth-service"]

    def test_3_column_table_skips_header_and_separator(self):
        """AC1: Header and separator rows are not included in 3-column table results."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_index_md(depmap_dir, """
| Source Domain | Target Domain | Via Repos |
|---|---|---|
| auth | billing | auth-svc |
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service._parse_cross_domain_deps()
            assert len(result) == 1
            assert result[0]["source"] == "auth"

    def test_get_graph_data_returns_edges_from_3_column_table(self):
        """AC1: get_graph_data() returns non-empty edges when _index.md uses 3-column format."""
        Service = _import_service()
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            _write_domains_json(depmap_dir, [
                {"name": "authentication", "description": "", "participating_repos": ["auth-svc"]},
                {"name": "user-management", "description": "", "participating_repos": ["user-svc"]},
            ])
            _write_index_md(depmap_dir, """
## Cross-Domain Dependency Graph

| Source Domain | Target Domain | Via Repos |
|---|---|---|
| authentication | user-management | auth-svc |
""")
            dep_map_svc = _make_dep_map_service(tmp)
            service = Service(dep_map_svc, _make_config_manager())
            result = service.get_graph_data()
            assert len(result["edges"]) == 1
            assert result["edges"][0]["source"] == "authentication"
            assert result["edges"][0]["target"] == "user-management"


# ─────────────────────────────────────────────────────────────────────────────
# Story #216 AC3: identify_affected_domains integration with programmatic format
# _parse_repo_to_domain_mapping must work with the programmatic heading
# "## Repo-to-Domain Matrix" (not the Claude-generated variant)
# ─────────────────────────────────────────────────────────────────────────────


class TestIdentifyAffectedDomainsIntegration:
    """AC3: identify_affected_domains() works with programmatic _index.md format."""

    def _make_service(self, tmp: str):
        """Build a minimal DependencyMapService for testing."""
        from unittest.mock import Mock
        from code_indexer.server.services.dependency_map_service import DependencyMapService

        gm = Mock()
        gm.golden_repos_dir = tmp
        tracking = Mock()
        tracking.get_tracking.return_value = {"status": "completed", "commit_hashes": None}
        config_mgr = Mock()
        analyzer = Mock()
        return DependencyMapService(gm, config_mgr, tracking, analyzer)

    def test_identify_affected_domains_with_programmatic_heading(self):
        """AC3: identify_affected_domains works with '## Repo-to-Domain Matrix' heading."""
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            depmap_dir.mkdir(parents=True)
            # Write _index.md with programmatic heading (exact match)
            index_content = """---
schema_version: 1.0
---

## Repo-to-Domain Matrix

| Repository | Domains |
|---|---|
| auth-service | authentication |
| billing-service | billing |
| shared-lib | authentication, billing |

## Cross-Domain Dependency Graph

| Source Domain | Target Domain | Via Repos |
|---|---|---|
| authentication | billing | auth-service |
"""
            (depmap_dir / "_index.md").write_text(index_content)

            svc = self._make_service(tmp)
            changed_repos = [{"alias": "auth-service", "clone_path": "/fake/auth"}]
            affected = svc.identify_affected_domains(changed_repos, [], [])
            assert "authentication" in affected

    def test_identify_affected_domains_multiple_domains_per_repo(self):
        """AC3: Repo assigned to multiple domains identifies all affected domains."""
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            depmap_dir.mkdir(parents=True)
            index_content = """## Repo-to-Domain Matrix

| Repository | Domains |
|---|---|
| shared-lib | authentication, billing |
"""
            (depmap_dir / "_index.md").write_text(index_content)

            svc = self._make_service(tmp)
            changed_repos = [{"alias": "shared-lib", "clone_path": "/fake/shared"}]
            affected = svc.identify_affected_domains(changed_repos, [], [])
            assert "authentication" in affected
            assert "billing" in affected

    def test_identify_affected_domains_returns_empty_for_unknown_repo(self):
        """AC3: Repo not in matrix returns empty set (no affected domains)."""
        with tempfile.TemporaryDirectory() as tmp:
            depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
            depmap_dir.mkdir(parents=True)
            index_content = """## Repo-to-Domain Matrix

| Repository | Domains |
|---|---|
| auth-service | authentication |
"""
            (depmap_dir / "_index.md").write_text(index_content)

            svc = self._make_service(tmp)
            changed_repos = [{"alias": "unknown-repo", "clone_path": "/fake/unknown"}]
            affected = svc.identify_affected_domains(changed_repos, [], [])
            assert len(affected) == 0
