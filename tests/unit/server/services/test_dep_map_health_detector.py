"""
Synthetic tests for DepMapHealthDetector (Story #342).

Tests reproduce all repairability conditions using real filesystem structures.
No mocking of the health detector itself -- tests against real filesystem state.

Test strategy:
  1. Create a temporary directory structure mimicking a dependency map output
  2. Write synthetic _domains.json, domain .md files, _index.md as needed
  3. Introduce the specific anomaly being tested
  4. Run the detector against the synthetic directory
  5. Assert the correct anomalies are detected and correct status returned

All 15 tests map to acceptance criteria AC1-AC9 from Story #342.
"""

import json
from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers for building synthetic dep-map output directories
# ─────────────────────────────────────────────────────────────────────────────

VALID_DOMAIN_CONTENT = """\
---
name: test-domain
description: A test domain for unit testing
participating_repos:
  - repo-alpha
  - repo-beta
last_analyzed: "2026-01-01T00:00:00Z"
---

# Domain Analysis: test-domain

## Overview

This is a test domain with sufficient content to pass the size check.
It covers repository integration between repo-alpha and repo-beta.
The domain encapsulates all cross-cutting concerns related to data access
and API boundary management. Components in this domain are responsible for
validating inputs, normalizing outputs, and enforcing service contracts.

## Repository Roles

- **repo-alpha**: Primary service provider implementing the core logic.
  This repository contains the authoritative implementation of the business
  rules and exposes a stable REST API consumed by downstream repositories.
- **repo-beta**: Secondary consumer that depends on repo-alpha's API.
  This repository handles user-facing features and delegates domain logic
  to repo-alpha via synchronous HTTP calls.

## Intra-Domain Dependencies

repo-beta imports from repo-alpha via standard REST API calls.
Evidence: repo-beta/src/client.py imports requests and calls /api/v1/items.
The dependency is unidirectional: repo-beta -> repo-alpha. No circular
dependency exists. Version compatibility is enforced via semver pinning.

## Cross-Domain Connections

No verified cross-domain dependencies.
All external integrations are handled via dedicated adapter repositories
that are not part of this domain's bounded context.
"""

VALID_INDEX_CONTENT = """\
---
schema_version: 1.0
last_analyzed: "2026-01-01T00:00:00Z"
repos_analyzed_count: 2
domains_count: 1
repos_analyzed:
  - repo-alpha
  - repo-beta
---

# Dependency Map Index

## Domain Catalog

| Domain | Description | Repo Count |
|---|---|---|
| test-domain | A test domain | 2 |

## Repo-to-Domain Matrix

| Repository | Domain |
|---|---|
| repo-alpha | test-domain |
| repo-beta | test-domain |

## Cross-Domain Dependencies

_No cross-domain dependencies detected._
"""


def make_domains_json(output_dir: Path, domains: list) -> None:
    """Write _domains.json with given domain list."""
    (output_dir / "_domains.json").write_text(json.dumps(domains))


def make_domain_file(
    output_dir: Path, name: str, content: str = None, size: int = None
) -> Path:
    """Create a domain .md file with given content or padded to a given size."""
    path = output_dir / f"{name}.md"
    if size is not None:
        path.write_text("x" * size)
    elif content is not None:
        path.write_text(content)
    else:
        path.write_text(VALID_DOMAIN_CONTENT.replace("test-domain", name))
    return path


def make_index_md(output_dir: Path, content: str = None) -> Path:
    """Create _index.md with given content or valid default."""
    path = output_dir / "_index.md"
    path.write_text(content if content is not None else VALID_INDEX_CONTENT)
    return path


def make_healthy_output_dir(output_dir: Path, domain_names=None) -> None:
    """Create a fully valid dependency map output directory."""
    if domain_names is None:
        domain_names = ["test-domain"]

    domains = []
    for name in domain_names:
        domains.append(
            {
                "name": name,
                "description": f"Domain {name}",
                "participating_repos": ["repo-alpha", "repo-beta"],
            }
        )
    make_domains_json(output_dir, domains)

    for name in domain_names:
        make_domain_file(output_dir, name)

    # Build valid index with all repos from all domains
    make_index_md(output_dir, _build_valid_index_for_domains(domain_names))


