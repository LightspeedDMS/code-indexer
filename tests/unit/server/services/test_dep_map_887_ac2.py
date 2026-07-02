"""
RED tests for Story #887 AC2 — Prose-Fragment Domain Name Rejection.

Tests: is_prose_fragment() pure function + parser integration.
Parser integration verifies rejected domains absent from graph nodes and
exactly one GARBAGE_DOMAIN_REJECTED anomaly per rejected occurrence.
"""

from pathlib import Path

import pytest

from tests.unit.server.services.test_dep_map_887_fixtures import (
    import_hygiene_symbol,
    make_parser,
    write_domain_md_graph,
    write_domains_json,
)

MAX_DOMAIN_NAME_LENGTH = 120


@pytest.fixture
def dep_map_root(tmp_path: Path) -> Path:
    (tmp_path / "dependency-map").mkdir()
    return tmp_path


# ===========================================================================
# Pure function tests
# ===========================================================================


class TestIsProseFragmentPureFunction:
    """Unit tests for is_prose_fragment() in dep_map_parser_hygiene."""

    def test_rejects_newline(self) -> None:
        fn = import_hygiene_symbol("is_prose_fragment")
        assert fn("foo\nbar") is True

    def test_rejects_open_paren(self) -> None:
        fn = import_hygiene_symbol("is_prose_fragment")
        assert fn("(AWS Infrastructure)") is True

    def test_rejects_close_paren(self) -> None:
        fn = import_hygiene_symbol("is_prose_fragment")
        assert fn("foo)bar") is True

    def test_rejects_colon_outside_url(self) -> None:
        fn = import_hygiene_symbol("is_prose_fragment")
        assert fn("foo: bar") is True

    def test_allows_colon_in_https_url(self) -> None:
        fn = import_hygiene_symbol("is_prose_fragment")
        assert fn("https://example.com") is False

    def test_allows_colon_in_http_url(self) -> None:
        fn = import_hygiene_symbol("is_prose_fragment")
        assert fn("http://example.com/path") is False

    def test_rejects_three_consecutive_spaces(self) -> None:
        fn = import_hygiene_symbol("is_prose_fragment")
        assert fn("foo   bar") is True

    def test_allows_one_space(self) -> None:
        fn = import_hygiene_symbol("is_prose_fragment")
        assert fn("my domain") is False

    def test_allows_two_consecutive_spaces(self) -> None:
        """Two spaces do NOT trigger the 3+ run heuristic."""
        fn = import_hygiene_symbol("is_prose_fragment")
        assert fn("foo  bar") is False

    def test_rejects_over_max_length(self) -> None:
        fn = import_hygiene_symbol("is_prose_fragment")
        assert fn("a" * (MAX_DOMAIN_NAME_LENGTH + 1)) is True

    def test_allows_exactly_max_length(self) -> None:
        fn = import_hygiene_symbol("is_prose_fragment")
        assert fn("a" * MAX_DOMAIN_NAME_LENGTH) is False

    def test_normal_domains_pass_all_heuristics(self) -> None:
        fn = import_hygiene_symbol("is_prose_fragment")
        for name in ("evolution-dms-core", "my-service", "domain123"):
            assert fn(name) is False, f"Normal domain {name!r} incorrectly rejected"

    def test_rejects_parenthesised_self_annotation(self) -> None:
        fn = import_hygiene_symbol("is_prose_fragment")
        assert fn("evolution-dms-core (self)") is True


# ===========================================================================
# Parser integration tests
# ===========================================================================


class TestAC2ParserIntegration:
    """Prose-fragment domains never become graph nodes; emit GARBAGE_DOMAIN_REJECTED."""

    def test_prose_fragment_absent_from_graph_nodes(self, dep_map_root: Path) -> None:
        """Prose-fragment target domain must NOT appear as source or target in any edge."""
        d = dep_map_root / "dependency-map"
        write_domains_json(
            d,
            [
                {"name": "clean-src", "description": "d", "participating_repos": []},
                {"name": "clean-tgt", "description": "d", "participating_repos": []},
            ],
        )
        frontmatter = "---\nname: clean-src\n---\n"
        body = (
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            # One row points to a prose-fragment domain
            "| r1 | r2 | (template repo) | Code-level | why | evidence |\n"
            # One row points to a clean domain
            "| r1 | r3 | clean-tgt | Code-level | why | evidence |\n\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
        )
        (d / "clean-src.md").write_text(frontmatter + body, encoding="utf-8")
        write_domain_md_graph(
            d,
            "clean-tgt",
            incoming_rows=[
                {
                    "external_repo": "r1",
                    "depends_on": "r3",
                    "source_domain": "clean-src",
                    "dep_type": "Code-level",
                }
            ],
        )

        edges, _, _, _ = make_parser(
            dep_map_root
        ).get_cross_domain_graph_with_channels()

        all_domains = {e["source_domain"] for e in edges} | {
            e["target_domain"] for e in edges
        }
        assert "(template repo)" not in all_domains, (
            "Prose-fragment domain must not appear as a graph node"
        )
        # Clean domain still present
        assert "clean-tgt" in all_domains

    def test_exactly_one_garbage_anomaly_per_prose_fragment_occurrence(
        self, dep_map_root: Path
    ) -> None:
        """One prose-fragment row → exactly 1 GARBAGE_DOMAIN_REJECTED in data_anomalies."""
        d = dep_map_root / "dependency-map"
        write_domains_json(
            d,
            [
                {"name": "src", "description": "d", "participating_repos": []},
                {"name": "tgt", "description": "d", "participating_repos": []},
            ],
        )
        frontmatter = "---\nname: src\n---\n"
        body = (
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            # Exactly one prose-fragment row
            "| r1 | r2 | (bad domain) | Code-level | why | evidence |\n"
            # One clean row
            "| r1 | r3 | tgt | Code-level | why | evidence |\n\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
        )
        (d / "src.md").write_text(frontmatter + body, encoding="utf-8")
        write_domain_md_graph(
            d,
            "tgt",
            incoming_rows=[
                {
                    "external_repo": "r1",
                    "depends_on": "r3",
                    "source_domain": "src",
                    "dep_type": "Code-level",
                }
            ],
        )

        _, _, _, data_anomalies = make_parser(
            dep_map_root
        ).get_cross_domain_graph_with_channels()

        AnomalyType = import_hygiene_symbol("AnomalyType")
        garbage = [
            a
            for a in data_anomalies
            if hasattr(a, "type") and a.type == AnomalyType.GARBAGE_DOMAIN_REJECTED
        ]
        assert len(garbage) == 1, (
            f"Expected exactly 1 GARBAGE_DOMAIN_REJECTED anomaly, "
            f"got {len(garbage)}: {garbage}"
        )
