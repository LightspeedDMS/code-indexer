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
