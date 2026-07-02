"""
Unit tests for DepMapMCPParser.find_consumers — Story #855.

Tests: exhaustive list across domains, malformed YAML resilience,
truncated markdown table resilience.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

import pytest

if TYPE_CHECKING:
    from code_indexer.server.services.dep_map_mcp_parser import DepMapMCPParser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_parser(path: Path) -> "DepMapMCPParser":  # type: ignore[name-defined]
    from code_indexer.server.services.dep_map_mcp_parser import DepMapMCPParser

    return DepMapMCPParser(path)


def _write_domains_json(dep_map_dir: Path, domains: List[Dict]) -> None:
    (dep_map_dir / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")


def _write_domain_md(
    dep_map_dir: Path,
    domain_name: str,
    incoming_rows: List[Dict[str, str]],
    participating_repos: Optional[List[str]] = None,
) -> None:
    if participating_repos is None:
        participating_repos = []
    repos_list = "\n".join(f"  - {r}" for r in participating_repos)
    frontmatter = f"---\nname: {domain_name}\nparticipating_repos:\n{repos_list}\n---\n"
    body = (
        f"# Domain Analysis: {domain_name}\n\n"
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
    )
    for row in incoming_rows:
        body += (
            f"| {row['external_repo']} | {row['depends_on']} | "
            f"{row['source_domain']} | {row['dep_type']} | "
            f"{row['why']} | {row['evidence']} |\n"
        )
    (dep_map_dir / f"{domain_name}.md").write_text(frontmatter + body, encoding="utf-8")


def _incoming_row(
    external_repo: str,
    depends_on: str,
    source_domain: str,
    dep_type: str = "Code-level",
    why: str = "why",
    evidence: str = "evidence",
) -> Dict[str, str]:
    return {
        "external_repo": external_repo,
        "depends_on": depends_on,
        "source_domain": source_domain,
        "dep_type": dep_type,
        "why": why,
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def dep_map_root(tmp_path: Path) -> Path:
    (tmp_path / "dependency-map").mkdir()
    return tmp_path


@pytest.fixture
def multi_domain_root(dep_map_root: Path) -> Path:
    d = dep_map_root / "dependency-map"
    domains = [
        {
            "name": "domain-alpha",
            "description": "d",
            "participating_repos": ["repo-a", "repo-x"],
        },
        {
            "name": "domain-beta",
            "description": "d",
            "participating_repos": ["repo-b", "repo-x"],
        },
        {
            "name": "domain-gamma",
            "description": "d",
            "participating_repos": ["repo-c", "repo-x"],
        },
    ]
    _write_domains_json(d, domains)
    _write_domain_md(
        d,
        "domain-alpha",
        [
            _incoming_row(
                "repo-a", "repo-x", "domain-alpha", "Code-level", "imports", "main.py"
            )
        ],
        ["repo-a", "repo-x"],
    )
    _write_domain_md(
        d,
        "domain-beta",
        [
            _incoming_row(
                "repo-b",
                "repo-x",
                "domain-beta",
                "Service integration",
                "REST",
                "client.py",
            )
        ],
        ["repo-b", "repo-x"],
    )
    _write_domain_md(
        d,
        "domain-gamma",
        [
            _incoming_row(
                "repo-c",
                "repo-x",
                "domain-gamma",
                "Data contracts",
                "schema",
                "schema.json",
            )
        ],
        ["repo-c", "repo-x"],
    )
    return dep_map_root


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestFindConsumersExhaustive:
    def test_find_consumers_returns_exhaustive_list_across_synthetic_domains(
        self, multi_domain_root: Path
    ):
        """All (domain, consuming_repo) pairs across 3 domains are returned."""
        parser = _make_parser(multi_domain_root)
        consumers, anomalies = parser.find_consumers("repo-x")

        assert anomalies == []
        assert len(consumers) == 3
        assert {c["domain"] for c in consumers} == {
            "domain-alpha",
            "domain-beta",
            "domain-gamma",
        }
        assert {c["consuming_repo"] for c in consumers} == {
            "repo-a",
            "repo-b",
            "repo-c",
        }
        for c in consumers:
            assert {"domain", "consuming_repo", "dependency_type", "evidence"} <= set(c)


class TestFindConsumersMalformedYaml:
    def test_find_consumers_handles_malformed_yaml(self, dep_map_root: Path):
        """Malformed YAML in one domain yields partial results plus one anomaly."""
        d = dep_map_root / "dependency-map"
        domains = [
            {
                "name": "domain-good",
                "description": "d",
                "participating_repos": ["consumer-good", "repo-y"],
            },
            {
                "name": "domain-bad",
                "description": "d",
                "participating_repos": ["consumer-bad", "repo-y"],
            },
        ]
        _write_domains_json(d, domains)
        _write_domain_md(
            d,
            "domain-good",
            [_incoming_row("consumer-good", "repo-y", "domain-good")],
            ["consumer-good", "repo-y"],
        )
        bad_content = "---\nname: [unclosed bracket\nbroken: :\n---\n# bad\n\n"
        (d / "domain-bad.md").write_text(bad_content, encoding="utf-8")

        consumers, anomalies = _make_parser(dep_map_root).find_consumers("repo-y")

        assert "consumer-good" in {c["consuming_repo"] for c in consumers}
        assert len(anomalies) == 1
        assert "domain-bad" in anomalies[0]["file"]
        assert isinstance(anomalies[0]["error"], str)


class TestFindConsumersTruncatedTable:
    def test_find_consumers_handles_truncated_markdown_table(self, dep_map_root: Path):
        """Header-only Incoming table yields empty consumers and no anomaly."""
        d = dep_map_root / "dependency-map"
        _write_domains_json(
            d,
            [
                {
                    "name": "dom-trunc",
                    "description": "d",
                    "participating_repos": ["some-repo", "target-repo"],
                }
            ],
        )
        fm = "---\nname: dom-trunc\nparticipating_repos:\n  - some-repo\n  - target-repo\n---\n"
        body = (
            "# Domain Analysis: dom-trunc\n\n"
            "## Cross-Domain Connections\n\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
        )
        (d / "dom-trunc.md").write_text(fm + body, encoding="utf-8")

        consumers, anomalies = _make_parser(dep_map_root).find_consumers("target-repo")

        assert consumers == []
        assert anomalies == []


class TestFindConsumersMissingPath:
    def test_find_consumers_missing_path_returns_empty_results_no_exception(
        self, tmp_path: Path
    ):
        """Non-existent dep_map_path yields ([], []) with no exception."""
        consumers, anomalies = _make_parser(tmp_path / "no-such-dir").find_consumers(
            "x"
        )

        assert consumers == []
        assert anomalies == []


class TestFindConsumersDualSourceInconsistency:
    def test_find_consumers_dual_source_inconsistency_emits_anomaly(
        self, dep_map_root: Path
    ):
        """MD table has repo-z as target but _domains.json excludes it — anomaly emitted."""
        d = dep_map_root / "dependency-map"
        _write_domains_json(
            d,
            [
                {
                    "name": "dom-incons",
                    "description": "d",
                    "participating_repos": ["other-repo"],
                }  # repo-z absent from JSON
            ],
        )
        fm = "---\nname: dom-incons\nparticipating_repos:\n  - other-repo\n---\n"
        body = (
            "# Domain Analysis: dom-incons\n\n"
            "## Cross-Domain Connections\n\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "| consumer-repo | repo-z | dom-incons | Code-level | why | evidence |\n"
        )
        (d / "dom-incons.md").write_text(fm + body, encoding="utf-8")

        consumers, anomalies = _make_parser(dep_map_root).find_consumers("repo-z")

        assert len(anomalies) >= 1
        assert "dom-incons" in str(anomalies) or "repo-z" in str(anomalies)


class TestFindConsumersEmptyRepoName:
    def test_find_consumers_empty_repo_name_returns_empty(self, dep_map_root: Path):
        """Empty repo_name yields ([], []) without parsing any files."""
        d = dep_map_root / "dependency-map"
        _write_domains_json(
            d,
            [{"name": "dom-a", "description": "d", "participating_repos": ["repo-a"]}],
        )
        _write_domain_md(
            d, "dom-a", [_incoming_row("repo-a", "repo-x", "dom-a")], ["repo-a"]
        )

        consumers, anomalies = _make_parser(dep_map_root).find_consumers("")

        assert consumers == []
        assert anomalies == []


class TestParserStubMethods:
    """Verify stub methods return correct ([], []) or (None, []) tuples."""

    @pytest.fixture
    def empty_parser(self, dep_map_root: Path) -> "DepMapMCPParser":  # type: ignore[name-defined]
        _write_domains_json(dep_map_root / "dependency-map", [])
        return _make_parser(dep_map_root)

    @pytest.mark.parametrize(
        "method,args,expected_result",
        [
            ("get_repo_domains", ("any-repo",), []),
            ("get_domain_summary", ("any-domain",), None),
            ("get_stale_domains", (0,), []),
        ],
    )
    def test_stub_returns_correct_signature(
        self,
        empty_parser: "DepMapMCPParser",  # type: ignore[name-defined]
        method: str,
        args: tuple,
        expected_result: object,
    ) -> None:
        """Each stub method returns (expected_result, []) with no I/O side-effects."""
        result, anomalies = getattr(empty_parser, method)(*args)
        assert result == expected_result
        assert anomalies == []


# ---------------------------------------------------------------------------
# S2 test helpers — split into small focused builders
# ---------------------------------------------------------------------------


def _build_frontmatter(domain_name: str, repos: List[str]) -> str:
    """Build YAML frontmatter block for a domain markdown file."""
    repos_list = "\n".join(f"  - {r}" for r in repos)
    return f"---\ndomain: {domain_name}\nparticipating_repos:\n{repos_list}\n---\n"


def _build_roles_table(roles: List[Dict[str, str]]) -> str:
    """Build the '## Repository Roles' markdown table."""
    header = "## Repository Roles\n\n| Repository | Language | Role |\n|---|---|---|\n"
    rows = "".join(
        f"| {r['repo']} | {r.get('language', 'Python')} | {r['role']} |\n"
        for r in roles
    )
    return header + rows


def _build_outgoing_table(rows: List[Dict[str, str]]) -> str:
    """Build the '### Outgoing Dependencies' markdown table."""
    header = (
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
    )
    body = "".join(
        f"| {r['this_repo']} | {r['depends_on']} | {r['target_domain']} | "
        f"{r.get('dep_type', 'Code-level')} | {r.get('why', 'why')} | "
        f"{r.get('evidence', 'evidence')} |\n"
        for r in rows
    )
    return header + body


def _build_incoming_table(rows: List[Dict[str, str]]) -> str:
    """Build the '### Incoming Dependencies' markdown table."""
    header = (
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
    )
    body = "".join(
        f"| {r['external_repo']} | {r['depends_on']} | {r['source_domain']} | "
        f"{r.get('dep_type', 'Code-level')} | {r.get('why', 'why')} | "
        f"{r.get('evidence', 'evidence')} |\n"
        for r in rows
    )
    return header + body


def _write_domain_md_full(
    dep_map_dir: Path,
    domain_name: str,
    roles: List[Dict[str, str]],
    outgoing_rows: Optional[List[Dict[str, str]]] = None,
    incoming_rows: Optional[List[Dict[str, str]]] = None,
) -> None:
    """Write a complete domain .md file with frontmatter + roles + dependency tables."""
    if outgoing_rows is None:
        outgoing_rows = []
    if incoming_rows is None:
        incoming_rows = []

    repo_names = [r["repo"] for r in roles]
    frontmatter = _build_frontmatter(domain_name, repo_names)
    roles_section = _build_roles_table(roles)
    outgoing_section = _build_outgoing_table(outgoing_rows)
    incoming_section = _build_incoming_table(incoming_rows)

    body = (
        f"# Domain Analysis: {domain_name}\n\n"
        "## Overview\n\nOverview text.\n\n"
        + roles_section
        + "\n## Cross-Domain Connections\n\n"
        + outgoing_section
        + "\n"
        + incoming_section
    )
    (dep_map_dir / f"{domain_name}.md").write_text(frontmatter + body, encoding="utf-8")


# ---------------------------------------------------------------------------
# S2 tests: get_repo_domains
# ---------------------------------------------------------------------------


class TestGetRepoDomains:
    @pytest.fixture
    def multi_domain_repo_root(self, dep_map_root: Path) -> Path:
        """repo-alpha participates in 2 domains; repo-beta in 1; repo-gamma in 2 others."""
        d = dep_map_root / "dependency-map"
        domains = [
            {
                "name": "domain-one",
                "description": "First domain",
                "participating_repos": ["repo-alpha", "repo-beta"],
            },
            {
                "name": "domain-two",
                "description": "Second domain",
                "participating_repos": ["repo-alpha", "repo-gamma"],
            },
            {
                "name": "domain-three",
                "description": "Third domain",
                "participating_repos": ["repo-gamma"],
            },
        ]
        _write_domains_json(d, domains)
        _write_domain_md_full(
            d,
            "domain-one",
            [
                {"repo": "repo-alpha", "language": "Python", "role": "Primary service"},
                {"repo": "repo-beta", "language": "Python", "role": "Consumer"},
            ],
        )
        _write_domain_md_full(
            d,
            "domain-two",
            [
                {"repo": "repo-alpha", "language": "Python", "role": "Core library"},
                {"repo": "repo-gamma", "language": "Java", "role": "Integration"},
            ],
        )
        _write_domain_md_full(
            d,
            "domain-three",
            [
                {"repo": "repo-gamma", "language": "Java", "role": "Standalone"},
            ],
        )
        return dep_map_root

    def test_get_repo_domains_returns_all_memberships(
        self, multi_domain_repo_root: Path
    ) -> None:
        """repo-alpha is in 2 domains → 2 entries with correct domain_name and role.

        This test is RED under the S1 stub (returns [] instead of 2 entries).
        """
        parser = _make_parser(multi_domain_repo_root)
        domains, anomalies = parser.get_repo_domains("repo-alpha")

        assert anomalies == []
        assert len(domains) == 2
        domain_names = {d["domain_name"] for d in domains}
        assert domain_names == {"domain-one", "domain-two"}
        for entry in domains:
            assert "domain_name" in entry
            assert "role" in entry
            assert entry["role"]  # non-empty role

        role_by_domain = {d["domain_name"]: d["role"] for d in domains}
        assert role_by_domain["domain-one"] == "Primary service"
        assert role_by_domain["domain-two"] == "Core library"

    def test_get_repo_domains_unknown_repo_returns_empty(
        self, multi_domain_repo_root: Path
    ) -> None:
        """Repo not in any domain → ([], []) with no anomalies.

        Already passes under the stub — included to guard against regression.
        """
        parser = _make_parser(multi_domain_repo_root)
        domains, anomalies = parser.get_repo_domains("nonexistent-repo")

        assert domains == []
        assert anomalies == []

    def test_get_repo_domains_handles_malformed_domain_markdown(
        self, dep_map_root: Path
    ) -> None:
        """Malformed YAML frontmatter in one domain .md → anomaly recorded, other entries preserved.

        This test is RED under the S1 stub (returns [] and [] instead of partial results + anomaly).
        """
        d = dep_map_root / "dependency-map"
        domains = [
            {
                "name": "domain-good",
                "description": "Good",
                "participating_repos": ["repo-a", "target-repo"],
            },
            {
                "name": "domain-bad",
                "description": "Bad",
                "participating_repos": ["target-repo"],
            },
        ]
        _write_domains_json(d, domains)
        _write_domain_md_full(
            d,
            "domain-good",
            [
                {"repo": "repo-a", "language": "Python", "role": "Consumer"},
                {"repo": "target-repo", "language": "Python", "role": "Provider"},
            ],
        )
        # Write malformed YAML frontmatter for domain-bad
        bad_content = "---\ndomain: [unclosed bracket\nbroken: :\n---\n# bad\n\n"
        (d / "domain-bad.md").write_text(bad_content, encoding="utf-8")

        parser = _make_parser(dep_map_root)
        domains_result, anomalies = parser.get_repo_domains("target-repo")

        # domain-good entry should still be returned
        assert any(r["domain_name"] == "domain-good" for r in domains_result)
        # An anomaly from the malformed file
        assert len(anomalies) >= 1
        assert "domain-bad" in anomalies[0]["file"]

    def test_get_repo_domains_missing_path_returns_empty_no_exception(
        self, tmp_path: Path
    ) -> None:
        """Non-existent dep_map_path → ([], []) from parser.

        Already passes under the stub — included to guard against regression.
        """
        parser = _make_parser(tmp_path / "no-such-dir")
        domains, anomalies = parser.get_repo_domains("any-repo")

        assert domains == []
        assert anomalies == []


# ---------------------------------------------------------------------------
# S2 tests: get_domain_summary
# ---------------------------------------------------------------------------


class TestGetDomainSummary:
    @pytest.fixture
    def summary_root(self, dep_map_root: Path) -> Path:
        """Set up a domain with full structure for summary parsing."""
        d = dep_map_root / "dependency-map"
        domains = [
            {
                "name": "my-domain",
                "description": "A full test domain",
                "participating_repos": ["repo-x", "repo-y"],
            }
        ]
        _write_domains_json(d, domains)
        _write_domain_md_full(
            d,
            "my-domain",
            [
                {"repo": "repo-x", "language": "Python", "role": "Core service"},
                {"repo": "repo-y", "language": "Java", "role": "Test fixture"},
            ],
            outgoing_rows=[
                {
                    "this_repo": "repo-x",
                    "depends_on": "repo-z",
                    "target_domain": "other-domain",
                    "dep_type": "Code-level",
                    "why": "imports",
                    "evidence": "main.py",
                },
                {
                    "this_repo": "repo-x",
                    "depends_on": "repo-w",
                    "target_domain": "other-domain",
                    "dep_type": "Service integration",
                    "why": "REST",
                    "evidence": "client.py",
                },
                {
                    "this_repo": "repo-y",
                    "depends_on": "repo-q",
                    "target_domain": "third-domain",
                    "dep_type": "Data contracts",
                    "why": "schema",
                    "evidence": "schema.json",
                },
            ],
        )
        return dep_map_root

    def test_get_domain_summary_returns_full_structure(
        self, summary_root: Path
    ) -> None:
        """Known domain returns summary with name, description, participating_repos,
        cross_domain_connections.

        This test is RED under the S1 stub (returns None instead of a dict).
        """
        parser = _make_parser(summary_root)
        summary, anomalies = parser.get_domain_summary("my-domain")

        assert anomalies == []
        assert summary is not None
        assert summary["name"] == "my-domain"
        assert summary["description"] == "A full test domain"

        pr = summary["participating_repos"]
        assert len(pr) == 2
        repo_names = {r["repo"] for r in pr}
        assert repo_names == {"repo-x", "repo-y"}
        role_by_repo = {r["repo"]: r["role"] for r in pr}
        assert role_by_repo["repo-x"] == "Core service"
        assert role_by_repo["repo-y"] == "Test fixture"

        cdc = summary["cross_domain_connections"]
        target_domains = {c["target_domain"] for c in cdc}
        assert "other-domain" in target_domains
        assert "third-domain" in target_domains
        count_by_domain = {c["target_domain"]: c["dependency_count"] for c in cdc}
        assert count_by_domain["other-domain"] == 2
        assert count_by_domain["third-domain"] == 1

    def test_get_domain_summary_unknown_domain_returns_null(
        self, summary_root: Path
    ) -> None:
        """Domain not in _domains.json → (None, []).

        Already passes under the stub — included to guard against regression.
        """
        parser = _make_parser(summary_root)
        summary, anomalies = parser.get_domain_summary("nonexistent-domain")

        assert summary is None
        assert anomalies == []

    def test_get_domain_summary_partial_parse_when_section_malformed(
        self, dep_map_root: Path
    ) -> None:
        """When the requested domain .md file has a corrupt cross-domain section
        (heading present but non-table text instead of pipe-delimited rows),
        get_domain_summary must:
        - Still return a summary (not None)
        - Parse frontmatter and participating_repos successfully
        - Record exactly one anomaly with 'cross_domain_connections' in error

        AC7: malformed target domain file → anomaly recorded; requested domain
        still returned if parseable.

        This test is RED until get_domain_summary calls _validate_section_has_table
        and wraps the cross-domain parse in its own try/except.
        """
        d = dep_map_root / "dependency-map"
        domains = [
            {
                "name": "target-domain",
                "description": "Target domain desc",
                "participating_repos": ["repo-a"],
            }
        ]
        _write_domains_json(d, domains)

        # Write target-domain.md with valid frontmatter + valid roles table
        # but corrupt outgoing section (non-table text where table is expected)
        frontmatter = (
            "---\nname: target-domain\ndescription: Target domain desc\n"
            "participating_repos:\n  - repo-a\n---\n"
        )
        roles_section = _build_roles_table(
            [{"repo": "repo-a", "language": "Python", "role": "Main"}]
        )
        corrupt_outgoing = (
            "### Outgoing Dependencies\n\n"
            "This section has been corrupted. No table here.\n"
            "Just garbage text instead of pipe-delimited rows.\n\n"
        )
        incoming_section = _build_incoming_table([])
        body = (
            "# Domain Analysis: target-domain\n\n"
            "## Overview\n\nOverview text.\n\n"
            + roles_section
            + "\n## Cross-Domain Connections\n\n"
            + corrupt_outgoing
            + "\n"
            + incoming_section
        )
        (d / "target-domain.md").write_text(frontmatter + body, encoding="utf-8")

        parser = _make_parser(dep_map_root)
        summary, anomalies = parser.get_domain_summary("target-domain")

        # Summary is NOT None — frontmatter and roles table parsed successfully
        assert summary is not None
        assert summary["name"] == "target-domain"
        assert summary["description"] == "Target domain desc"
        # Roles table was parseable → participating_repos populated
        assert summary["participating_repos"] == [{"repo": "repo-a", "role": "Main"}]
        # Failed cross-domain section → empty default
        assert summary["cross_domain_connections"] == []
        # Exactly one anomaly with section-specific label
        assert len(anomalies) == 1
        assert "target-domain.md" in anomalies[0]["file"]
        assert "cross_domain_connections" in anomalies[0]["error"]

    def test_get_domain_summary_partial_parse_when_frontmatter_malformed(
        self, dep_map_root: Path
    ) -> None:
        """When the requested domain .md file has malformed YAML frontmatter,
        get_domain_summary must:
        - Still return a summary (not None) because body sections may be parseable
        - Set name and description to empty sentinels (frontmatter parse failed)
        - Record exactly one anomaly with 'frontmatter' in error

        AC7: malformed target domain file → anomaly recorded; domain still returned.

        This test is RED until get_domain_summary calls _parse_frontmatter_strict
        on the requested domain file within its own try/except.
        """
        d = dep_map_root / "dependency-map"
        domains = [
            {
                "name": "fm-domain",
                "description": "FM domain desc",
                "participating_repos": ["repo-p"],
            }
        ]
        _write_domains_json(d, domains)

        # Malformed YAML frontmatter + valid body sections
        bad_fm = "---\nname: [unclosed bracket\nbroken: :\n---\n"
        roles_section = _build_roles_table(
            [{"repo": "repo-p", "language": "Python", "role": "Provider"}]
        )
        body = (
            "# Domain Analysis: fm-domain\n\n"
            "## Overview\n\nOverview text.\n\n"
            + roles_section
            + "\n## Cross-Domain Connections\n\n"
            + _build_outgoing_table([])
            + "\n"
            + _build_incoming_table([])
        )
        (d / "fm-domain.md").write_text(bad_fm + body, encoding="utf-8")

        parser = _make_parser(dep_map_root)
        summary, anomalies = parser.get_domain_summary("fm-domain")

        # Summary is NOT None — file is readable, body sections parseable
        assert summary is not None
        # name/description fall back to empty sentinels since frontmatter failed
        assert summary["name"] == ""
        assert summary["description"] == ""
        # Body sections were still attempted: roles table has repo-p
        assert summary["participating_repos"] == [
            {"repo": "repo-p", "role": "Provider"}
        ]
        # No outgoing rows → empty
        assert summary["cross_domain_connections"] == []
        # Exactly one anomaly with frontmatter-specific label
        assert len(anomalies) == 1
        assert "fm-domain.md" in anomalies[0]["file"]
        assert "frontmatter" in anomalies[0]["error"]

    def test_get_domain_summary_partial_parse_when_participating_repos_malformed(
        self, dep_map_root: Path
    ) -> None:
        """When the roles section of the requested domain .md has corrupt content
        (heading present but non-table text), get_domain_summary must:
        - Still return a summary (not None)
        - Set participating_repos to [] (section failed)
        - Record exactly one anomaly with 'participating_repos' in error
        - Still parse cross_domain_connections successfully

        AC7: malformed target domain file → anomaly recorded; section-level
        resilience — other sections continue to be parsed.

        This test is RED until get_domain_summary calls _validate_section_has_table
        for the roles section and wraps it in its own try/except.
        """
        d = dep_map_root / "dependency-map"
        domains = [
            {
                "name": "pr-domain",
                "description": "PR domain desc",
                "participating_repos": ["repo-q"],
            }
        ]
        _write_domains_json(d, domains)

        # Write pr-domain.md with valid frontmatter + corrupt roles section
        # + valid outgoing section
        frontmatter = (
            "---\nname: pr-domain\ndescription: PR domain desc\n"
            "participating_repos:\n  - repo-q\n---\n"
        )
        corrupt_roles = (
            "## Repository Roles\n\n"
            "This roles section has been corrupted. No table here.\n"
            "Just garbage text instead of pipe-delimited rows.\n\n"
        )
        outgoing_rows = [
            {
                "this_repo": "repo-q",
                "depends_on": "repo-r",
                "target_domain": "far-domain",
                "dep_type": "Code-level",
                "why": "imports",
                "evidence": "main.py",
            }
        ]
        outgoing_section = _build_outgoing_table(outgoing_rows)
        incoming_section = _build_incoming_table([])
        body = (
            "# Domain Analysis: pr-domain\n\n"
            "## Overview\n\nOverview text.\n\n"
            + corrupt_roles
            + "\n## Cross-Domain Connections\n\n"
            + outgoing_section
            + "\n"
            + incoming_section
        )
        (d / "pr-domain.md").write_text(frontmatter + body, encoding="utf-8")

        parser = _make_parser(dep_map_root)
        summary, anomalies = parser.get_domain_summary("pr-domain")

        # Summary still returned
        assert summary is not None
        # Frontmatter parsed successfully
        assert summary["name"] == "pr-domain"
        assert summary["description"] == "PR domain desc"
        # Failed roles section defaults to empty
        assert summary["participating_repos"] == []
        # Cross-domain connections still parsed successfully
        assert len(summary["cross_domain_connections"]) == 1
        assert summary["cross_domain_connections"][0]["target_domain"] == "far-domain"
        # Exactly one anomaly with section-specific label
        assert len(anomalies) == 1
        assert "pr-domain.md" in anomalies[0]["file"]
        assert "participating_repos" in anomalies[0]["error"]

    def test_get_domain_summary_missing_path_returns_none_no_exception(
        self, tmp_path: Path
    ) -> None:
        """Non-existent dep_map_path → (None, []) from parser.

        Already passes under the stub — included to guard against regression.
        """
        parser = _make_parser(tmp_path / "no-such-dir")
        summary, anomalies = parser.get_domain_summary("any-domain")

        assert summary is None
        assert anomalies == []

    def test_get_domain_summary_file_not_found_returns_summary_with_read_anomaly_and_fallback_fields(
        self, dep_map_root: Path
    ) -> None:
        """Domain exists in _domains.json but .md file does not exist on disk.

        Expected: summary NOT None; name = domain_name (fallback),
        description = _domains.json description (fallback),
        participating_repos = [], cross_domain_connections = [];
        exactly one read anomaly ('file not found').

        Covers lines 476 (_build_name_description empty content),
        521 (_build_participating_repos empty content),
        554 (_build_cross_domain_connections empty content).
        """
        d = dep_map_root / "dependency-map"
        domains = [
            {
                "name": "ghost-domain",
                "description": "Ghost domain from _domains.json",
                "participating_repos": ["repo-ghost"],
            }
        ]
        _write_domains_json(d, domains)
        # Deliberately do NOT write ghost-domain.md

        parser = _make_parser(dep_map_root)
        summary, anomalies = parser.get_domain_summary("ghost-domain")

        assert summary is not None
        assert summary["name"] == "ghost-domain"
        assert summary["description"] == "Ghost domain from _domains.json"
        assert summary["participating_repos"] == []
        assert summary["cross_domain_connections"] == []
        assert len(anomalies) == 1
        assert "file not found" in anomalies[0]["error"]

    def test_get_domain_summary_no_frontmatter_uses_domains_json_fallbacks(
        self, dep_map_root: Path
    ) -> None:
        """.md file exists with roles table but no '---' frontmatter opener.

        Expected: summary NOT None; name = domain_name (fallback),
        description = _domains.json description (fallback);
        participating_repos populated from the roles table; no frontmatter anomaly.

        Covers line 478 (_build_name_description: content does not start with '---').
        """
        d = dep_map_root / "dependency-map"
        domains = [
            {
                "name": "nofm-domain",
                "description": "No-frontmatter domain desc",
                "participating_repos": ["repo-nofm"],
            }
        ]
        _write_domains_json(d, domains)

        # Write .md with ONLY a roles table + outgoing table — no frontmatter block
        roles_section = _build_roles_table(
            [{"repo": "repo-nofm", "language": "Python", "role": "Worker"}]
        )
        body = (
            "# Domain Analysis: nofm-domain\n\n"
            "## Overview\n\nOverview text.\n\n"
            + roles_section
            + "\n## Cross-Domain Connections\n\n"
            + _build_outgoing_table([])
            + "\n"
            + _build_incoming_table([])
        )
        (d / "nofm-domain.md").write_text(body, encoding="utf-8")

        parser = _make_parser(dep_map_root)
        summary, anomalies = parser.get_domain_summary("nofm-domain")

        assert summary is not None
        assert summary["name"] == "nofm-domain"
        assert summary["description"] == "No-frontmatter domain desc"
        assert summary["participating_repos"] == [
            {"repo": "repo-nofm", "role": "Worker"}
        ]
        # No frontmatter anomaly — the absent-frontmatter path is a silent fallback
        assert not any("frontmatter" in a.get("error", "") for a in anomalies)

    def test_get_domain_summary_frontmatter_opener_never_closed_records_anomaly(
        self, dep_map_root: Path
    ) -> None:
        """.md file starts with '---' but has no closing '---'.

        Expected: summary NOT None; name = "" and description = "" (sentinel
        values from failed frontmatter parse); exactly one anomaly whose error
        contains 'frontmatter' and 'never closed'.

        Covers line 482 (_build_name_description: len(parts) < 3 → ValueError).
        """
        d = dep_map_root / "dependency-map"
        domains = [
            {
                "name": "unclosed-domain",
                "description": "Unclosed frontmatter domain",
                "participating_repos": ["repo-uc"],
            }
        ]
        _write_domains_json(d, domains)

        # Content starts with '---' opener but the block is never closed.
        # Every character after the opener must avoid the three-dash sequence
        # because split("---", 2) would otherwise yield 3 parts and bypass
        # the len(parts) < 3 guard.  Plain prose only — no table separators.
        content = (
            "---\nname: unclosed-domain\ndescription: something\n"
            "no closing fence in this file\n\n"
            "Some body text without any triple-dash sequences.\n"
        )
        (d / "unclosed-domain.md").write_text(content, encoding="utf-8")

        parser = _make_parser(dep_map_root)
        summary, anomalies = parser.get_domain_summary("unclosed-domain")

        assert summary is not None
        assert summary["name"] == ""
        assert summary["description"] == ""
        assert len(anomalies) == 1
        assert "frontmatter" in anomalies[0]["error"]
        assert "never closed" in anomalies[0]["error"]


# ---------------------------------------------------------------------------
# S3 tests: get_stale_domains
# ---------------------------------------------------------------------------


def _write_domain_md_with_last_analyzed(
    dep_map_dir: Path,
    domain_name: str,
    last_analyzed: Optional[str],
) -> None:
    """Write a minimal domain .md file with optional last_analyzed in frontmatter.

    When last_analyzed is None, the frontmatter block is written without the
    last_analyzed key (simulating a domain file that has frontmatter but omits
    the field — AC3: missing last_analyzed → anomaly).
    """
    if last_analyzed is not None:
        frontmatter = f"---\nname: {domain_name}\nlast_analyzed: {last_analyzed}\n---\n"
    else:
        frontmatter = f"---\nname: {domain_name}\n---\n"
    body = f"# Domain Analysis: {domain_name}\n\nSome content.\n"
    (dep_map_dir / f"{domain_name}.md").write_text(frontmatter + body, encoding="utf-8")


class TestGetStaleDomains:
    @pytest.fixture
    def stale_root(self, dep_map_root: Path) -> Path:
        """Set up 3 domains with different last_analyzed dates, relative to frozen now=2026-04-20."""
        d = dep_map_root / "dependency-map"
        # frozen now = 2026-04-20T00:00:00+00:00
        # domain-old: 2026-01-01 → 109 days stale
        # domain-mid: 2026-03-01 → 50 days stale
        # domain-recent: 2026-04-18 → 2 days stale
        domains = [
            {"name": "domain-old", "description": "d", "participating_repos": []},
            {"name": "domain-mid", "description": "d", "participating_repos": []},
            {"name": "domain-recent", "description": "d", "participating_repos": []},
        ]
        _write_domains_json(d, domains)
        _write_domain_md_with_last_analyzed(
            d, "domain-old", "2026-01-01T00:00:00+00:00"
        )
        _write_domain_md_with_last_analyzed(
            d, "domain-mid", "2026-03-01T00:00:00+00:00"
        )
        _write_domain_md_with_last_analyzed(
            d, "domain-recent", "2026-04-18T00:00:00+00:00"
        )
        return dep_map_root

    def _freeze_now(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Freeze _current_utc_now to 2026-04-20 UTC for deterministic days_stale."""
        from datetime import datetime, timezone

        import code_indexer.server.services.dep_map_mcp_parser as mod

        monkeypatch.setattr(
            mod, "_current_utc_now", lambda: datetime(2026, 4, 20, tzinfo=timezone.utc)
        )

    def test_get_stale_domains_returns_only_items_exceeding_threshold(
        self, stale_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """threshold=5 returns only domains older than 5 days (domain-old: 109d, domain-mid: 50d)."""
        self._freeze_now(monkeypatch)
        parser = _make_parser(stale_root)
        stale, anomalies = parser.get_stale_domains(5)

        assert anomalies == []
        names = {d["domain_name"] for d in stale}
        assert "domain-old" in names
        assert "domain-mid" in names
        assert "domain-recent" not in names
        for entry in stale:
            assert "domain_name" in entry
            assert "last_analyzed" in entry
            assert "days_stale" in entry
            assert isinstance(entry["days_stale"], int)

    def test_get_stale_domains_threshold_zero_returns_all_parseable(
        self, stale_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """threshold=0 returns all domains with parseable last_analyzed."""
        self._freeze_now(monkeypatch)
        parser = _make_parser(stale_root)
        stale, anomalies = parser.get_stale_domains(0)

        assert anomalies == []
        names = {d["domain_name"] for d in stale}
        assert names == {"domain-old", "domain-mid", "domain-recent"}

    def test_get_stale_domains_sorted_descending_by_days_stale(
        self, stale_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Results are sorted descending by days_stale (domain-old first, domain-recent last)."""
        self._freeze_now(monkeypatch)
        parser = _make_parser(stale_root)
        stale, anomalies = parser.get_stale_domains(0)

        assert anomalies == []
        assert len(stale) == 3
        days = [d["days_stale"] for d in stale]
        assert days == sorted(days, reverse=True), f"Not sorted descending: {days}"
        assert stale[0]["domain_name"] == "domain-old"
        assert stale[-1]["domain_name"] == "domain-recent"

    def test_get_stale_domains_missing_last_analyzed_field_becomes_anomaly(
        self, dep_map_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Domain whose frontmatter lacks the last_analyzed key → anomaly, NOT in stale list."""
        d = dep_map_root / "dependency-map"
        domains = [
            {"name": "no-date-domain", "description": "d", "participating_repos": []},
        ]
        _write_domains_json(d, domains)
        # frontmatter has no last_analyzed key — simulates AC3 missing field
        _write_domain_md_with_last_analyzed(d, "no-date-domain", None)
        self._freeze_now(monkeypatch)

        parser = _make_parser(dep_map_root)
        stale, anomalies = parser.get_stale_domains(0)

        assert stale == []
        assert len(anomalies) == 1
        assert "no-date-domain" in anomalies[0]["file"]

    def test_get_stale_domains_unparseable_date_becomes_anomaly(
        self, dep_map_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Domain with unparseable last_analyzed string → anomaly, NOT in stale list."""
        d = dep_map_root / "dependency-map"
        domains = [
            {"name": "bad-date-domain", "description": "d", "participating_repos": []},
        ]
        _write_domains_json(d, domains)
        _write_domain_md_with_last_analyzed(d, "bad-date-domain", "not-a-date")
        self._freeze_now(monkeypatch)

        parser = _make_parser(dep_map_root)
        stale, anomalies = parser.get_stale_domains(0)

        assert stale == []
        assert len(anomalies) == 1
        assert "bad-date-domain" in anomalies[0]["file"]

    def test_get_stale_domains_uses_utc_now(
        self, dep_map_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """days_stale is computed from the injectable _current_utc_now — clock control works."""
        from datetime import datetime, timezone

        import code_indexer.server.services.dep_map_mcp_parser as mod

        d = dep_map_root / "dependency-map"
        domains = [{"name": "dom-clock", "description": "d", "participating_repos": []}]
        _write_domains_json(d, domains)
        # last_analyzed = 2026-01-01; if now = 2026-04-20 → 109 days
        _write_domain_md_with_last_analyzed(d, "dom-clock", "2026-01-01T00:00:00+00:00")

        monkeypatch.setattr(
            mod, "_current_utc_now", lambda: datetime(2026, 4, 20, tzinfo=timezone.utc)
        )
        parser = _make_parser(dep_map_root)
        stale, anomalies = parser.get_stale_domains(0)

        assert anomalies == []
        assert len(stale) == 1
        assert stale[0]["domain_name"] == "dom-clock"
        assert stale[0]["days_stale"] == 109

    def test_get_stale_domains_naive_datetime_becomes_anomaly_not_stale(
        self, dep_map_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Domain with naive ISO datetime (no tz) → anomaly, NOT in stale list.

        A naive string like '2026-04-18T12:00:00' (no Z, no offset) must be
        rejected explicitly rather than silently treated as host local time,
        which would shift the timestamp by the host's UTC offset.
        """
        d = dep_map_root / "dependency-map"
        domains = [
            {"name": "naive-dt-domain", "description": "d", "participating_repos": []},
        ]
        _write_domains_json(d, domains)
        # Naive ISO string — no offset, no Z suffix
        _write_domain_md_with_last_analyzed(d, "naive-dt-domain", "2026-04-18T12:00:00")
        self._freeze_now(monkeypatch)

        parser = _make_parser(dep_map_root)
        stale, anomalies = parser.get_stale_domains(0)

        assert stale == [], "Naive datetime domain must NOT appear in stale_domains"
        assert len(anomalies) == 1, "Exactly one anomaly expected for naive datetime"
        assert "naive-dt-domain.md" in anomalies[0]["file"], (
            "Anomaly must reference the specific markdown file naive-dt-domain.md"
        )
        error_msg = anomalies[0]["error"]
        assert "timezone-aware" in error_msg or "naive" in error_msg, (
            f"Error message must mention 'timezone-aware' or 'naive', got: {error_msg!r}"
        )

    def test_get_stale_domains_last_analyzed_is_json_serializable_string(
        self, dep_map_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stale_domains[*].last_analyzed must be a JSON-serializable ISO-8601 string.

        Regression test for manual-E2E bug: PyYAML parses bare (unquoted) ISO-8601
        dates into native datetime objects. When the parser forwards those raw
        values into the response dict, ``json.dumps`` at the MCP layer raises
        ``TypeError: Object of type datetime is not JSON serializable`` (JSON-RPC
        error -32603). The parser must normalize ``last_analyzed`` to a string
        (the parsed UTC-aware ISO-8601 form) before returning.
        """
        import json as _json  # local import keeps top-of-file untouched

        d = dep_map_root / "dependency-map"
        domains = [
            {"name": "dom-serialize", "description": "d", "participating_repos": []}
        ]
        _write_domains_json(d, domains)
        # Bare, unquoted ISO-8601 — PyYAML will parse this into a datetime object
        _write_domain_md_with_last_analyzed(
            d, "dom-serialize", "2026-01-01T00:00:00+00:00"
        )
        self._freeze_now(monkeypatch)

        parser = _make_parser(dep_map_root)
        stale, anomalies = parser.get_stale_domains(0)

        assert len(stale) == 1, "Domain must be included; PyYAML-parsed dates are valid"
        assert anomalies == [], "No anomalies expected for a valid ISO-8601 frontmatter"

        last_analyzed_val = stale[0]["last_analyzed"]
        assert isinstance(last_analyzed_val, str), (
            f"last_analyzed must be a string for JSON serialization, got "
            f"{type(last_analyzed_val).__name__}: {last_analyzed_val!r}"
        )
        # Must be JSON-serializable end-to-end (reproduces the real MCP crash)
        serialized = _json.dumps(stale)
        assert "dom-serialize" in serialized
        assert "last_analyzed" in serialized


# ---------------------------------------------------------------------------
# S4 tests: get_cross_domain_graph
# ---------------------------------------------------------------------------


def _write_domain_md_graph(
    dep_map_dir: Path,
    domain_name: str,
    outgoing_rows: List[Dict[str, str]],
    incoming_rows: Optional[List[Dict[str, str]]] = None,
) -> None:
    """Write a domain .md for cross-domain graph tests.

    Outgoing row keys: this_repo, depends_on, target_domain, dep_type.
    Incoming row keys: external_repo, depends_on, source_domain, dep_type.
    """
    if dep_map_dir is None:
        raise ValueError("dep_map_dir must not be None")
    if not domain_name or not domain_name.strip():
        raise ValueError("domain_name must not be empty or whitespace-only")
    if "/" in domain_name or "\\" in domain_name or ".." in domain_name:
        raise ValueError(
            f"domain_name contains unsafe characters for use as a filename: {domain_name!r}"
        )
    if outgoing_rows is None:
        outgoing_rows = []
    if incoming_rows is None:
        incoming_rows = []

    # Secondary containment check — ensures the resolved path stays under base.
    dest = (dep_map_dir / f"{domain_name}.md").resolve()
    dest.relative_to(dep_map_dir.resolve())  # raises ValueError if dest escapes base

    frontmatter = f"---\nname: {domain_name}\n---\n"

    out_header = (
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
    )
    out_body = "".join(
        f"| {r['this_repo']} | {r['depends_on']} | {r['target_domain']} | "
        f"{r.get('dep_type', 'Code-level')} | why | evidence |\n"
        for r in outgoing_rows
    )

    in_header = (
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
    )
    in_body = "".join(
        f"| {r['external_repo']} | {r['depends_on']} | {r['source_domain']} | "
        f"{r.get('dep_type', 'Code-level')} | why | evidence |\n"
        for r in incoming_rows
    )

    body = (
        f"# Domain Analysis: {domain_name}\n\n"
        "## Cross-Domain Connections\n\n"
        + out_header
        + out_body
        + "\n"
        + in_header
        + in_body
    )
    dest.write_text(frontmatter + body, encoding="utf-8")


class TestGetCrossDomainGraph:
    """Tests for DepMapMCPParser.get_cross_domain_graph() — Story #858 (S4)."""

    @pytest.fixture
    def graph_root(self, dep_map_root: Path) -> Path:
        """Four-domain graph: A→B, A→C, B→D, C→D with bidirectional consistency."""
        d = dep_map_root / "dependency-map"
        domains = [
            {"name": "domain-a", "description": "d", "participating_repos": []},
            {"name": "domain-b", "description": "d", "participating_repos": []},
            {"name": "domain-c", "description": "d", "participating_repos": []},
            {"name": "domain-d", "description": "d", "participating_repos": []},
        ]
        _write_domains_json(d, domains)
        _write_domain_md_graph(
            d,
            "domain-a",
            outgoing_rows=[
                {
                    "this_repo": "repo-a1",
                    "depends_on": "repo-b1",
                    "target_domain": "domain-b",
                    "dep_type": "Code-level",
                },
                {
                    "this_repo": "repo-a1",
                    "depends_on": "repo-c1",
                    "target_domain": "domain-c",
                    "dep_type": "Service integration",
                },
            ],
            incoming_rows=[],
        )
        _write_domain_md_graph(
            d,
            "domain-b",
            outgoing_rows=[
                {
                    "this_repo": "repo-b1",
                    "depends_on": "repo-d1",
                    "target_domain": "domain-d",
                    "dep_type": "Code-level",
                },
            ],
            incoming_rows=[
                {
                    "external_repo": "repo-a1",
                    "depends_on": "repo-b1",
                    "source_domain": "domain-a",
                    "dep_type": "Code-level",
                },
            ],
        )
        _write_domain_md_graph(
            d,
            "domain-c",
            outgoing_rows=[
                {
                    "this_repo": "repo-c1",
                    "depends_on": "repo-d1",
                    "target_domain": "domain-d",
                    "dep_type": "Data contracts",
                },
            ],
            incoming_rows=[
                {
                    "external_repo": "repo-a1",
                    "depends_on": "repo-c1",
                    "source_domain": "domain-a",
                    "dep_type": "Service integration",
                },
            ],
        )
        _write_domain_md_graph(
            d,
            "domain-d",
            outgoing_rows=[],
            incoming_rows=[
                {
                    "external_repo": "repo-b1",
                    "depends_on": "repo-d1",
                    "source_domain": "domain-b",
                    "dep_type": "Code-level",
                },
                {
                    "external_repo": "repo-c1",
                    "depends_on": "repo-d1",
                    "source_domain": "domain-c",
                    "dep_type": "Data contracts",
                },
            ],
        )
        return dep_map_root

    def test_get_cross_domain_graph_returns_all_edges(self, graph_root: Path) -> None:
        """Synthetic 4-domain graph yields exactly 4 edges: A→B, A→C, B→D, C→D."""
        parser = _make_parser(graph_root)
        edges, anomalies, *_ = parser.get_cross_domain_graph()

        assert anomalies == [], f"Expected no anomalies, got: {anomalies}"
        edge_pairs = {(e["source_domain"], e["target_domain"]) for e in edges}
        expected = {
            ("domain-a", "domain-b"),
            ("domain-a", "domain-c"),
            ("domain-b", "domain-d"),
            ("domain-c", "domain-d"),
        }
        assert edge_pairs == expected, f"Edge pairs mismatch: {edge_pairs}"
        for edge in edges:
            assert "source_domain" in edge
            assert "target_domain" in edge
            assert "dependency_count" in edge
            assert "types" in edge
            assert isinstance(edge["types"], list)
            assert len(edge["types"]) > 0, "types must never be empty"

    def test_get_cross_domain_graph_aggregates_duplicate_edges(
        self, dep_map_root: Path
    ) -> None:
        """Two outgoing rows from src to tgt → one edge with count=2, types merged."""
        d = dep_map_root / "dependency-map"
        _write_domains_json(
            d,
            [
                {"name": "src-domain", "description": "d", "participating_repos": []},
                {"name": "tgt-domain", "description": "d", "participating_repos": []},
            ],
        )
        _write_domain_md_graph(
            d,
            "src-domain",
            outgoing_rows=[
                {
                    "this_repo": "repo-s1",
                    "depends_on": "repo-t1",
                    "target_domain": "tgt-domain",
                    "dep_type": "Code-level",
                },
                {
                    "this_repo": "repo-s2",
                    "depends_on": "repo-t1",
                    "target_domain": "tgt-domain",
                    "dep_type": "Service integration",
                },
            ],
            incoming_rows=[],
        )
        _write_domain_md_graph(
            d,
            "tgt-domain",
            outgoing_rows=[],
            incoming_rows=[
                {
                    "external_repo": "repo-s1",
                    "depends_on": "repo-t1",
                    "source_domain": "src-domain",
                    "dep_type": "Code-level",
                },
                {
                    "external_repo": "repo-s2",
                    "depends_on": "repo-t1",
                    "source_domain": "src-domain",
                    "dep_type": "Service integration",
                },
            ],
        )

        parser = _make_parser(dep_map_root)
        edges, anomalies, *_ = parser.get_cross_domain_graph()

        assert anomalies == []
        assert len(edges) == 1, f"Expected exactly 1 aggregated edge, got {len(edges)}"
        edge = edges[0]
        assert edge["source_domain"] == "src-domain"
        assert edge["target_domain"] == "tgt-domain"
        assert edge["dependency_count"] == 2
        assert set(edge["types"]) == {"Code-level", "Service integration"}

    def test_get_cross_domain_graph_deterministic_type_order(
        self, dep_map_root: Path
    ) -> None:
        """types list is sorted alphabetically for deterministic output."""
        d = dep_map_root / "dependency-map"
        _write_domains_json(
            d,
            [
                {"name": "dom-x", "description": "d", "participating_repos": []},
                {"name": "dom-y", "description": "d", "participating_repos": []},
            ],
        )
        _write_domain_md_graph(
            d,
            "dom-x",
            outgoing_rows=[
                {
                    "this_repo": "r1",
                    "depends_on": "r2",
                    "target_domain": "dom-y",
                    "dep_type": "Service integration",
                },
                {
                    "this_repo": "r1",
                    "depends_on": "r3",
                    "target_domain": "dom-y",
                    "dep_type": "Code-level",
                },
                {
                    "this_repo": "r1",
                    "depends_on": "r4",
                    "target_domain": "dom-y",
                    "dep_type": "Data contracts",
                },
            ],
            incoming_rows=[],
        )
        _write_domain_md_graph(
            d,
            "dom-y",
            outgoing_rows=[],
            incoming_rows=[
                {
                    "external_repo": "r1",
                    "depends_on": "r2",
                    "source_domain": "dom-x",
                    "dep_type": "Service integration",
                },
                {
                    "external_repo": "r1",
                    "depends_on": "r3",
                    "source_domain": "dom-x",
                    "dep_type": "Code-level",
                },
                {
                    "external_repo": "r1",
                    "depends_on": "r4",
                    "source_domain": "dom-x",
                    "dep_type": "Data contracts",
                },
            ],
        )

        parser = _make_parser(dep_map_root)
        edges, anomalies, *_ = parser.get_cross_domain_graph()

        assert anomalies == []
        assert len(edges) == 1
        types = edges[0]["types"]
        assert types == sorted(types), f"types not sorted: {types}"
        assert types == ["Code-level", "Data contracts", "Service integration"]

    def test_get_cross_domain_graph_malformed_file_produces_anomaly_not_exception(
        self, dep_map_root: Path
    ) -> None:
        """Malformed YAML in one file → anomaly recorded, healthy edges still returned."""
        d = dep_map_root / "dependency-map"
        _write_domains_json(
            d,
            [
                {
                    "name": "healthy-domain",
                    "description": "d",
                    "participating_repos": [],
                },
                {
                    "name": "broken-domain",
                    "description": "d",
                    "participating_repos": [],
                },
                {
                    "name": "target-domain",
                    "description": "d",
                    "participating_repos": [],
                },
            ],
        )
        _write_domain_md_graph(
            d,
            "healthy-domain",
            outgoing_rows=[
                {
                    "this_repo": "repo-h",
                    "depends_on": "repo-t",
                    "target_domain": "target-domain",
                    "dep_type": "Code-level",
                },
            ],
            incoming_rows=[],
        )
        (d / "broken-domain.md").write_text(
            "---\nname: [unclosed bracket\nbroken: :\n---\n# Domain Analysis\n",
            encoding="utf-8",
        )
        _write_domain_md_graph(
            d,
            "target-domain",
            outgoing_rows=[],
            incoming_rows=[
                {
                    "external_repo": "repo-h",
                    "depends_on": "repo-t",
                    "source_domain": "healthy-domain",
                    "dep_type": "Code-level",
                },
            ],
        )

        parser = _make_parser(dep_map_root)
        edges, anomalies, *_ = parser.get_cross_domain_graph()

        assert len(anomalies) >= 1, "Expected anomaly for broken-domain"
        assert any("broken-domain" in a["file"] for a in anomalies)
        edge_pairs = {(e["source_domain"], e["target_domain"]) for e in edges}
        assert ("healthy-domain", "target-domain") in edge_pairs, (
            "Healthy edges must still be returned when one file is malformed"
        )

    def test_get_cross_domain_graph_bidirectional_mismatch_emits_anomaly(
        self, dep_map_root: Path
    ) -> None:
        """A's outgoing says A→B, but B's incoming omits A → bidirectional anomaly emitted."""
        d = dep_map_root / "dependency-map"
        _write_domains_json(
            d,
            [
                {"name": "source-dom", "description": "d", "participating_repos": []},
                {"name": "target-dom", "description": "d", "participating_repos": []},
            ],
        )
        _write_domain_md_graph(
            d,
            "source-dom",
            outgoing_rows=[
                {
                    "this_repo": "repo-s",
                    "depends_on": "repo-t",
                    "target_domain": "target-dom",
                    "dep_type": "Code-level",
                },
            ],
            incoming_rows=[],
        )
        # target-dom has NO incoming from source-dom (deliberate mismatch)
        _write_domain_md_graph(d, "target-dom", outgoing_rows=[], incoming_rows=[])

        parser = _make_parser(dep_map_root)
        edges, anomalies, *_ = parser.get_cross_domain_graph()

        edge_pairs = {(e["source_domain"], e["target_domain"]) for e in edges}
        assert ("source-dom", "target-dom") in edge_pairs, (
            "Edge must still be emitted even when incoming verification fails"
        )
        assert len(anomalies) >= 1, "Expected anomaly for bidirectional mismatch"
        anomaly_text = " ".join(a["error"] + a["file"] for a in anomalies)
        assert "source-dom" in anomaly_text or "target-dom" in anomaly_text, (
            f"Anomaly must reference the mismatched domains, got: {anomalies}"
        )

    def test_get_cross_domain_graph_empty_when_no_domains(
        self, dep_map_root: Path
    ) -> None:
        """Empty _domains.json → edges=[], anomalies=[]."""
        d = dep_map_root / "dependency-map"
        _write_domains_json(d, [])

        parser = _make_parser(dep_map_root)
        edges, anomalies, *_ = parser.get_cross_domain_graph()

        assert edges == []
        assert anomalies == []

    def test_get_cross_domain_graph_missing_dep_map_path_returns_empty(
        self, tmp_path: Path
    ) -> None:
        """Non-existent dep_map_path → ([], []) from parser; handler surfaces success=false."""
        parser = _make_parser(tmp_path / "no-such-dir")
        edges, anomalies, *_ = parser.get_cross_domain_graph()

        assert edges == []
        assert anomalies == []

    def test_get_cross_domain_graph_edge_with_no_derivable_types_emits_anomaly_and_omits_edge(
        self, dep_map_root: Path
    ) -> None:
        """Outgoing row with blank dep_type → anomaly recorded, edge omitted (AC-F6)."""
        d = dep_map_root / "dependency-map"
        _write_domains_json(
            d,
            [
                {
                    "name": "empty-type-src",
                    "description": "d",
                    "participating_repos": [],
                },
                {
                    "name": "empty-type-tgt",
                    "description": "d",
                    "participating_repos": [],
                },
            ],
        )
        # Manually write a file with a blank Type column in the outgoing table.
        frontmatter = "---\nname: empty-type-src\n---\n"
        body = (
            "# Domain Analysis: empty-type-src\n\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "| repo-e | repo-t | empty-type-tgt |  | why | evidence |\n"
            "\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
        )
        (d / "empty-type-src.md").write_text(frontmatter + body, encoding="utf-8")
        _write_domain_md_graph(d, "empty-type-tgt", outgoing_rows=[], incoming_rows=[])

        parser = _make_parser(dep_map_root)
        edges, anomalies, *_ = parser.get_cross_domain_graph()

        edge_pairs = {(e["source_domain"], e["target_domain"]) for e in edges}
        assert ("empty-type-src", "empty-type-tgt") not in edge_pairs, (
            "Edge with no derivable types must be omitted per AC-F6"
        )
        assert len(anomalies) >= 1, "Expected anomaly when edge has no derivable types"
