"""
Tests for DepMapHealthDetector Check 7: stale participating repos (Story #342 / Bug #396).

Check 7 is the symmetric inverse of Check 6.
Check 6: known_repos - covered_repos  → repos in DB but NOT in _domains.json
Check 7: covered_repos - known_repos  → repos in _domains.json but NOT in DB

All tests follow the same pattern as test_dep_map_health_detector.py:
real filesystem, real detector, no mocking.
"""

import json
from pathlib import Path


from code_indexer.server.services.dep_map_health_detector import (
    REPAIRABLE_ANOMALY_TYPES,
    DepMapHealthDetector,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

VALID_DOMAIN_CONTENT_TEMPLATE = """\
---
name: {name}
description: A test domain
participating_repos:
{repo_lines}
last_analyzed: "2026-01-01T00:00:00Z"
---

# Domain Analysis: {name}

## Overview

This is a test domain with sufficient content to pass the size check.
It covers repository integration between participating repositories.
The domain encapsulates all cross-cutting concerns related to data access
and API boundary management. Components in this domain are responsible for
validating inputs, normalizing outputs, and enforcing service contracts.

## Repository Roles

{role_lines}

## Intra-Domain Dependencies

Repos integrate via shared REST API calls.
Evidence: client.py imports requests and calls /api/v1/items.
The dependency is unidirectional. No circular dependency exists.
Version compatibility is enforced via semver pinning.

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


def _make_domain_content(name: str, repos: list) -> str:
    repo_lines = "\n".join(f"  - {r}" for r in repos)
    role_lines = "\n".join(f"- **{r}**: Service role for testing." for r in repos)
    return VALID_DOMAIN_CONTENT_TEMPLATE.format(
        name=name, repo_lines=repo_lines, role_lines=role_lines
    )


def _setup_single_domain(
    output_dir: Path,
    domain_name: str,
    participating_repos: list,
) -> None:
    """Write _domains.json, domain .md file, and _index.md for a single domain."""
    domains = [
        {
            "name": domain_name,
            "description": f"Domain {domain_name}",
            "participating_repos": participating_repos,
        }
    ]
    (output_dir / "_domains.json").write_text(json.dumps(domains))
    (output_dir / f"{domain_name}.md").write_text(
        _make_domain_content(domain_name, participating_repos)
    )
    (output_dir / "_index.md").write_text(VALID_INDEX_CONTENT)


def _setup_multi_domain(
    output_dir: Path,
    domain_specs: list,
) -> None:
    """Write _domains.json + .md files for multiple domains. domain_specs is list of (name, repos)."""
    domains = [
        {
            "name": name,
            "description": f"Domain {name}",
            "participating_repos": repos,
        }
        for name, repos in domain_specs
    ]
    (output_dir / "_domains.json").write_text(json.dumps(domains))
    for name, repos in domain_specs:
        (output_dir / f"{name}.md").write_text(_make_domain_content(name, repos))
    (output_dir / "_index.md").write_text(VALID_INDEX_CONTENT)


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Stale repo detected when in participating_repos but not in known_repos
# ─────────────────────────────────────────────────────────────────────────────


class TestStaleRepoDetected:
    """Check 7: A repo listed in participating_repos that no longer exists as a golden repo."""

    def test_stale_repo_detected_when_participating_but_not_known(self, tmp_path):
        """
        A repo in participating_repos but not in known_repos produces
        a stale_participating_repo anomaly.
        """
        # Domain lists repo-alpha, repo-beta, AND stale-deleted-repo
        _setup_single_domain(
            tmp_path,
            domain_name="test-domain",
            participating_repos=["repo-alpha", "repo-beta", "stale-deleted-repo"],
        )
        # DB only knows repo-alpha and repo-beta (stale-deleted-repo was removed)
        known_repos = {"repo-alpha", "repo-beta"}

        detector = DepMapHealthDetector()
        report = detector.detect(tmp_path, known_repos=known_repos)

        stale_anomalies = [
            a for a in report.anomalies if a.type == "stale_participating_repo"
        ]
        assert len(stale_anomalies) == 1
        assert "stale-deleted-repo" in stale_anomalies[0].missing_repos

    def test_stale_repo_anomaly_has_detail_message(self, tmp_path):
        """Stale repo anomaly includes a detail message with the count."""
        _setup_single_domain(
            tmp_path,
            domain_name="test-domain",
            participating_repos=["repo-alpha", "stale-repo-1", "stale-repo-2"],
        )
        known_repos = {"repo-alpha"}

        detector = DepMapHealthDetector()
        report = detector.detect(tmp_path, known_repos=known_repos)

        stale_anomalies = [
            a for a in report.anomalies if a.type == "stale_participating_repo"
        ]
        assert len(stale_anomalies) == 1
        assert stale_anomalies[0].detail is not None
        assert "2" in stale_anomalies[0].detail  # 2 stale repos


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: No anomaly when all participating repos are known
# ─────────────────────────────────────────────────────────────────────────────


class TestNoAnomalyWhenAllReposKnown:
    """Check 7: Clean state produces no stale_participating_repo anomaly."""

    def test_no_anomaly_when_all_participating_repos_are_known(self, tmp_path):
        """
        When every repo in participating_repos exists in known_repos,
        no stale_participating_repo anomaly is produced.
        """
        _setup_single_domain(
            tmp_path,
            domain_name="test-domain",
            participating_repos=["repo-alpha", "repo-beta"],
        )
        # known_repos exactly matches participating_repos
        known_repos = {"repo-alpha", "repo-beta"}

        detector = DepMapHealthDetector()
        report = detector.detect(tmp_path, known_repos=known_repos)

        stale_anomalies = [
            a for a in report.anomalies if a.type == "stale_participating_repo"
        ]
        assert len(stale_anomalies) == 0

    def test_no_anomaly_when_known_repos_is_superset(self, tmp_path):
        """
        known_repos being a superset of participating_repos is fine (no stale anomaly).
        That scenario would produce an uncovered_repo anomaly (Check 6), not a stale one.
        """
        _setup_single_domain(
            tmp_path,
            domain_name="test-domain",
            participating_repos=["repo-alpha"],
        )
        # known_repos has more repos than what's covered — that's Check 6's concern
        known_repos = {"repo-alpha", "repo-beta", "repo-gamma"}

        detector = DepMapHealthDetector()
        report = detector.detect(tmp_path, known_repos=known_repos)

        stale_anomalies = [
            a for a in report.anomalies if a.type == "stale_participating_repo"
        ]
        assert len(stale_anomalies) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: cidx-meta excluded from stale check
# ─────────────────────────────────────────────────────────────────────────────


class TestCidxMetaExcluded:
    """cidx-meta in participating_repos but not in known_repos should NOT trigger Check 7."""

    def test_cidx_meta_excluded_from_stale_check(self, tmp_path):
        """
        cidx-meta listed in participating_repos but absent from known_repos
        should NOT produce a stale_participating_repo anomaly.
        """
        _setup_single_domain(
            tmp_path,
            domain_name="test-domain",
            participating_repos=["repo-alpha", "cidx-meta"],
        )
        # known_repos does NOT include cidx-meta (it is always excluded)
        known_repos = {"repo-alpha"}

        detector = DepMapHealthDetector()
        report = detector.detect(tmp_path, known_repos=known_repos)

        stale_anomalies = [
            a for a in report.anomalies if a.type == "stale_participating_repo"
        ]
        assert len(stale_anomalies) == 0

    def test_cidx_meta_excluded_but_other_stale_still_detected(self, tmp_path):
        """
        cidx-meta is excluded but other stale repos are still detected.
        """
        _setup_single_domain(
            tmp_path,
            domain_name="test-domain",
            participating_repos=["repo-alpha", "cidx-meta", "really-stale-repo"],
        )
        known_repos = {"repo-alpha"}

        detector = DepMapHealthDetector()
        report = detector.detect(tmp_path, known_repos=known_repos)

        stale_anomalies = [
            a for a in report.anomalies if a.type == "stale_participating_repo"
        ]
        assert len(stale_anomalies) == 1
        assert "really-stale-repo" in stale_anomalies[0].missing_repos
        # cidx-meta must NOT be in the stale list
        assert "cidx-meta" not in stale_anomalies[0].missing_repos


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Same stale repo across multiple domains appears once in missing_repos
# ─────────────────────────────────────────────────────────────────────────────


class TestStaleRepoAcrossMultipleDomains:
    """Same stale repo appearing in multiple domains should appear once in missing_repos."""

    def test_stale_repos_across_multiple_domains(self, tmp_path):
        """
        When the same stale repo appears in multiple domains' participating_repos,
        it should appear exactly once in the anomaly's missing_repos list.
        """
        _setup_multi_domain(
            tmp_path,
            domain_specs=[
                ("domain-a", ["repo-alpha", "stale-repo"]),
                ("domain-b", ["repo-beta", "stale-repo"]),
            ],
        )
        # stale-repo appears in both domains but is not in known_repos
        known_repos = {"repo-alpha", "repo-beta"}

        detector = DepMapHealthDetector()
        report = detector.detect(tmp_path, known_repos=known_repos)

        stale_anomalies = [
            a for a in report.anomalies if a.type == "stale_participating_repo"
        ]
        assert len(stale_anomalies) == 1
        # stale-repo should appear exactly once (de-duplicated)
        stale_list = stale_anomalies[0].missing_repos
        assert stale_list.count("stale-repo") == 1

    def test_multiple_distinct_stale_repos_all_reported(self, tmp_path):
        """Multiple distinct stale repos from different domains are all reported."""
        _setup_multi_domain(
            tmp_path,
            domain_specs=[
                ("domain-a", ["repo-alpha", "stale-from-a"]),
                ("domain-b", ["repo-beta", "stale-from-b"]),
            ],
        )
        known_repos = {"repo-alpha", "repo-beta"}

        detector = DepMapHealthDetector()
        report = detector.detect(tmp_path, known_repos=known_repos)

        stale_anomalies = [
            a for a in report.anomalies if a.type == "stale_participating_repo"
        ]
        assert len(stale_anomalies) == 1
        stale_list = stale_anomalies[0].missing_repos
        assert "stale-from-a" in stale_list
        assert "stale-from-b" in stale_list


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Check 7 skipped when known_repos is None
# ─────────────────────────────────────────────────────────────────────────────


class TestCheckSkippedWhenKnownReposNone:
    """Check 7 must be skipped when known_repos=None (backward compatibility)."""

    def test_stale_repo_check_skipped_when_known_repos_none(self, tmp_path):
        """
        When known_repos is not provided (None), Check 7 must not run.
        Even if participating_repos contains repos that would be stale,
        no stale_participating_repo anomaly is produced.
        """
        _setup_single_domain(
            tmp_path,
            domain_name="test-domain",
            participating_repos=["repo-alpha", "ghost-repo-1", "ghost-repo-2"],
        )
        # No known_repos argument → both Check 6 and Check 7 are skipped
        detector = DepMapHealthDetector()
        report = detector.detect(tmp_path)  # known_repos defaults to None

        stale_anomalies = [
            a for a in report.anomalies if a.type == "stale_participating_repo"
        ]
        assert len(stale_anomalies) == 0

    def test_check7_skipped_when_known_repos_explicitly_none(self, tmp_path):
        """Explicitly passing known_repos=None also skips Check 7."""
        _setup_single_domain(
            tmp_path,
            domain_name="test-domain",
            participating_repos=["repo-alpha", "ghost-repo"],
        )
        detector = DepMapHealthDetector()
        report = detector.detect(tmp_path, known_repos=None)

        stale_anomalies = [
            a for a in report.anomalies if a.type == "stale_participating_repo"
        ]
        assert len(stale_anomalies) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: stale_participating_repo is in REPAIRABLE_ANOMALY_TYPES
# ─────────────────────────────────────────────────────────────────────────────


class TestStaleParticipatingRepoIsRepairable:
    """stale_participating_repo must be in REPAIRABLE_ANOMALY_TYPES."""

    def test_stale_participating_repo_in_repairable_types(self, tmp_path):
        """
        Verify that 'stale_participating_repo' IS in REPAIRABLE_ANOMALY_TYPES.
        Story #717 added Phase 1.5 repair for stale repo cleanup.
        """
        assert "stale_participating_repo" in REPAIRABLE_ANOMALY_TYPES

    def test_stale_repo_not_counted_as_repairable(self, tmp_path):
        """When a stale repo anomaly is detected, repairable_count is NOT incremented."""
        _setup_single_domain(
            tmp_path,
            domain_name="test-domain",
            participating_repos=["repo-alpha", "stale-gone-repo"],
        )
        known_repos = {"repo-alpha"}

        detector = DepMapHealthDetector()
        report = detector.detect(tmp_path, known_repos=known_repos)

        stale_anomalies = [
            a for a in report.anomalies if a.type == "stale_participating_repo"
        ]
        assert len(stale_anomalies) == 1
        assert report.repairable_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: stale_participating_repo triggers needs_repair status (not critical)
# ─────────────────────────────────────────────────────────────────────────────


class TestStaleParticipatingRepoTriggersNeedsRepairNotCritical:
    """stale_participating_repo must escalate to needs_repair, NOT critical."""

    def test_stale_participating_repo_triggers_needs_repair_status(self, tmp_path):
        """
        A stale_participating_repo anomaly should produce needs_repair status,
        not critical. Data inconsistency, not a missing critical file.
        """
        _setup_single_domain(
            tmp_path,
            domain_name="test-domain",
            participating_repos=["repo-alpha", "stale-repo"],
        )
        known_repos = {"repo-alpha"}

        detector = DepMapHealthDetector()
        report = detector.detect(tmp_path, known_repos=known_repos)

        assert report.status == "needs_repair"
        assert report.status != "critical"

    def test_stale_participating_repo_not_in_critical_anomaly_types(self, tmp_path):
        """Verify 'stale_participating_repo' is NOT in CRITICAL_ANOMALY_TYPES."""
        from code_indexer.server.services.dep_map_health_detector import (
            CRITICAL_ANOMALY_TYPES,
        )

        assert "stale_participating_repo" not in CRITICAL_ANOMALY_TYPES
