"""
Tests for DepMapHealthDetector Check 9: frontmatter/JSON mismatch (Story #688).

Check 9 detects when a domain .md file's YAML frontmatter participating_repos
does not match the corresponding _domains.json participating_repos entry.

Comparison rules:
  - Set-based (order-insensitive, duplicate-insensitive)
  - Case-sensitive
  - None/null/[] all treated as empty set
  - Malformed domains (Check 5 territory) are skipped
  - Missing .md files are skipped (Check 1 handles)
  - frontmatter_json_mismatch IS in REPAIRABLE_ANOMALY_TYPES

All tests: real filesystem (tmp_path), real services, no mocking.
"""

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from code_indexer.server.services.dep_map_health_detector import (
    REPAIRABLE_ANOMALY_TYPES,
    Anomaly,
    DepMapHealthDetector,
    HealthReport,
)

# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

VALID_DOMAIN_BODY = (
    "\n# Domain Analysis: {name}\n\n"
    "## Overview\n\n"
    "This is a test domain with sufficient content to pass the size check.\n"
    "It covers repository integration between participating repositories.\n"
    "The domain encapsulates all cross-cutting concerns related to data access\n"
    "and API boundary management.\n\n"
    "## Repository Roles\n\n"
    "{role_lines}\n\n"
    "## Intra-Domain Dependencies\n\n"
    "Repos integrate via shared REST API calls.\n"
    "The dependency is unidirectional. No circular dependency exists.\n\n"
    "## Cross-Domain Connections\n\n"
    "No verified cross-domain dependencies.\n"
)


def _make_domain_md(
    output_dir: Path,
    name: str,
    frontmatter_repos: List[str],
    extra_frontmatter: str = "",
) -> Path:
    """Write a domain .md file with YAML frontmatter and required sections."""
    repo_lines = "\n".join(f"  - {r}" for r in frontmatter_repos)
    role_lines = (
        "\n".join(f"- **{r}**: Service role." for r in frontmatter_repos)
        or "- No roles."
    )
    body = VALID_DOMAIN_BODY.format(name=name, role_lines=role_lines)
    fm_repos_block = (
        f"participating_repos:\n{repo_lines}\n"
        if frontmatter_repos
        else "participating_repos: []\n"
    )
    content = (
        f"---\n"
        f"name: {name}\n"
        f"description: A test domain for {name}\n"
        f"{fm_repos_block}"
        f"{extra_frontmatter}"
        f'last_analyzed: "2026-01-01T00:00:00Z"\n'
        f"---" + body
    )
    path = output_dir / f"{name}.md"
    path.write_text(content)
    return path


def _make_domains_json(output_dir: Path, domains: List[Dict[str, Any]]) -> None:
    """Write _domains.json with the given domain entries."""
    (output_dir / "_domains.json").write_text(json.dumps(domains, indent=2))


def _make_index_md(output_dir: Path) -> None:
    """Write a minimal valid _index.md."""
    (output_dir / "_index.md").write_text(
        "---\nschema_version: 1.0\n---\n\n"
        "# Dependency Map Index\n\n"
        "## Domain Catalog\n\n| Domain | Description | Repo Count |\n|---|---|---|\n\n"
        "## Repo-to-Domain Matrix\n\n| Repository | Domain |\n|---|---|\n\n"
        "## Cross-Domain Dependencies\n\n_No cross-domain dependencies detected._\n"
    )


def _detector() -> DepMapHealthDetector:
    return DepMapHealthDetector()


def _mismatch_anomalies(report: HealthReport) -> List[Anomaly]:
    """Return frontmatter_json_mismatch anomalies from a health report."""
    return [a for a in report.anomalies if a.type == "frontmatter_json_mismatch"]


def _setup_single_domain(
    output_dir: Path,
    name: str,
    frontmatter_repos: List[str],
    json_repos: Any,
    extra_frontmatter: str = "",
) -> None:
    """Write a single-domain test fixture with given frontmatter and JSON repos."""
    _make_domain_md(
        output_dir, name, frontmatter_repos, extra_frontmatter=extra_frontmatter
    )
    _make_domains_json(
        output_dir,
        [{"name": name, "description": "A domain", "participating_repos": json_repos}],
    )
    _make_index_md(output_dir)