def _build_valid_index_for_domains(domain_names: list) -> str:
    """Build a valid _index.md content for the given domain names."""
    repos = ["repo-alpha", "repo-beta"]
    catalog_rows = "\n".join(
        f"| {name} | Domain {name} | 2 |" for name in domain_names
    )
    matrix_rows = "\n".join(f"| {repo} | {domain_names[0]} |" for repo in repos)
    return f"""\
---
schema_version: 1.0
last_analyzed: "2026-01-01T00:00:00Z"
repos_analyzed_count: {len(repos)}
domains_count: {len(domain_names)}
repos_analyzed:
  - repo-alpha
  - repo-beta
---

# Dependency Map Index

## Domain Catalog

| Domain | Description | Repo Count |
|---|---|---|
{catalog_rows}

## Repo-to-Domain Matrix

| Repository | Domain |
|---|---|
{matrix_rows}

## Cross-Domain Dependencies

_No cross-domain dependencies detected._
"""


# ─────────────────────────────────────────────────────────────────────────────
# Import the detector (will fail until implementation exists - RED phase)
# ─────────────────────────────────────────────────────────────────────────────


def _get_detector():
    """Import DepMapHealthDetector -- fails if not yet implemented."""
    from code_indexer.server.services.dep_map_health_detector import (
        DepMapHealthDetector,
    )
    return DepMapHealthDetector()


