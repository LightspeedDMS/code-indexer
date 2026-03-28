"""
Synthetic tests for IndexRegenerator (Story #342 AC14).

Tests reproduce regeneration scenarios using real filesystem structures.
No mocking -- tests against real filesystem state and real parsing logic.

Test strategy:
  1. Create a temporary directory with _domains.json and domain .md files
  2. Run regenerator against the directory
  3. Assert the _index.md content is correct (catalog, matrix, cross-domain deps)

All 7 tests map to acceptance criteria AC14 from Story #342.
"""

# Reuse helpers from health detector tests
from tests.unit.server.services.test_dep_map_health_detector import (
    make_domains_json,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helper to get the regenerator
# ─────────────────────────────────────────────────────────────────────────────


def _get_regenerator():
    """Import IndexRegenerator -- fails if not yet implemented."""
    from code_indexer.server.services.dep_map_index_regenerator import (
        IndexRegenerator,
    )

    return IndexRegenerator()


# ─────────────────────────────────────────────────────────────────────────────
# Domain file content helpers
# ─────────────────────────────────────────────────────────────────────────────


def make_domain_md_content(name: str, repos: list, description: str = None) -> str:
    """Build a valid domain .md file content with given name and repos."""
    desc = description or f"Domain {name}"
    repos_yaml = "\n".join(f"  - {r}" for r in repos)
    repos_roles = "\n".join(f"- **{r}**: Participates in {name}." for r in repos)
    return f"""\
---
name: {name}
description: {desc}
participating_repos:
{repos_yaml}
last_analyzed: "2026-01-01T00:00:00Z"
---

# Domain Analysis: {name}

## Overview

This is domain {name} with sufficient content to pass the size check.
It covers repository integration between the participating repositories.
The domain encapsulates all cross-cutting concerns related to data access
and API boundary management. Components in this domain are responsible for
validating inputs, normalizing outputs, and enforcing service contracts.

## Repository Roles

{repos_roles}

## Intra-Domain Dependencies

Standard intra-domain dependency relationships between participating repos.

## Cross-Domain Connections

No verified cross-domain dependencies.
All external integrations are handled via dedicated adapter repositories.
"""


# ─────────────────────────────────────────────────────────────────────────────
# AC14.1: Regenerate _index.md from single domain
# ─────────────────────────────────────────────────────────────────────────────


class TestRegenerateSingleDomain:
    """AC14.1: Regenerate produces correct _index.md for a single domain."""

    def test_regenerate_single_domain(self, tmp_path):
        """Single domain with 2 repos produces correct catalog and matrix in _index.md."""
        make_domains_json(
            tmp_path,
            [
                {
                    "name": "data-access",
                    "description": "Data access layer",
                    "participating_repos": ["repo-alpha", "repo-beta"],
                }
            ],
        )
        content = make_domain_md_content("data-access", ["repo-alpha", "repo-beta"])
        (tmp_path / "data-access.md").write_text(content)

        regenerator = _get_regenerator()
        index_path = regenerator.regenerate(tmp_path)

        assert index_path == tmp_path / "_index.md"
        assert index_path.exists()

        text = index_path.read_text(encoding="utf-8")

        # Catalog section
        assert "## Domain Catalog" in text
        assert "data-access" in text

        # Matrix section
        assert "## Repo-to-Domain Matrix" in text
        assert "repo-alpha" in text
        assert "repo-beta" in text

        # Both repos mapped to data-access
        assert "| repo-alpha | data-access |" in text
        assert "| repo-beta | data-access |" in text


# ─────────────────────────────────────────────────────────────────────────────
# AC14.2: Regenerate from multiple domains
# ─────────────────────────────────────────────────────────────────────────────


class TestRegenerateMultipleDomains:
    """AC14.2: Regenerate produces correct _index.md for multiple domains."""

    def test_regenerate_multiple_domains(self, tmp_path):
        """3 domains all appear in catalog and matrix in regenerated _index.md."""
        domains = [
            {
                "name": "auth-domain",
                "description": "Authentication",
                "participating_repos": ["auth-svc"],
            },
            {
                "name": "billing-domain",
                "description": "Billing",
                "participating_repos": ["billing-svc", "payment-svc"],
            },
            {
                "name": "ui-domain",
                "description": "User interface",
                "participating_repos": ["web-app"],
            },
        ]
        make_domains_json(tmp_path, domains)
        (tmp_path / "auth-domain.md").write_text(
            make_domain_md_content("auth-domain", ["auth-svc"])
        )
        (tmp_path / "billing-domain.md").write_text(
            make_domain_md_content("billing-domain", ["billing-svc", "payment-svc"])
        )
        (tmp_path / "ui-domain.md").write_text(
            make_domain_md_content("ui-domain", ["web-app"])
        )

        regenerator = _get_regenerator()
        index_path = regenerator.regenerate(tmp_path)

        text = index_path.read_text(encoding="utf-8")

        # All 3 domains in catalog
        assert "auth-domain" in text
        assert "billing-domain" in text
        assert "ui-domain" in text

        # All repos in matrix
        assert "auth-svc" in text
        assert "billing-svc" in text
        assert "payment-svc" in text
        assert "web-app" in text

        # Correct domain mappings in matrix
        assert "| auth-svc | auth-domain |" in text
        assert "| billing-svc | billing-domain |" in text
        assert "| payment-svc | billing-domain |" in text
        assert "| web-app | ui-domain |" in text


# ─────────────────────────────────────────────────────────────────────────────
# AC14.3: Missing domain file is skipped gracefully
# ─────────────────────────────────────────────────────────────────────────────


class TestRegenerateMissingDomainFileSkipped:
    """AC14.3: Domain in JSON but file missing is skipped without error."""

    def test_regenerate_missing_domain_file_skipped(self, tmp_path):
        """Domain in _domains.json but missing .md file is skipped gracefully."""
        make_domains_json(
            tmp_path,
            [
                {
                    "name": "present-domain",
                    "description": "Present",
                    "participating_repos": ["repo-a"],
                },
                {
                    "name": "missing-domain",
                    "description": "Missing",
                    "participating_repos": ["repo-b"],
                },
            ],
        )
        # Only create present-domain.md, not missing-domain.md
        (tmp_path / "present-domain.md").write_text(
            make_domain_md_content("present-domain", ["repo-a"])
        )

        regenerator = _get_regenerator()
        # Should not raise
        index_path = regenerator.regenerate(tmp_path)

        text = index_path.read_text(encoding="utf-8")
        # present-domain appears
        assert "present-domain" in text
        # missing-domain gracefully absent from output
        assert "missing-domain" not in text


# ─────────────────────────────────────────────────────────────────────────────
# AC14.4: No cross-domain dependencies produces correct placeholder
# ─────────────────────────────────────────────────────────────────────────────


class TestRegenerateNoCrossDomainDeps:
    """AC14.4: When no cross-domain deps exist, index contains placeholder text."""

    def test_regenerate_no_cross_domain_deps(self, tmp_path):
        """Domains with no cross-domain references produce placeholder in index."""
        make_domains_json(
            tmp_path,
            [
                {
                    "name": "isolated-domain",
                    "description": "Isolated",
                    "participating_repos": ["repo-x"],
                }
            ],
        )
        (tmp_path / "isolated-domain.md").write_text(
            make_domain_md_content("isolated-domain", ["repo-x"])
        )

        regenerator = _get_regenerator()
        index_path = regenerator.regenerate(tmp_path)

        text = index_path.read_text(encoding="utf-8")

        assert "## Cross-Domain Dependencies" in text
        # When no cross-domain dependencies, a placeholder line is present
        assert "No cross-domain dependencies detected" in text


# ─────────────────────────────────────────────────────────────────────────────
# AC14.5: Output _index.md has valid YAML frontmatter with correct fields
# ─────────────────────────────────────────────────────────────────────────────


class TestRegeneratePreservesFrontmatter:
    """AC14.5: Output _index.md has YAML frontmatter with required fields."""

    def test_regenerate_preserves_frontmatter(self, tmp_path):
        """Regenerated _index.md has YAML frontmatter with schema_version and counts."""
        make_domains_json(
            tmp_path,
            [
                {
                    "name": "core-domain",
                    "description": "Core",
                    "participating_repos": ["repo-1", "repo-2"],
                }
            ],
        )
        (tmp_path / "core-domain.md").write_text(
            make_domain_md_content("core-domain", ["repo-1", "repo-2"])
        )

        regenerator = _get_regenerator()
        index_path = regenerator.regenerate(tmp_path)

        text = index_path.read_text(encoding="utf-8")

        # Must start with YAML frontmatter
        assert text.startswith("---\n")
        assert "schema_version:" in text
        assert "domains_count:" in text
        assert "repos_analyzed_count:" in text
        assert "last_analyzed:" in text
        assert "repos_analyzed:" in text

        # Correct counts
        assert "domains_count: 1" in text
        assert "repos_analyzed_count: 2" in text


# ─────────────────────────────────────────────────────────────────────────────
# AC14.6: Existing _index.md is overwritten
# ─────────────────────────────────────────────────────────────────────────────


class TestRegenerateOverwritesExistingIndex:
    """AC14.6: Existing _index.md is replaced by regenerate()."""

    def test_regenerate_overwrites_existing_index(self, tmp_path):
        """Pre-existing _index.md is replaced with fresh regenerated content."""
        make_domains_json(
            tmp_path,
            [
                {
                    "name": "fresh-domain",
                    "description": "Fresh",
                    "participating_repos": ["repo-new"],
                }
            ],
        )
        (tmp_path / "fresh-domain.md").write_text(
            make_domain_md_content("fresh-domain", ["repo-new"])
        )

        # Write stale/old content to _index.md
        old_content = "STALE OLD INDEX CONTENT - should be replaced"
        (tmp_path / "_index.md").write_text(old_content)

        regenerator = _get_regenerator()
        index_path = regenerator.regenerate(tmp_path)

        text = index_path.read_text(encoding="utf-8")

        # Old stale content must be gone
        assert "STALE OLD INDEX CONTENT" not in text
        # Fresh domain appears in new content
        assert "fresh-domain" in text
        assert "repo-new" in text


# ─────────────────────────────────────────────────────────────────────────────
# AC14.7: Empty _domains.json produces minimal valid _index.md
# ─────────────────────────────────────────────────────────────────────────────


class TestRegenerateEmptyDomains:
    """AC14.7: Empty _domains.json produces minimal but valid _index.md."""

    def test_regenerate_empty_domains(self, tmp_path):
        """Empty _domains.json produces a valid _index.md with zero counts."""
        make_domains_json(tmp_path, [])

        regenerator = _get_regenerator()
        index_path = regenerator.regenerate(tmp_path)

        assert index_path.exists()
        text = index_path.read_text(encoding="utf-8")

        # Must still have required sections
        assert "## Domain Catalog" in text
        assert "## Repo-to-Domain Matrix" in text
        assert "## Cross-Domain Dependencies" in text

        # Frontmatter counts must be 0
        assert "domains_count: 0" in text
        assert "repos_analyzed_count: 0" in text


# ─────────────────────────────────────────────────────────────────────────────
# Bug #348: Cross-domain edge parsing fixes
# ─────────────────────────────────────────────────────────────────────────────


def make_domain_md_with_outgoing(name: str, repos: list, outgoing_rows: list) -> str:
    """
    Build a domain .md file with a populated Outgoing Dependencies table.

    outgoing_rows: list of (source_repo, depends_on, target_domain, dep_type, why) tuples
    """
    repos_yaml = "\n".join(f"  - {r}" for r in repos)
    repos_roles = "\n".join(f"- **{r}**: Participates in {name}." for r in repos)

    rows_md = ""
    for source_repo, depends_on, target_domain, dep_type, why in outgoing_rows:
        rows_md += (
            f"| {source_repo} | {depends_on} | {target_domain}"
            f" | {dep_type} | {why} | see code |\n"
        )

    return f"""\
---
name: {name}
description: Domain {name}
participating_repos:
{repos_yaml}
last_analyzed: "2026-01-01T00:00:00Z"
---

# Domain Analysis: {name}

## Overview

This is domain {name} with sufficient content to pass the size check.

## Repository Roles

{repos_roles}

## Intra-Domain Dependencies

Standard intra-domain dependency relationships.

## Cross-Domain Connections

### Outgoing Dependencies

| This Repo | Depends On | Target Domain | Type | Why | Evidence |
|---|---|---|---|---|---|
{rows_md}
### Incoming Dependencies

None detected.
"""


def make_domain_md_with_no_outgoing(name: str, repos: list) -> str:
    """Build a domain .md file with Outgoing Dependencies sentinel (no deps)."""
    repos_yaml = "\n".join(f"  - {r}" for r in repos)
    repos_roles = "\n".join(f"- **{r}**: Participates in {name}." for r in repos)

    return f"""\
---
name: {name}
description: Domain {name}
participating_repos:
{repos_yaml}
last_analyzed: "2026-01-01T00:00:00Z"
---

# Domain Analysis: {name}

## Overview

Domain {name} has no outgoing cross-domain dependencies.

## Repository Roles

{repos_roles}

## Intra-Domain Dependencies

Standard intra-domain dependency relationships.

## Cross-Domain Connections

### Outgoing Dependencies

No verified cross-domain dependencies.

### Incoming Dependencies

None detected.
"""


class TestRegenerateMixedCaseDomainEdges:
    """Bug #348 fix: Mixed-case domain names must produce correct cross-domain edges."""

    def test_mixed_case_domains_produce_edges(self, tmp_path):
        """
        Domains with mixed-case names (e.g., 'Core DMS Platform') must produce
        cross-domain edges in the regenerated _index.md.

        Previously broken: case-sensitivity mismatch caused `if target in line_lower`
        to always fail for mixed-case domain names.
        """
        source_domain = "Core DMS Platform"
        target_domain = "Dealer Configuration"

        domains = [
            {
                "name": source_domain,
                "description": "Core DMS platform services",
                "participating_repos": ["dms-core", "dms-api"],
            },
            {
                "name": target_domain,
                "description": "Dealer configuration management",
                "participating_repos": ["dealer-config"],
            },
        ]
        make_domains_json(tmp_path, domains)

        # Source domain has an outgoing edge to target domain
        (tmp_path / f"{source_domain}.md").write_text(
            make_domain_md_with_outgoing(
                source_domain,
                ["dms-core", "dms-api"],
                [
                    (
                        "dms-core",
                        "dealer-config",
                        target_domain,
                        "api-call",
                        "Reads dealer config data",
                    )
                ],
            )
        )
        (tmp_path / f"{target_domain}.md").write_text(
            make_domain_md_with_no_outgoing(target_domain, ["dealer-config"])
        )

        regenerator = _get_regenerator()
        index_path = regenerator.regenerate(tmp_path)
        text = index_path.read_text(encoding="utf-8")

        # Cross-domain section must exist
        assert "## Cross-Domain Dependencies" in text
        # Must NOT contain the "no dependencies" placeholder
        assert "No cross-domain dependencies detected" not in text
        # Edge must appear: source → target
        assert source_domain in text
        assert target_domain in text
        # A table row connecting them must be present
        assert f"| {source_domain} | {target_domain} |" in text


class TestRegenerateLowercaseDomainEdgesRegression:
    """Bug #348 fix regression: Lowercase domain names must still produce edges."""

    def test_lowercase_domains_produce_edges(self, tmp_path):
        """
        Lowercase domain names (e.g., 'auth-domain') must still produce
        cross-domain edges -- regression test after case-sensitivity fix.
        """
        source_domain = "auth-domain"
        target_domain = "data-domain"

        domains = [
            {
                "name": source_domain,
                "description": "Authentication domain",
                "participating_repos": ["auth-svc"],
            },
            {
                "name": target_domain,
                "description": "Data access domain",
                "participating_repos": ["data-svc"],
            },
        ]
        make_domains_json(tmp_path, domains)

        (tmp_path / f"{source_domain}.md").write_text(
            make_domain_md_with_outgoing(
                source_domain,
                ["auth-svc"],
                [
                    (
                        "auth-svc",
                        "data-svc",
                        target_domain,
                        "db-query",
                        "User credential lookup",
                    )
                ],
            )
        )
        (tmp_path / f"{target_domain}.md").write_text(
            make_domain_md_with_no_outgoing(target_domain, ["data-svc"])
        )

        regenerator = _get_regenerator()
        index_path = regenerator.regenerate(tmp_path)
        text = index_path.read_text(encoding="utf-8")

        assert "## Cross-Domain Dependencies" in text
        assert "No cross-domain dependencies detected" not in text
        assert f"| {source_domain} | {target_domain} |" in text


class TestRegenerateEmptyOutgoingTableProducesNoEdges:
    """Bug #348: Outgoing table with sentinel produces no edges."""

    def test_empty_outgoing_table_produces_no_edges(self, tmp_path):
        """
        A domain whose Outgoing Dependencies section contains only the sentinel
        'No verified cross-domain dependencies.' must produce no cross-domain edges.
        """
        domains = [
            {
                "name": "domain-a",
                "description": "Domain A",
                "participating_repos": ["repo-a"],
            },
            {
                "name": "domain-b",
                "description": "Domain B",
                "participating_repos": ["repo-b"],
            },
        ]
        make_domains_json(tmp_path, domains)

        (tmp_path / "domain-a.md").write_text(
            make_domain_md_with_no_outgoing("domain-a", ["repo-a"])
        )
        (tmp_path / "domain-b.md").write_text(
            make_domain_md_with_no_outgoing("domain-b", ["repo-b"])
        )

        regenerator = _get_regenerator()
        index_path = regenerator.regenerate(tmp_path)
        text = index_path.read_text(encoding="utf-8")

        assert "## Cross-Domain Dependencies" in text
        assert "No cross-domain dependencies detected" in text


class TestRegenerateCorrect5ColumnFormat:
    """Bug #348 fix: Regenerated cross-domain table must use 5-column format."""

    def test_cross_domain_table_has_5_column_format(self, tmp_path):
        """
        The cross-domain table must use the 5-column format:
        Source Domain | Target Domain | Via Repos | Type | Why

        Previously broken: regenerator wrote 3-column format
        'Source Domain | Target Domain | Evidence'.
        """
        source_domain = "service-domain"
        target_domain = "infra-domain"

        domains = [
            {
                "name": source_domain,
                "description": "Service layer",
                "participating_repos": ["svc-repo"],
            },
            {
                "name": target_domain,
                "description": "Infrastructure layer",
                "participating_repos": ["infra-repo"],
            },
        ]
        make_domains_json(tmp_path, domains)

        (tmp_path / f"{source_domain}.md").write_text(
            make_domain_md_with_outgoing(
                source_domain,
                ["svc-repo"],
                [
                    (
                        "svc-repo",
                        "infra-repo",
                        target_domain,
                        "http-call",
                        "Service calls infra API",
                    )
                ],
            )
        )
        (tmp_path / f"{target_domain}.md").write_text(
            make_domain_md_with_no_outgoing(target_domain, ["infra-repo"])
        )

        regenerator = _get_regenerator()
        index_path = regenerator.regenerate(tmp_path)
        text = index_path.read_text(encoding="utf-8")

        # Must have 5-column header
        assert "| Source Domain | Target Domain | Via Repos | Type | Why |" in text
        # Must NOT have old 3-column header
        assert "| Source Domain | Target Domain | Evidence |" not in text
