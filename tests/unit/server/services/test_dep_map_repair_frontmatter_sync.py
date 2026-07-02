"""
Tests for Phase 3.5 frontmatter sync and Phase 1 routing extension (Story #688).

Phase 3.5 extension: When a `frontmatter_json_mismatch` anomaly exists, sync
the .md file's frontmatter participating_repos to match _domains.json.

Phase 1 routing extension: `empty_json_metadata` anomalies must also trigger
domain_analyzer (Claude CLI re-analysis), even though the type is not in
REPAIRABLE_ANOMALY_TYPES.

All tests: real filesystem (tmp_path), real services, no mocking of core logic.
domain_analyzer is a test double because it is external and expensive.
"""

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from code_indexer.server.services.dep_map_health_detector import DepMapHealthDetector


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
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
    "Repos integrate via shared REST API calls.\n\n"
    "## Cross-Domain Connections\n\n"
    "No verified cross-domain dependencies.\n"
)

# Extra body padding to push total content over the 1000-char size threshold.
# Use this in body_override when the test requires a non-undersized domain.
LARGE_BODY_PADDING = "\n\n## Extended Analysis\n\n" + (
    "This domain is analyzed in depth.\n" * 30
)


def _make_domain_md(
    output_dir: Path,
    name: str,
    frontmatter_repos: List[str],
    extra_frontmatter: str = "",
    body_override: str = "",
) -> Path:
    """Write a domain .md file with YAML frontmatter and required sections."""
    repo_lines = "\n".join(f"  - {r}" for r in frontmatter_repos)
    role_lines = (
        "\n".join(f"- **{r}**: Service role." for r in frontmatter_repos)
        or "- No roles."
    )
    body = body_override or VALID_DOMAIN_BODY.format(name=name, role_lines=role_lines)
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


def _repair_executor(domain_analyzer=None):
    from code_indexer.server.services.dep_map_repair_executor import (
        DepMapRepairExecutor,
    )
    from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator

    return DepMapRepairExecutor(
        health_detector=_detector(),
        index_regenerator=IndexRegenerator(),
        domain_analyzer=domain_analyzer,
    )


def _setup_mismatch(
    output_dir: Path,
    name: str,
    frontmatter_repos: List[str],
    json_repos: List[str],
    extra_frontmatter: str = "",
    body_override: str = "",
) -> None:
    """Write a single-domain fixture with a frontmatter/JSON repos mismatch."""
    _make_domain_md(
        output_dir,
        name,
        frontmatter_repos,
        extra_frontmatter=extra_frontmatter,
        body_override=body_override,
    )
    _make_domains_json(
        output_dir,
        [{"name": name, "description": "A domain", "participating_repos": json_repos}],
    )
    _make_index_md(output_dir)


def _setup_empty_json_metadata(
    output_dir: Path,
    name: str,
    repos: List[str],
) -> None:
    """
    Write a single-domain fixture that triggers `empty_json_metadata` anomaly.

    Preconditions:
      - .md file exists with ## Overview section (valid content)
      - _domains.json entry has empty description and evidence
      - participating_repos in frontmatter and JSON are in sync (no mismatch)
    """
    _make_domain_md(output_dir, name, frontmatter_repos=repos)
    _make_domains_json(
        output_dir,
        [
            {
                "name": name,
                "description": "",
                "evidence": "",
                "participating_repos": repos,
            }
        ],
    )
    _make_index_md(output_dir)


def _frontmatter_repos_from_file(md_file: Path) -> List[str]:
    """Parse frontmatter and return sorted participating_repos list (or [] if absent)."""
    from code_indexer.server.services.dep_map_file_utils import parse_yaml_frontmatter

    content = md_file.read_text()
    fm = parse_yaml_frontmatter(content)
    if fm is None:
        return []
    raw = fm.get("participating_repos")
    return sorted(raw) if isinstance(raw, list) else []


