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
            ("get_stale_domains", (), []),
            ("get_cross_domain_graph", (), []),
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
        """When a domain file has malformed YAML frontmatter, an anomaly is recorded
        but the requested domain summary is still returned if its own file is parseable.

        This test is RED under the S1 stub (returns None instead of a summary dict).
        """
        d = dep_map_root / "dependency-map"
        domains = [
            {
                "name": "target-domain",
                "description": "Target domain desc",
                "participating_repos": ["repo-a"],
            },
            {
                "name": "other-domain",
                "description": "Other",
                "participating_repos": ["repo-b"],
            },
        ]
        _write_domains_json(d, domains)
        _write_domain_md_full(
            d,
            "target-domain",
            [{"repo": "repo-a", "language": "Python", "role": "Main"}],
            outgoing_rows=[
                {
                    "this_repo": "repo-a",
                    "depends_on": "repo-b",
                    "target_domain": "other-domain",
                    "dep_type": "Code-level",
                    "why": "imports",
                    "evidence": "main.py",
                }
            ],
        )
        # Write malformed other-domain .md
        bad_content = "---\ndomain: [unclosed\nbroken: :\n---\n# bad\n\n"
        (d / "other-domain.md").write_text(bad_content, encoding="utf-8")

        parser = _make_parser(dep_map_root)
        summary, anomalies = parser.get_domain_summary("target-domain")

        # Summary for target-domain should still be returned
        assert summary is not None
        assert summary["name"] == "target-domain"
        assert summary["description"] == "Target domain desc"

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
