"""
RED tests for Story #887 AC3 (case normalization) and AC4 (self-loop preservation).

AC3: normalize_identifier pure function + lowercase emitted + CASE_NORMALIZATION_APPLIED anomaly.
AC4: self-loop edge preserved + SELF_LOOP anomaly in data_anomalies + invariant.
"""

from pathlib import Path

import pytest

from tests.unit.server.services.test_dep_map_887_fixtures import (
    import_hygiene_symbol,
    make_parser,
    make_self_loop_graph,
    write_domain_md_graph,
    write_domains_json,
)


@pytest.fixture
def dep_map_root(tmp_path: Path) -> Path:
    (tmp_path / "dependency-map").mkdir()
    return tmp_path


# ===========================================================================
# AC3: Case Normalization — pure function
# ===========================================================================


class TestNormalizeIdentifierPureFunction:
    """Unit tests for normalize_identifier() in dep_map_parser_hygiene."""

    def test_lowercases_mixed_case(self) -> None:
        fn = import_hygiene_symbol("normalize_identifier")
        result, modified = fn("Evolution")
        assert result == "evolution"
        assert modified is True

    def test_already_lowercase_unchanged(self) -> None:
        fn = import_hygiene_symbol("normalize_identifier")
        result, modified = fn("evolution")
        assert result == "evolution"
        assert modified is False

    def test_strips_backticks_and_lowercases(self) -> None:
        fn = import_hygiene_symbol("normalize_identifier")
        result, modified = fn("`Evolution`")
        assert result == "evolution"
        assert modified is True

    def test_backtick_wrapped_lowercase_reports_modified(self) -> None:
        """Backtick-wrapped all-lowercase: stripped → modified (backtick removed)."""
        fn = import_hygiene_symbol("normalize_identifier")
        result, modified = fn("`evolution`")
        assert result == "evolution"
        assert modified is True

    def test_empty_string_unchanged(self) -> None:
        fn = import_hygiene_symbol("normalize_identifier")
        result, modified = fn("")
        assert result == ""
        assert modified is False


# ===========================================================================
# AC3: Case Normalization — parser integration
# ===========================================================================


class TestAC3ParserIntegration:
    """Mixed-case domain names emitted lowercase + CASE_NORMALIZATION_APPLIED anomaly."""

    def test_graph_edges_emit_lowercase_domains(self, dep_map_root: Path) -> None:
        """Mixed-case domain names appear in lowercase in emitted graph edges."""
        d = dep_map_root / "dependency-map"
        write_domains_json(
            d,
            [
                {"name": "Evolution", "description": "d", "participating_repos": []},
                {"name": "Other", "description": "d", "participating_repos": []},
            ],
        )
        (d / "Evolution.md").write_text(
            "---\nname: Evolution\n---\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "| repo-e | repo-o | Other | Code-level | why | evidence |\n\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n",
            encoding="utf-8",
        )
        (d / "Other.md").write_text(
            "---\nname: Other\n---\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "| repo-e | repo-o | Evolution | Code-level | why | evidence |\n",
            encoding="utf-8",
        )

        edges, _, _, _ = make_parser(
            dep_map_root
        ).get_cross_domain_graph_with_channels()

        # Must produce at least one edge — proves the loop below is non-vacuous
        assert len(edges) >= 1, (
            "Expected at least one graph edge from mixed-case fixture"
        )
        # The specific normalized edge must be present
        pairs = {(e["source_domain"], e["target_domain"]) for e in edges}
        assert ("evolution", "other") in pairs, (
            f"Expected normalized edge ('evolution','other'), got: {pairs}"
        )
        for edge in edges:
            assert edge["source_domain"] == edge["source_domain"].lower(), (
                f"source_domain not lowercase: {edge['source_domain']!r}"
            )
            assert edge["target_domain"] == edge["target_domain"].lower(), (
                f"target_domain not lowercase: {edge['target_domain']!r}"
            )

    def test_case_normalization_applied_anomaly_in_data_channel(
        self, dep_map_root: Path
    ) -> None:
        """Mixed-case identifier emits CASE_NORMALIZATION_APPLIED in data_anomalies."""
        d = dep_map_root / "dependency-map"
        write_domains_json(
            d,
            [
                {"name": "MixedCase", "description": "d", "participating_repos": []},
                {"name": "other", "description": "d", "participating_repos": []},
            ],
        )
        (d / "MixedCase.md").write_text(
            "---\nname: MixedCase\n---\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "| r1 | r2 | other | Code-level | why | evidence |\n\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n",
            encoding="utf-8",
        )
        write_domain_md_graph(
            d,
            "other",
            incoming_rows=[
                {
                    "external_repo": "r1",
                    "depends_on": "r2",
                    "source_domain": "MixedCase",
                    "dep_type": "Code-level",
                }
            ],
        )

        _, _, _, data_anomalies = make_parser(
            dep_map_root
        ).get_cross_domain_graph_with_channels()

        AnomalyType = import_hygiene_symbol("AnomalyType")
        case_anomalies = [
            a
            for a in data_anomalies
            if hasattr(a, "type") and a.type == AnomalyType.CASE_NORMALIZATION_APPLIED
        ]
        assert len(case_anomalies) >= 1, (
            f"Expected CASE_NORMALIZATION_APPLIED in data_anomalies, got: {data_anomalies}"
        )


