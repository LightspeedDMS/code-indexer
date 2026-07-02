"""
RED tests for Story #887 AC1 — parser integration for backtick stripping.

Each test injects backtick-wrapped values into the exact field under test with
no clean fallback, so each test fails if stripping is absent.

Fields covered: consuming_repo, depends_on, repo (roles table), edge endpoints
(both target_domain and source_domain independently).
"""

from pathlib import Path

import pytest

from tests.unit.server.services.test_dep_map_887_fixtures import (
    make_parser,
    write_domain_md_graph,
    write_domains_json,
)


@pytest.fixture
def dep_map_root(tmp_path: Path) -> Path:
    (tmp_path / "dependency-map").mkdir()
    return tmp_path


class TestAC1ParserIntegration:
    """Backtick stripping applied through DepMapMCPParser public API."""

    def test_consuming_repo_stripped_in_find_consumers(
        self, dep_map_root: Path
    ) -> None:
        """External Repo column backtick-wrapped → consuming_repo stripped.

        Only backtick-wrapped repo is in the table; assertion checks the clean
        value so stripping is required to satisfy it.
        """
        d = dep_map_root / "dependency-map"
        write_domains_json(
            d,
            [
                {
                    "name": "domain-alpha",
                    "description": "d",
                    "participating_repos": ["repo-consumer", "repo-x"],
                }
            ],
        )
        frontmatter = (
            "---\nname: domain-alpha\nparticipating_repos:\n"
            "  - repo-consumer\n  - repo-x\n---\n"
        )
        body = (
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            # External Repo (the consuming_repo field) is backtick-wrapped
            "| `repo-consumer` | repo-x | domain-alpha | Code-level | why | ev |\n"
        )
        (d / "domain-alpha.md").write_text(frontmatter + body, encoding="utf-8")

        consumers, _ = make_parser(dep_map_root).find_consumers("repo-x")

        assert len(consumers) == 1
        repo = consumers[0]["consuming_repo"]
        assert repo == "repo-consumer", (
            f"Expected 'repo-consumer' after stripping, got: {repo!r}"
        )

    def test_depends_on_stripped_matches_clean_query(self, dep_map_root: Path) -> None:
        """Backtick-wrapped Depends On column → stripped before query match.

        The query is the clean name; only the backtick-wrapped form exists in
        the table.  The test fails if depends_on is not stripped before matching.
        """
        d = dep_map_root / "dependency-map"
        write_domains_json(
            d,
            [
                {
                    "name": "domain-bt",
                    "description": "d",
                    "participating_repos": ["consumer-bt", "repo-target"],
                }
            ],
        )
        frontmatter = (
            "---\nname: domain-bt\nparticipating_repos:\n"
            "  - consumer-bt\n  - repo-target\n---\n"
        )
        body = (
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            # Depends On column is backtick-wrapped — only this row exists
            "| consumer-bt | `repo-target` | domain-bt | Code-level | why | ev |\n"
        )
        (d / "domain-bt.md").write_text(frontmatter + body, encoding="utf-8")

        # Query with the clean name — matches only if backticks stripped from depends_on
        consumers, _ = make_parser(dep_map_root).find_consumers("repo-target")

        assert len(consumers) == 1, (
            "find_consumers must strip backticks from depends_on column before matching; "
            f"got {len(consumers)} consumers"
        )
        assert consumers[0]["consuming_repo"] == "consumer-bt"

    def test_repo_stripped_in_get_domain_summary_roles_table(
        self, dep_map_root: Path
    ) -> None:
        """Repository column in roles table is ONLY available backtick-wrapped.

        _domains.json is intentionally empty so the result is driven entirely
        by the roles table parse. The assertion checks the clean name — fails
        if stripping is absent.
        """
        d = dep_map_root / "dependency-map"
        # ONLY backtick-wrapped repo in roles table; no clean duplicate anywhere
        write_domains_json(
            d,
            [
                {
                    "name": "bt-domain",
                    "description": "d",
                    # participating_repos left empty to force roles-table parsing
                    "participating_repos": [],
                }
            ],
        )
        frontmatter = "---\nname: bt-domain\ndescription: test\n---\n"
        body = (
            "## Repository Roles\n\n"
            "| Repository | Language | Role |\n"
            "|---|---|---|\n"
            # ONLY backtick-wrapped form — no clean version exists
            "| `repo-bt` | Python | Consumer |\n\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
        )
        (d / "bt-domain.md").write_text(frontmatter + body, encoding="utf-8")

        summary, _ = make_parser(dep_map_root).get_domain_summary("bt-domain")

        assert summary is not None
        repos = [pr["repo"] for pr in summary["participating_repos"]]
        # Must be the clean value — only possible if backticks are stripped
        assert repos == ["repo-bt"], (
            f"Expected ['repo-bt'] after stripping backticks from roles table, got: {repos}"
        )

    def test_target_domain_stripped_in_edge(self, dep_map_root: Path) -> None:
        """target_domain backtick-wrapped in outgoing table → stripped in graph edge."""
        d = dep_map_root / "dependency-map"
        write_domains_json(
            d,
            [
                {"name": "src-domain", "description": "d", "participating_repos": []},
                {"name": "tgt-domain", "description": "d", "participating_repos": []},
            ],
        )
        frontmatter = "---\nname: src-domain\n---\n"
        body = (
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            # target_domain backtick-wrapped
            "| repo-s | repo-t | `tgt-domain` | Code-level | why | evidence |\n\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
        )
        (d / "src-domain.md").write_text(frontmatter + body, encoding="utf-8")
        write_domain_md_graph(
            d,
            "tgt-domain",
            incoming_rows=[
                {
                    "external_repo": "repo-s",
                    "depends_on": "repo-t",
                    "source_domain": "src-domain",
                    "dep_type": "Code-level",
                }
            ],
        )

        edges, _, _, _ = make_parser(
            dep_map_root
        ).get_cross_domain_graph_with_channels()

        assert len(edges) >= 1, "Expected edge after target_domain backtick strip"
        tgt_domains = [e["target_domain"] for e in edges]
        # Clean value must appear — only possible if backticks stripped from outgoing row
        assert "tgt-domain" in tgt_domains, (
            f"Expected clean 'tgt-domain', got: {tgt_domains}"
        )
        for val in tgt_domains:
            assert not val.startswith("`") and not val.endswith("`")

    def test_source_domain_stripped_in_edge(self, dep_map_root: Path) -> None:
        """source_domain backtick-wrapped in incoming table → stripped in graph edge.

        Incoming claims are collected in _collect_incoming_claims; source_domain
        comes from cells[_COL_SOURCE_DOMAIN] which must be stripped.
        """
        d = dep_map_root / "dependency-map"
        write_domains_json(
            d,
            [
                {"name": "src-domain", "description": "d", "participating_repos": []},
                {"name": "tgt-domain", "description": "d", "participating_repos": []},
            ],
        )
        # outgoing row uses clean domain name
        write_domain_md_graph(
            d,
            "src-domain",
            outgoing_rows=[
                {
                    "this_repo": "repo-s",
                    "depends_on": "repo-t",
                    "target_domain": "tgt-domain",
                    "dep_type": "Code-level",
                }
            ],
        )
        frontmatter = "---\nname: tgt-domain\n---\n"
        body = (
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            # source_domain backtick-wrapped in incoming table
            "| repo-s | repo-t | `src-domain` | Code-level | why | evidence |\n"
        )
        (d / "tgt-domain.md").write_text(frontmatter + body, encoding="utf-8")

        edges, _, _, _ = make_parser(
            dep_map_root
        ).get_cross_domain_graph_with_channels()

        assert len(edges) >= 1, "Expected edge after source_domain backtick strip"
        # The edge source must be the clean value
        src_domains = [e["source_domain"] for e in edges]
        assert "src-domain" in src_domains, (
            f"Expected clean 'src-domain', got: {src_domains}"
        )
        for val in src_domains:
            assert not val.startswith("`") and not val.endswith("`")