def _detect_no_mismatch(output_dir: Path) -> bool:
    """Return True when no frontmatter_json_mismatch anomalies are present."""
    return len(_mismatch_anomalies(_detector().detect(output_dir))) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: No anomaly when frontmatter and JSON agree
# ─────────────────────────────────────────────────────────────────────────────


class TestCheck9NoAnomalyWhenAgreement:
    """Check 9: No anomaly when frontmatter and JSON participating_repos match."""

    @pytest.mark.parametrize(
        "fm_repos,json_repos",
        [
            (["repo-alpha", "repo-beta"], ["repo-alpha", "repo-beta"]),  # identical
            (
                ["repo-beta", "repo-alpha"],
                ["repo-alpha", "repo-beta"],
            ),  # different order
            (
                ["repo-alpha", "repo-alpha", "repo-beta"],
                ["repo-alpha", "repo-beta"],
            ),  # dedup
        ],
        ids=["identical", "different-order", "duplicates-dedup"],
    )
    def test_no_anomaly_when_sets_equal(self, tmp_path, fm_repos, json_repos):
        """Set-equal repos (identical, reordered, or deduplicated) produce no anomaly."""
        _setup_single_domain(tmp_path, "test-domain", fm_repos, json_repos)
        assert _detect_no_mismatch(tmp_path)

    def test_no_anomaly_when_both_empty(self, tmp_path):
        """Both empty lists normalize to empty set: no anomaly."""
        _setup_single_domain(
            tmp_path, "test-domain", frontmatter_repos=[], json_repos=[]
        )
        assert _detect_no_mismatch(tmp_path)

    @pytest.mark.parametrize(
        "json_entry",
        [
            {"name": "test-domain", "description": "D"},  # missing key
            {
                "name": "test-domain",
                "description": "D",
                "participating_repos": None,
            },  # null
        ],
        ids=["missing-key", "null-value"],
    )
    def test_no_anomaly_when_json_null_or_missing_and_fm_empty(
        self, tmp_path, json_entry
    ):
        """null and missing participating_repos in JSON both normalize to empty set."""
        _make_domain_md(tmp_path, "test-domain", frontmatter_repos=[])
        _make_domains_json(tmp_path, [json_entry])
        _make_index_md(tmp_path)
        assert _detect_no_mismatch(tmp_path)


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Anomaly when mismatch detected
# ─────────────────────────────────────────────────────────────────────────────


class TestCheck9AnomalyOnMismatch:
    """Check 9: Anomaly emitted when frontmatter and JSON participating_repos differ."""

    @pytest.mark.parametrize(
        "fm_repos,json_repos,expected_in_detail",
        [
            # frontmatter has extra repo → frontmatter_only reported
            (
                ["repo-alpha", "fm-only"],
                ["repo-alpha"],
                ["fm-only", "frontmatter_only"],
            ),
            # JSON has extra repo → json_only reported
            (["repo-alpha"], ["repo-alpha", "json-only"], ["json-only", "json_only"]),
            # symmetric difference → both sides reported
            (
                ["repo-alpha", "fm-only"],
                ["repo-alpha", "json-only"],
                ["fm-only", "json-only", "frontmatter_only", "json_only"],
            ),
        ],
        ids=["fm-extra", "json-extra", "symmetric-diff"],
    )
    def test_anomaly_detail_reflects_mismatch(
        self, tmp_path, fm_repos, json_repos, expected_in_detail
    ):
        """Mismatch detail string lists the differing repos on each side."""
        _setup_single_domain(tmp_path, "test-domain", fm_repos, json_repos)
        anomalies = _mismatch_anomalies(_detector().detect(tmp_path))
        assert len(anomalies) == 1
        for token in expected_in_detail:
            assert token in anomalies[0].detail

    def test_anomaly_domain_field_matches_name(self, tmp_path):
        """Anomaly domain field must equal the domain name."""
        _setup_single_domain(
            tmp_path, "my-domain", frontmatter_repos=["a"], json_repos=["b"]
        )
        anomalies = _mismatch_anomalies(_detector().detect(tmp_path))
        assert len(anomalies) == 1
        assert anomalies[0].domain == "my-domain"

    def test_case_sensitive_comparison(self, tmp_path):
        """Repo-Alpha and repo-alpha are different repos: mismatch anomaly emitted."""
        _setup_single_domain(
            tmp_path,
            "test-domain",
            frontmatter_repos=["Repo-Alpha"],
            json_repos=["repo-alpha"],
        )
        assert len(_mismatch_anomalies(_detector().detect(tmp_path))) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Skipped scenarios (Check 1 and Check 5 territory)