# ===========================================================================
# AC4: Self-Loop Preservation — parser integration
# ===========================================================================


class TestAC4SelfLoopPreservation:
    """Self-loop edges preserved in graph output with SELF_LOOP anomaly in data channel."""

    def test_self_loop_edge_preserved_in_graph(self, dep_map_root: Path) -> None:
        """Self-loop (domain→domain) appears in edges output — NOT dropped."""
        make_self_loop_graph(dep_map_root, "loop-domain")

        edges, _, _, _ = make_parser(
            dep_map_root
        ).get_cross_domain_graph_with_channels()

        self_loops = [
            e
            for e in edges
            if e["source_domain"] == e["target_domain"] == "loop-domain"
        ]
        assert len(self_loops) == 1, (
            f"Expected self-loop edge preserved, got edges: {edges}"
        )

    def test_self_loop_emits_self_loop_anomaly_in_data_channel(
        self, dep_map_root: Path
    ) -> None:
        """Self-loop edge emits SELF_LOOP anomaly in data_anomalies."""
        make_self_loop_graph(dep_map_root, "loop-dom")

        _, _, _, data_anomalies = make_parser(
            dep_map_root
        ).get_cross_domain_graph_with_channels()

        AnomalyType = import_hygiene_symbol("AnomalyType")
        self_loop_anomalies = [
            a
            for a in data_anomalies
            if hasattr(a, "type") and a.type == AnomalyType.SELF_LOOP
        ]
        assert len(self_loop_anomalies) >= 1, (
            f"Expected SELF_LOOP anomaly in data_anomalies, got: {data_anomalies}"
        )

    def test_invariant_each_self_loop_edge_has_data_anomaly(
        self, dep_map_root: Path
    ) -> None:
        """Invariant: count of SELF_LOOP anomalies >= count of self-loop edges."""
        make_self_loop_graph(dep_map_root, "inv-loop")

        edges, _, _, data_anomalies = make_parser(
            dep_map_root
        ).get_cross_domain_graph_with_channels()

        AnomalyType = import_hygiene_symbol("AnomalyType")
        self_loops_in_edges = [
            e for e in edges if e["source_domain"] == e["target_domain"]
        ]
        self_loop_anomalies = [
            a
            for a in data_anomalies
            if hasattr(a, "type") and a.type == AnomalyType.SELF_LOOP
        ]
        assert len(self_loops_in_edges) >= 1, (
            "Fixture must produce at least one self-loop edge"
        )
        assert len(self_loop_anomalies) >= len(self_loops_in_edges), (
            f"Must have at least one SELF_LOOP anomaly per self-loop edge: "
            f"edges={len(self_loops_in_edges)}, anomalies={len(self_loop_anomalies)}"
        )


# ===========================================================================
# AC4: Self-Loop with empty types — UNCONDITIONAL preservation (Blocker 5)
# ===========================================================================


class TestAC4SelfLoopEmptyTypesUnconditional:
    """AC4: self-loop edge preserved even when dep_type cell is blank (empty types set)."""

    def test_self_loop_with_empty_types_preserved_in_graph_edges(
        self, dep_map_root: Path
    ) -> None:
        """Self-loop edge with empty dep_type must appear in edges, not be dropped.

        AC4 mandates self-loops are preserved UNCONDITIONALLY. The current
        finalize_graph_edges() drops ANY edge with empty types via 'continue',
        which silently discards self-loops whose type cell is blank.
        This test exposes that bug (RED) before the fix (GREEN).
        """
        d = dep_map_root / "dependency-map"
        # Write _domains.json with one domain
        write_domains_json(
            d,
            [{"name": "loop", "description": "d", "participating_repos": []}],
        )
        # Write domain .md with self-loop outgoing row where dep_type is EMPTY
        # so that edge_data[("loop","loop")]["types"] remains an empty set.
        (d / "loop.md").write_text(
            "---\nname: loop\n---\n"
            "# Domain Analysis: loop\n\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "| repo-x | repo-y | loop |  | why | evidence |\n\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "| repo-x | repo-y | loop |  | why | evidence |\n",
            encoding="utf-8",
        )

        edges, _, _, data_anomalies = make_parser(
            dep_map_root
        ).get_cross_domain_graph_with_channels()

        # Self-loop edge MUST appear in edges (AC4: unconditional preservation)
        self_loops = [
            e for e in edges if e["source_domain"] == e["target_domain"] == "loop"
        ]
        assert len(self_loops) == 1, (
            f"AC4: self-loop with empty types must be preserved in edges, "
            f"got edges={edges}"
        )

        # SELF_LOOP anomaly must appear in data_anomalies
        AnomalyType = import_hygiene_symbol("AnomalyType")
        self_loop_anomalies = [
            a
            for a in data_anomalies
            if hasattr(a, "type") and a.type == AnomalyType.SELF_LOOP
        ]
        assert len(self_loop_anomalies) >= 1, (
            f"AC4: SELF_LOOP anomaly must be in data_anomalies, got: {data_anomalies}"
        )