# ─────────────────────────────────────────────────────────────────────────────
# AC1: Zero-char domain file detected (status="critical")
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectZeroCharDomain:
    """AC1: Zero-character domain file triggers critical health status."""

    def test_detect_zero_char_domain(self, tmp_path):
        """Zero-byte domain file produces zero_char_domain anomaly with critical status."""
        make_domains_json(
            tmp_path,
            [
                {
                    "name": "txt-db-storage-engine",
                    "description": "Storage engine domain",
                    "participating_repos": ["repo-a"],
                },
                {
                    "name": "payments-platform",
                    "description": "Payments domain",
                    "participating_repos": ["repo-b"],
                },
            ],
        )
        # Create valid domain file
        make_domain_file(tmp_path, "payments-platform")
        # Create ZERO-byte domain file
        (tmp_path / "txt-db-storage-engine.md").write_text("")
        make_index_md(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        assert report.status == "critical"
        anomaly_types = [a.type for a in report.anomalies]
        assert "zero_char_domain" in anomaly_types

        zero_char = next(a for a in report.anomalies if a.type == "zero_char_domain")
        assert zero_char.domain == "txt-db-storage-engine"

    def test_zero_char_sets_critical_not_needs_repair(self, tmp_path):
        """Confirm zero_char escalates to critical, not just needs_repair."""
        make_domains_json(
            tmp_path,
            [{"name": "my-domain", "description": "d", "participating_repos": ["r"]}],
        )
        (tmp_path / "my-domain.md").write_text("")
        make_index_md(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        assert report.status == "critical"


# ─────────────────────────────────────────────────────────────────────────────
# AC2: Missing domain file detected (status="critical")
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectMissingDomainFile:
    """AC2: Domain listed in _domains.json but file missing on disk."""

    def test_detect_missing_domain_file(self, tmp_path):
        """Missing domain .md file produces missing_domain_file anomaly with critical status."""
        make_domains_json(
            tmp_path,
            [
                {
                    "name": "payments-platform",
                    "description": "Payments domain",
                    "participating_repos": ["repo-a"],
                }
            ],
        )
        # Do NOT create payments-platform.md
        make_index_md(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        assert report.status == "critical"
        anomaly_types = [a.type for a in report.anomalies]
        assert "missing_domain_file" in anomaly_types

        missing = next(a for a in report.anomalies if a.type == "missing_domain_file")
        assert missing.domain == "payments-platform"

    def test_missing_domain_sets_critical(self, tmp_path):
        """Confirm missing domain escalates to critical status."""
        make_domains_json(
            tmp_path,
            [
                {"name": "ghost-domain", "description": "d", "participating_repos": []},
                {"name": "real-domain", "description": "d", "participating_repos": []},
            ],
        )
        make_domain_file(tmp_path, "real-domain")
        # ghost-domain.md intentionally absent
        make_index_md(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        assert report.status == "critical"


# ─────────────────────────────────────────────────────────────────────────────
# AC3: Undersized domain detected (status="needs_repair")
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectUndersizedDomain:
    """AC3: Domain file exists but is suspiciously small (< 1000 chars)."""

    def test_detect_undersized_domain(self, tmp_path):
        """Domain file with <1000 chars produces undersized_domain anomaly, needs_repair."""
        make_domains_json(
            tmp_path,
            [
                {
                    "name": "tiny-domain",
                    "description": "Small domain",
                    "participating_repos": ["r"],
                }
            ],
        )
        # 500 chars -- below the 1000 threshold
        make_domain_file(tmp_path, "tiny-domain", size=500)
        make_index_md(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        assert report.status == "needs_repair"
        anomaly_types = [a.type for a in report.anomalies]
        assert "undersized_domain" in anomaly_types

        undersized = next(a for a in report.anomalies if a.type == "undersized_domain")
        assert undersized.domain == "tiny-domain"
        assert undersized.size == 500

    def test_undersized_domain_not_critical(self, tmp_path):
        """Undersized domain should be needs_repair, NOT critical."""
        make_domains_json(
            tmp_path,
            [{"name": "small-d", "description": "d", "participating_repos": []}],
        )
        make_domain_file(tmp_path, "small-d", size=999)
        make_index_md(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        assert report.status == "needs_repair"
        assert report.status != "critical"

    def test_domain_at_exactly_1000_chars_is_not_undersized(self, tmp_path):
        """Domain file at exactly 1000 chars should NOT be flagged as undersized."""
        make_domains_json(
            tmp_path,
            [{"name": "ok-domain", "description": "d", "participating_repos": []}],
        )
        make_domain_file(tmp_path, "ok-domain", size=1000)
        make_index_md(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        anomaly_types = [a.type for a in report.anomalies]
        assert "undersized_domain" not in anomaly_types


# ─────────────────────────────────────────────────────────────────────────────
# AC4: Orphan .md file not in _domains.json
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectOrphanDomainFile:
    """AC4: A markdown file exists on disk but is not tracked in _domains.json."""

    def test_detect_orphan_domain_file(self, tmp_path):
        """Untracked .md file produces orphan_domain_file anomaly."""
        make_domains_json(
            tmp_path,
            [{"name": "known-domain", "description": "d", "participating_repos": []}],
        )
        make_domain_file(tmp_path, "known-domain")
        # Create orphan file not in _domains.json
        (tmp_path / "old-domain.md").write_text("orphan content")
        make_index_md(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        anomaly_types = [a.type for a in report.anomalies]
        assert "orphan_domain_file" in anomaly_types

        orphan = next(a for a in report.anomalies if a.type == "orphan_domain_file")
        assert orphan.file == "old-domain.md"

    def test_underscore_prefixed_files_not_flagged_as_orphans(self, tmp_path):
        """_domains.json, _index.md, _activity.md should NOT be flagged as orphans."""
        make_domains_json(
            tmp_path,
            [{"name": "my-domain", "description": "d", "participating_repos": []}],
        )
        make_domain_file(tmp_path, "my-domain")
        make_index_md(tmp_path)
        # Create underscore-prefixed metadata files
        (tmp_path / "_activity.md").write_text("activity log")
        (tmp_path / "_journal.md").write_text("journal log")

        detector = _get_detector()
        report = detector.detect(tmp_path)

        orphan_files = [
            a.file for a in report.anomalies if a.type == "orphan_domain_file"
        ]
        assert "_activity.md" not in orphan_files
        assert "_journal.md" not in orphan_files
        assert "_index.md" not in orphan_files
        assert "_domains.json" not in orphan_files


# ─────────────────────────────────────────────────────────────────────────────
# AC5: _domains.json count vs .md file count mismatch
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectDomainCountMismatch:
    """AC5: Number of domains in JSON does not match number of domain files on disk."""

    def test_detect_domain_count_mismatch(self, tmp_path):
        """Mismatch between JSON count and file count produces domain_count_mismatch anomaly."""
        # 4 in JSON, but only 3 files on disk
        make_domains_json(
            tmp_path,
            [
                {"name": "domain-a", "description": "d", "participating_repos": []},
                {"name": "domain-b", "description": "d", "participating_repos": []},
                {"name": "domain-c", "description": "d", "participating_repos": []},
                {"name": "domain-d", "description": "d", "participating_repos": []},
            ],
        )
        make_domain_file(tmp_path, "domain-a")
        make_domain_file(tmp_path, "domain-b")
        make_domain_file(tmp_path, "domain-c")
        # domain-d.md intentionally absent
        make_index_md(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        anomaly_types = [a.type for a in report.anomalies]
        assert "domain_count_mismatch" in anomaly_types

        mismatch = next(
            a for a in report.anomalies if a.type == "domain_count_mismatch"
        )
        assert mismatch.json_count == 4
        assert mismatch.file_count == 3


# ─────────────────────────────────────────────────────────────────────────────
# AC6: Missing _index.md (status="needs_repair")
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectMissingIndex:
    """AC6: Output directory has domain files but no _index.md."""

    def test_detect_missing_index(self, tmp_path):
        """Missing _index.md produces missing_index anomaly with needs_repair status."""
        make_domains_json(
            tmp_path,
            [{"name": "my-domain", "description": "d", "participating_repos": []}],
        )
        make_domain_file(tmp_path, "my-domain")
        # Do NOT create _index.md

        detector = _get_detector()
        report = detector.detect(tmp_path)

        assert report.status == "needs_repair"
        anomaly_types = [a.type for a in report.anomalies]
        assert "missing_index" in anomaly_types

    def test_missing_index_not_critical(self, tmp_path):
        """Missing _index.md should be needs_repair (not critical) since it's a derivative."""
        make_domains_json(
            tmp_path,
            [{"name": "d", "description": "d", "participating_repos": []}],
        )
        make_domain_file(tmp_path, "d")
        # No _index.md

        detector = _get_detector()
        report = detector.detect(tmp_path)

        assert report.status != "critical"
        assert report.status == "needs_repair"


# ─────────────────────────────────────────────────────────────────────────────
# AC7: Stale _index.md (repos not in matrix)
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectStaleIndex:
    """AC7: _index.md repo-to-domain matrix is out of sync with domain frontmatter."""

    def test_detect_stale_index(self, tmp_path):
        """_index.md missing repos that appear in domain frontmatter produces stale_index."""
        # Domain file references repos A, B, C, D
        domain_content = """\
---
name: my-domain
description: Test domain
participating_repos:
  - repo-a
  - repo-b
  - repo-c
  - repo-d
last_analyzed: "2026-01-01T00:00:00Z"
---

# Domain Analysis: my-domain

## Overview

This domain covers integration across four repositories.
Testing stale index detection when one repo is missing from the matrix.

## Repository Roles

- **repo-a**: Core service.
- **repo-b**: Secondary service.
- **repo-c**: Client library.
- **repo-d**: New repo added after index was generated.

## Intra-Domain Dependencies

All repos integrate via shared REST API.

## Cross-Domain Connections

No verified cross-domain dependencies.
"""
        make_domains_json(
            tmp_path,
            [
                {
                    "name": "my-domain",
                    "description": "d",
                    "participating_repos": ["repo-a", "repo-b", "repo-c", "repo-d"],
                }
            ],
        )
        (tmp_path / "my-domain.md").write_text(domain_content)

        # _index.md only lists repos A, B, C (missing D)
        index_content = """\
---
schema_version: 1.0
last_analyzed: "2026-01-01T00:00:00Z"
repos_analyzed_count: 3
domains_count: 1
repos_analyzed:
  - repo-a
  - repo-b
  - repo-c
---

# Dependency Map Index

## Domain Catalog

| Domain | Description | Repo Count |
|---|---|---|
| my-domain | Test domain | 3 |

## Repo-to-Domain Matrix

| Repository | Domain |
|---|---|
| repo-a | my-domain |
| repo-b | my-domain |
| repo-c | my-domain |

## Cross-Domain Dependencies

_No cross-domain dependencies detected._
"""
        (tmp_path / "_index.md").write_text(index_content)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        anomaly_types = [a.type for a in report.anomalies]
        assert "stale_index" in anomaly_types

        stale = next(a for a in report.anomalies if a.type == "stale_index")
        assert "repo-d" in stale.missing_repos


# ─────────────────────────────────────────────────────────────────────────────
# AC8: Incomplete domain (missing required sections)
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectIncompleteDomain:
    """AC8: Domain file is missing required structural sections."""

    def test_detect_malformed_domain_no_frontmatter(self, tmp_path):
        """Domain file without YAML frontmatter produces malformed_domain anomaly."""
        make_domains_json(
            tmp_path,
            [{"name": "bad-domain", "description": "d", "participating_repos": []}],
        )
        # Large enough to pass size check but no YAML frontmatter
        content = "x" * 1200 + "\n# Domain Analysis: bad-domain\n\n## Overview\n\nSome content.\n\n## Repository Roles\n\nRoles.\n\n## Intra-Domain Dependencies\n\nNone.\n\n## Cross-Domain Connections\n\nNone.\n"
        (tmp_path / "bad-domain.md").write_text(content)
        make_index_md(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        anomaly_types = [a.type for a in report.anomalies]
        assert "malformed_domain" in anomaly_types

        malformed = next(a for a in report.anomalies if a.type == "malformed_domain")
        assert malformed.domain == "bad-domain"

    def test_detect_incomplete_domain_missing_sections(self, tmp_path):
        """Domain file missing required sections produces incomplete_domain anomaly."""
        make_domains_json(
            tmp_path,
            [
                {
                    "name": "partial-domain",
                    "description": "d",
                    "participating_repos": [],
                }
            ],
        )
        # Has frontmatter and Overview but missing Repository Roles and Cross-Domain Connections.
        # Content must exceed DOMAIN_SIZE_THRESHOLD (1000 bytes) so Check 1 passes and Check 5 runs.
        content = """\
---
name: partial-domain
description: Partial domain
participating_repos: []
---

# Domain Analysis: partial-domain

## Overview

This domain has frontmatter and overview but is missing other required sections.
It contains enough content to pass the size check but lacks structural completeness.
This is padding text to make the file exceed the 1000 character threshold for size checking.
More padding content here to ensure we reach the required minimum size.
Additional lines of content are included purely to push this file above the 1000-byte
threshold so that the detector does not classify it as undersized and instead proceeds
to check for the presence of the required structural sections such as Repository Roles.
Without this padding the file would be flagged as undersized rather than incomplete.
This distinction is important: undersized means the file is too short to be meaningful,
while incomplete means the file is large enough but missing required section headers.
Extra padding line to push content well past the 1000-byte size threshold requirement.
"""
        (tmp_path / "partial-domain.md").write_text(content)
        make_index_md(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        anomaly_types = [a.type for a in report.anomalies]
        assert "incomplete_domain" in anomaly_types

        incomplete = next(a for a in report.anomalies if a.type == "incomplete_domain")
        assert incomplete.domain == "partial-domain"


# ─────────────────────────────────────────────────────────────────────────────
# AC9: Healthy state returns no false positives
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectHealthyNoFalsePositives:
    """AC9: A well-formed output directory reports clean health."""

    def test_detect_healthy_no_false_positives(self, tmp_path):
        """Well-formed output directory returns status='healthy' with empty anomalies."""
        make_healthy_output_dir(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        assert report.status == "healthy"
        assert len(report.anomalies) == 0

    def test_detect_healthy_multiple_domains(self, tmp_path):
        """Multiple well-formed domains all pass without false positives."""
        make_healthy_output_dir(tmp_path, domain_names=["domain-a", "domain-b", "domain-c"])

        detector = _get_detector()
        report = detector.detect(tmp_path)

        assert report.status == "healthy"
        assert len(report.anomalies) == 0

    def test_detect_empty_domains_json_and_no_files_is_valid(self, tmp_path):
        """Empty _domains.json with no domain files is edge case - should be healthy (no mismatch)."""
        make_domains_json(tmp_path, [])
        # No domain .md files
        make_index_md(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        # Empty domain list with no files is a valid (if empty) state
        # No domain_count_mismatch since both are 0
        mismatch_anomalies = [a for a in report.anomalies if a.type == "domain_count_mismatch"]
        assert len(mismatch_anomalies) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Additional synthetic tests (edge cases and multi-anomaly detection)
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectEmptyOutputDir:
    """Nonexistent or empty directory returns critical status."""

    def test_detect_nonexistent_dir_returns_critical(self, tmp_path):
        """Non-existent output directory returns critical status."""
        nonexistent = tmp_path / "does-not-exist"

        detector = _get_detector()
        report = detector.detect(nonexistent)

        assert report.status == "critical"

    def test_detect_empty_dir_returns_critical(self, tmp_path):
        """Empty output directory (no _domains.json) returns critical status."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        detector = _get_detector()
        report = detector.detect(empty_dir)

        assert report.status == "critical"


class TestDetectMultipleAnomalies:
    """Multiple anomaly types detected simultaneously."""

    def test_detect_multiple_anomalies(self, tmp_path):
        """Multiple anomaly types simultaneously are all reported."""
        make_domains_json(
            tmp_path,
            [
                {"name": "domain-a", "description": "d", "participating_repos": []},
                {"name": "domain-b", "description": "d", "participating_repos": []},
                {"name": "domain-c", "description": "d", "participating_repos": []},
            ],
        )
        # domain-a: zero char (critical)
        (tmp_path / "domain-a.md").write_text("")
        # domain-b: OK
        make_domain_file(tmp_path, "domain-b")
        # domain-c: missing (critical)
        # orphan: not in JSON
        (tmp_path / "orphan.md").write_text("I am an orphan")
        # No _index.md (needs_repair)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        # Status should be critical (highest severity present)
        assert report.status == "critical"

        anomaly_types = [a.type for a in report.anomalies]
        # All anomalies detected
        assert "zero_char_domain" in anomaly_types
        assert "missing_domain_file" in anomaly_types
        assert "orphan_domain_file" in anomaly_types
        assert "missing_index" in anomaly_types

    def test_critical_overrides_needs_repair(self, tmp_path):
        """When both critical and needs_repair anomalies present, status is critical."""
        make_domains_json(
            tmp_path,
            [
                {"name": "critical-domain", "description": "d", "participating_repos": []},
                {"name": "small-domain", "description": "d", "participating_repos": []},
            ],
        )
        # critical: zero char
        (tmp_path / "critical-domain.md").write_text("")
        # needs_repair: undersized
        make_domain_file(tmp_path, "small-domain", size=500)
        make_index_md(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        # Critical should override needs_repair
        assert report.status == "critical"


class TestDetectMissingDomainsJson:
    """Graceful handling when _domains.json is absent."""

    def test_detect_missing_domains_json_treats_as_empty(self, tmp_path):
        """No _domains.json treats domain list as empty (graceful)."""
        # Create some domain files with no _domains.json
        make_domain_file(tmp_path, "orphan-domain")
        make_index_md(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        # Any .md files without _domains.json means they are all orphans
        anomaly_types = [a.type for a in report.anomalies]
        assert "orphan_domain_file" in anomaly_types


# ─────────────────────────────────────────────────────────────────────────────
# Check 6: Repos not covered by any domain (known_repos parameter)
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectUncoveredRepos:
    """Check 6: Golden repos not assigned to any domain produce uncovered_repo anomaly."""

    def test_detect_uncovered_repo(self, tmp_path):
        """Repos in known_repos but not in any domain's participating_repos produce anomaly."""
        make_healthy_output_dir(tmp_path)
        # make_healthy_output_dir creates domains with participating_repos: [repo-alpha, repo-beta]
        known_repos = {"repo-alpha", "repo-beta", "uncovered-repo-1", "uncovered-repo-2"}

        detector = _get_detector()
        report = detector.detect(tmp_path, known_repos=known_repos)

        uncovered = [a for a in report.anomalies if a.type == "uncovered_repo"]
        assert len(uncovered) == 1
        assert "uncovered-repo-1" in uncovered[0].missing_repos
        assert "uncovered-repo-2" in uncovered[0].missing_repos
        assert report.status == "needs_repair"

    def test_detect_no_uncovered_repos_when_all_covered(self, tmp_path):
        """No anomaly when all known repos are covered by domains."""
        make_healthy_output_dir(tmp_path)
        known_repos = {"repo-alpha", "repo-beta"}

        detector = _get_detector()
        report = detector.detect(tmp_path, known_repos=known_repos)

        uncovered = [a for a in report.anomalies if a.type == "uncovered_repo"]
        assert len(uncovered) == 0
        assert report.status == "healthy"

    def test_detect_no_check6_when_known_repos_not_provided(self, tmp_path):
        """Check 6 is skipped when known_repos is None (backward compatibility)."""
        make_healthy_output_dir(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)  # No known_repos argument

        uncovered = [a for a in report.anomalies if a.type == "uncovered_repo"]
        assert len(uncovered) == 0
        assert report.status == "healthy"

    def test_detect_cidx_meta_excluded_from_uncovered(self, tmp_path):
        """cidx-meta is always excluded from the uncovered check (it is the meta repo itself)."""
        make_healthy_output_dir(tmp_path)
        known_repos = {"repo-alpha", "repo-beta", "cidx-meta"}

        detector = _get_detector()
        report = detector.detect(tmp_path, known_repos=known_repos)

        uncovered = [a for a in report.anomalies if a.type == "uncovered_repo"]
        assert len(uncovered) == 0
        assert report.status == "healthy"

    def test_detect_uncovered_detail_message(self, tmp_path):
        """Uncovered repo anomaly includes count in detail field."""
        make_healthy_output_dir(tmp_path)
        known_repos = {"repo-alpha", "repo-beta", "new-repo"}

        detector = _get_detector()
        report = detector.detect(tmp_path, known_repos=known_repos)

        uncovered = [a for a in report.anomalies if a.type == "uncovered_repo"]
        assert len(uncovered) == 1
        assert "1" in uncovered[0].detail  # "1 repo(s) not in any domain"

    def test_detect_uncovered_not_in_repairable_types(self, tmp_path):
        """uncovered_repo anomaly is NOT counted as repairable (requires full re-run)."""
        make_healthy_output_dir(tmp_path)
        known_repos = {"repo-alpha", "repo-beta", "new-uncovered-repo"}

        detector = _get_detector()
        report = detector.detect(tmp_path, known_repos=known_repos)

        uncovered = [a for a in report.anomalies if a.type == "uncovered_repo"]
        assert len(uncovered) == 1
        # repairable_count must NOT include uncovered_repo
        assert report.repairable_count == 0

    def test_detect_uncovered_not_critical(self, tmp_path):
        """uncovered_repo anomaly escalates to needs_repair, not critical."""
        make_healthy_output_dir(tmp_path)
        known_repos = {"repo-alpha", "repo-beta", "new-repo-1", "new-repo-2"}

        detector = _get_detector()
        report = detector.detect(tmp_path, known_repos=known_repos)

        assert report.status == "needs_repair"
        assert report.status != "critical"


class TestHealthReportStructure:
    """HealthReport has correct structure and fields."""

    def test_health_report_has_status_and_anomalies(self, tmp_path):
        """HealthReport exposes status and anomalies list."""
        make_healthy_output_dir(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        # Must have status field
        assert hasattr(report, "status")
        assert report.status in ("healthy", "needs_repair", "critical")

        # Must have anomalies list
        assert hasattr(report, "anomalies")
        assert isinstance(report.anomalies, list)

    def test_health_report_repairable_count(self, tmp_path):
        """HealthReport includes repairable_count field."""
        make_domains_json(
            tmp_path,
            [{"name": "my-domain", "description": "d", "participating_repos": []}],
        )
        (tmp_path / "my-domain.md").write_text("")  # zero char -- needs repair
        make_index_md(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        # Must have repairable_count
        assert hasattr(report, "repairable_count")
        assert report.repairable_count >= 1

    def test_anomaly_has_type_field(self, tmp_path):
        """Each Anomaly object has a type field."""
        make_domains_json(
            tmp_path,
            [{"name": "my-domain", "description": "d", "participating_repos": []}],
        )
        (tmp_path / "my-domain.md").write_text("")
        make_index_md(tmp_path)

        detector = _get_detector()
        report = detector.detect(tmp_path)

        assert len(report.anomalies) > 0
        for anomaly in report.anomalies:
            assert hasattr(anomaly, "type")
            assert isinstance(anomaly.type, str)