def _extract_body_after_frontmatter(content: str) -> str:
    """
    Return the raw string after the closing --- frontmatter delimiter.

    Uses string slicing to preserve the exact byte sequence without any
    newline normalization. Returns the full content if no frontmatter found.
    """
    if not content.startswith("---"):
        return content
    # Find the closing --- (must appear after the opening line)
    close_marker = "\n---"
    close_idx = content.find(close_marker, 3)
    if close_idx == -1:
        return content
    # Return everything after the closing --- line (including trailing newline)
    return content[close_idx + len(close_marker) :]


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Phase 3.5 frontmatter sync
# ─────────────────────────────────────────────────────────────────────────────


class TestPhase35FrontmatterSync:
    """Phase 3.5 must sync frontmatter participating_repos to match _domains.json."""

    @pytest.mark.parametrize(
        "fm_repos,json_repos",
        [
            (["repo-alpha", "fm-only"], ["repo-alpha", "json-only"]),
            (["fm-only"], ["json-only"]),
        ],
        ids=["partial-change", "full-replace"],
    )
    def test_frontmatter_repos_synced_exactly_to_json(
        self, tmp_path, fm_repos, json_repos
    ):
        """After repair, sorted frontmatter repos exactly equal sorted json_repos."""
        _setup_mismatch(tmp_path, "test-domain", fm_repos, json_repos)
        report = _detector().detect(tmp_path)
        _repair_executor().execute(tmp_path, report)

        result_repos = _frontmatter_repos_from_file(tmp_path / "test-domain.md")
        assert result_repos == sorted(json_repos), (
            f"Frontmatter repos must match JSON repos. "
            f"Expected {sorted(json_repos)!r}, got {result_repos!r}"
        )

    def test_non_repos_frontmatter_and_body_preserved(self, tmp_path):
        """Repair must not alter non-repos frontmatter keys or raw markdown body."""
        custom_body = (
            "\n\n# My Domain\n\n## Overview\n\nUnique body here.\n"
            "Line2.\n\n## Repository Roles\n\nRole.\n"
        )
        _setup_mismatch(
            tmp_path,
            "test-domain",
            frontmatter_repos=["fm-only"],
            json_repos=["json-only"],
            extra_frontmatter="custom_key: custom_value\n",
            body_override=custom_body,
        )
        original_content = (tmp_path / "test-domain.md").read_text()
        original_body = _extract_body_after_frontmatter(original_content)

        report = _detector().detect(tmp_path)
        _repair_executor().execute(tmp_path, report)

        content_after = (tmp_path / "test-domain.md").read_text()
        from code_indexer.server.services.dep_map_file_utils import (
            parse_yaml_frontmatter,
        )

        fm_after = parse_yaml_frontmatter(content_after)
        assert fm_after is not None
        assert fm_after.get("custom_key") == "custom_value"
        assert fm_after.get("name") == "test-domain"
        assert "last_analyzed" in fm_after
        assert _extract_body_after_frontmatter(content_after) == original_body

    def test_sync_idempotent_and_resolves_anomaly(self, tmp_path):
        """Two repair runs produce identical state; mismatch anomaly is gone after first."""
        _setup_mismatch(tmp_path, "test-domain", ["fm-only"], ["json-only"])

        report1 = _detector().detect(tmp_path)
        _repair_executor().execute(tmp_path, report1)
        state1 = (tmp_path / "test-domain.md").read_text()

        report2 = _detector().detect(tmp_path)
        mismatch_after = [
            a for a in report2.anomalies if a.type == "frontmatter_json_mismatch"
        ]
        assert len(mismatch_after) == 0

        _repair_executor().execute(tmp_path, report2)
        state2 = (tmp_path / "test-domain.md").read_text()
        assert state1 == state2


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Phase 1 routing for empty_json_metadata
# ─────────────────────────────────────────────────────────────────────────────


