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

import json
from pathlib import Path

import pytest

# Reuse helpers from health detector tests
from tests.unit.server.services.test_dep_map_health_detector import (
    VALID_DOMAIN_CONTENT,
    VALID_INDEX_CONTENT,
    make_domains_json,
    make_domain_file,
    make_index_md,
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
    repos_roles = "\n".join(
        f"- **{r}**: Participates in {name}." for r in repos
    )
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