# ─────────────────────────────────────────────────────────────────────────────


class TestCheck9SkippedScenarios:
    """Check 9: Must skip malformed domains (no frontmatter) and missing .md files."""

    def test_no_check9_for_missing_md_file(self, tmp_path):
        """Missing .md file is Check 1 territory: Check 9 must skip it."""
        _make_domains_json(
            tmp_path,
            [
                {
                    "name": "ghost-domain",
                    "description": "D",
                    "participating_repos": ["repo-a"],
                }
            ],
        )
        _make_index_md(tmp_path)
        assert _detect_no_mismatch(tmp_path)

    def test_no_check9_for_domain_without_frontmatter(self, tmp_path):
        """Domain without YAML frontmatter is Check 5 territory: Check 9 must skip it."""
        large_body = "x" * 1100
        (tmp_path / "malformed-domain.md").write_text(
            f"# Malformed\n\n## Overview\n\n{large_body}\n\n## Repository Roles\n\nsome role"
        )
        _make_domains_json(
            tmp_path,
            [
                {
                    "name": "malformed-domain",
                    "description": "D",
                    "participating_repos": ["repo-a"],
                }
            ],
        )
        _make_index_md(tmp_path)
        assert _detect_no_mismatch(tmp_path)

    def test_no_check9_when_fm_no_repos_key_and_json_empty(self, tmp_path):
        """Frontmatter without participating_repos key + empty JSON: both empty, no anomaly."""
        content = (
            "---\n"
            "name: test-domain\n"
            "description: A domain\n"
            'last_analyzed: "2026-01-01T00:00:00Z"\n'
            "---\n\n"
            "# Domain\n\n## Overview\n\nContent.\n\n## Repository Roles\n\nRole.\n"
        )
        (tmp_path / "test-domain.md").write_text(content)
        _make_domains_json(
            tmp_path,
            [{"name": "test-domain", "description": "D", "participating_repos": []}],
        )
        _make_index_md(tmp_path)
        assert _detect_no_mismatch(tmp_path)


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: frontmatter_json_mismatch repairability semantics
# ─────────────────────────────────────────────────────────────────────────────


class TestCheck9Repairability:
    """frontmatter_json_mismatch must be in REPAIRABLE_ANOMALY_TYPES and counted."""

    def test_frontmatter_json_mismatch_in_repairable_types(self):
        """Verify frontmatter_json_mismatch is in REPAIRABLE_ANOMALY_TYPES."""
        assert "frontmatter_json_mismatch" in REPAIRABLE_ANOMALY_TYPES

    def test_mismatch_counted_as_repairable(self, tmp_path):
        """repairable_count incremented for frontmatter_json_mismatch anomaly."""
        _setup_single_domain(
            tmp_path,
            "test-domain",
            frontmatter_repos=["fm-only"],
            json_repos=["json-only"],
        )
        report = _detector().detect(tmp_path)
        assert len(_mismatch_anomalies(report)) == 1
        assert report.repairable_count >= 1

    def test_mismatch_status_is_needs_repair_not_critical(self, tmp_path):
        """frontmatter_json_mismatch must escalate to needs_repair, not critical."""
        _setup_single_domain(
            tmp_path,
            "test-domain",
            frontmatter_repos=["fm-only"],
            json_repos=["json-only"],
        )
        report = _detector().detect(tmp_path)
        assert report.status == "needs_repair"