class TestPhase1RoutingForEmptyJsonMetadata:
    """Phase 1 must trigger domain_analyzer for empty_json_metadata anomalies."""

    def test_analyzer_called_for_empty_json_metadata_domain(self, tmp_path):
        """When domain has empty_json_metadata, Phase 1 calls domain_analyzer for it."""
        name = "test-domain"
        repos = ["repo-alpha", "repo-beta"]
        _setup_empty_json_metadata(tmp_path, name, repos)

        called_domains: List[str] = []

        def fake_analyzer(output_dir, domain_info, domain_list, repo_list):
            called_domains.append(domain_info.get("name"))
            _make_domain_md(output_dir, domain_info["name"], frontmatter_repos=repos)
            return True

        report = _detector().detect(tmp_path)
        empty_meta = [a for a in report.anomalies if a.type == "empty_json_metadata"]
        assert len(empty_meta) == 1, (
            "Prerequisite: empty_json_metadata must be detected"
        )

        _repair_executor(domain_analyzer=fake_analyzer).execute(tmp_path, report)

        assert name in called_domains, (
            f"Phase 1 must call domain_analyzer for empty_json_metadata. "
            f"Called for: {called_domains}"
        )

    def test_analyzer_not_called_when_no_empty_json_metadata(self, tmp_path):
        """When no empty_json_metadata anomaly exists, Phase 1 skips the domain."""
        repos = ["repo-alpha"]
        # Use LARGE_BODY_PADDING to exceed the 1000-char size threshold so
        # undersized_domain is not triggered (which would invoke Phase 1).
        large_body = (
            VALID_DOMAIN_BODY.format(
                name="clean-domain",
                role_lines="\n".join(f"- **{r}**: Service role." for r in repos),
            )
            + LARGE_BODY_PADDING
        )
        _make_domain_md(
            tmp_path, "clean-domain", frontmatter_repos=repos, body_override=large_body
        )
        _make_domains_json(
            tmp_path,
            [
                {
                    "name": "clean-domain",
                    "description": "Has desc",
                    "evidence": "Some evidence",
                    "participating_repos": repos,
                }
            ],
        )
        _make_index_md(tmp_path)

        called_domains: List[str] = []

        def fake_analyzer(output_dir, domain_info, domain_list, repo_list):
            called_domains.append(domain_info.get("name"))
            return True

        report = _detector().detect(tmp_path)
        _repair_executor(domain_analyzer=fake_analyzer).execute(tmp_path, report)

        assert "clean-domain" not in called_domains, (
            "Phase 1 must NOT call domain_analyzer when no empty_json_metadata"
        )

    def test_analyzer_called_for_both_repairable_and_empty_json_metadata(
        self, tmp_path
    ):
        """Phase 1 calls analyzer for REPAIRABLE_ANOMALY_TYPES AND empty_json_metadata."""
        repos = ["repo-alpha"]
        # Domain A: undersized (REPAIRABLE) - write tiny content
        (tmp_path / "tiny-domain.md").write_text("# Tiny\n\nSmall content.\n")
        # Domain B: empty_json_metadata
        _make_domain_md(tmp_path, "drift-domain", frontmatter_repos=repos)
        _make_domains_json(
            tmp_path,
            [
                {
                    "name": "tiny-domain",
                    "description": "D",
                    "participating_repos": repos,
                },
                {
                    "name": "drift-domain",
                    "description": "",
                    "evidence": "",
                    "participating_repos": repos,
                },
            ],
        )
        _make_index_md(tmp_path)

        called_domains: List[str] = []

        def fake_analyzer(output_dir, domain_info, domain_list, repo_list):
            called_domains.append(domain_info.get("name"))
            _make_domain_md(output_dir, domain_info["name"], frontmatter_repos=repos)
            return True

        report = _detector().detect(tmp_path)
        _repair_executor(domain_analyzer=fake_analyzer).execute(tmp_path, report)

        assert "tiny-domain" in called_domains, (
            "Phase 1 must repair undersized (REPAIRABLE) domains"
        )
        assert "drift-domain" in called_domains, (
            "Phase 1 must also repair empty_json_metadata domains"
        )
