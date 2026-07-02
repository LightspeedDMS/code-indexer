"""
RED tests for Story #887 AC7 (channel split) and AC8 (module split compliance).

AC7: AnomalyType enum with bound channel; split_anomaly_channels; 4-tuple return;
     malformed YAML → parser channel; bidi mismatch → data channel.
AC8: 4 modules importable; all public API methods return correct tuple shapes;
     each module ≤500 lines.
"""

from pathlib import Path

import pytest

from tests.unit.server.services.test_dep_map_887_fixtures import (
    import_hygiene_symbol,
    make_parser,
    make_simple_graph,
    write_domains_json,
)


@pytest.fixture
def dep_map_root(tmp_path: Path) -> Path:
    (tmp_path / "dependency-map").mkdir()
    return tmp_path


@pytest.fixture
def AnomalyType():
    return import_hygiene_symbol("AnomalyType")


# ===========================================================================
# AC7: Anomaly Channel Split
# ===========================================================================


class TestAnomalyTypeEnum:
    """AnomalyType enum variants carry bound channel; split_anomaly_channels works."""

    def test_all_variants_have_parser_or_data_channel(self, AnomalyType) -> None:
        """Every AnomalyType variant has channel in ('parser', 'data')."""
        import enum

        assert issubclass(AnomalyType, enum.Enum)
        for v in AnomalyType:
            assert hasattr(v, "channel")
            assert v.channel in ("parser", "data"), (
                f"{v.name}.channel={v.channel!r} not in ('parser','data')"
            )

    def test_required_parser_and_data_variants_present(self, AnomalyType) -> None:
        """parser channel has MALFORMED_YAML + PATH_TRAVERSAL_REJECTED;
        data channel has BIDIRECTIONAL_MISMATCH, SELF_LOOP, GARBAGE_DOMAIN_REJECTED,
        CASE_NORMALIZATION_APPLIED."""
        parser_names = {v.name for v in AnomalyType if v.channel == "parser"}
        data_names = {v.name for v in AnomalyType if v.channel == "data"}
        for name in ("MALFORMED_YAML", "PATH_TRAVERSAL_REJECTED"):
            assert name in parser_names, f"Missing {name} in parser channel"
        for name in (
            "BIDIRECTIONAL_MISMATCH",
            "SELF_LOOP",
            "GARBAGE_DOMAIN_REJECTED",
            "CASE_NORMALIZATION_APPLIED",
        ):
            assert name in data_names, f"Missing {name} in data channel"

    def test_split_anomaly_channels_separates_entries(self, AnomalyType) -> None:
        """split_anomaly_channels puts each entry in the correct output list."""
        AnomalyEntry = import_hygiene_symbol("AnomalyEntry")
        fn = import_hygiene_symbol("split_anomaly_channels")
        entries = [
            AnomalyEntry(
                type=AnomalyType.MALFORMED_YAML,
                file="f.md",
                message="bad yaml",
                channel="parser",
            ),
            AnomalyEntry(
                type=AnomalyType.SELF_LOOP,
                file="g.md",
                message="self loop",
                channel="data",
            ),
        ]
        parser_out, data_out = fn(entries)

        assert len(parser_out) == 1 and parser_out[0].type == AnomalyType.MALFORMED_YAML
        assert len(data_out) == 1 and data_out[0].type == AnomalyType.SELF_LOOP


class TestChannelRoutingIntegration:
    """4-tuple return; legacy union; malformed YAML→parser; bidi→data."""

    def test_get_cross_domain_graph_returns_four_tuple_and_legacy_union(
        self, dep_map_root: Path
    ) -> None:
        """get_cross_domain_graph_with_channels returns 4-tuple;
        legacy count == parser + data."""
        make_simple_graph(dep_map_root, "dom-c", "dom-d", bidirectional=False)

        result = make_parser(dep_map_root).get_cross_domain_graph_with_channels()

        assert len(result) == 4, f"Expected 4-tuple, got {len(result)}-tuple"
        _, anomalies, parser_anomalies, data_anomalies = result
        assert len(anomalies) == len(parser_anomalies) + len(data_anomalies)

    def test_malformed_yaml_in_parser_channel_not_data(
        self, AnomalyType, dep_map_root: Path
    ) -> None:
        """Malformed YAML frontmatter anomaly → parser_anomalies, not data_anomalies."""
        d = dep_map_root / "dependency-map"
        write_domains_json(
            d, [{"name": "bad-yaml", "description": "d", "participating_repos": []}]
        )
        (d / "bad-yaml.md").write_text(
            "---\nname: [unclosed\nbroken: :\n---\n# bad\n", encoding="utf-8"
        )
        _, _, parser_anomalies, data_anomalies = make_parser(
            dep_map_root
        ).get_cross_domain_graph_with_channels()

        assert any("bad-yaml" in str(a) for a in parser_anomalies), (
            "Malformed YAML must appear in parser_anomalies"
        )
        assert not any("bad-yaml" in str(a) for a in data_anomalies), (
            "Malformed YAML must NOT appear in data_anomalies"
        )

    def test_bidi_mismatch_in_data_channel_not_parser(
        self, AnomalyType, dep_map_root: Path
    ) -> None:
        """Bidirectional mismatch → data_anomalies, not parser_anomalies."""
        make_simple_graph(dep_map_root, "src-e", "tgt-e", bidirectional=False)
        _, _, parser_anomalies, data_anomalies = make_parser(
            dep_map_root
        ).get_cross_domain_graph_with_channels()

        bidi_data = [
            a for a in data_anomalies if a.type == AnomalyType.BIDIRECTIONAL_MISMATCH
        ]
        bidi_parser = [
            a for a in parser_anomalies if a.type == AnomalyType.BIDIRECTIONAL_MISMATCH
        ]
        assert len(bidi_data) >= 1, "Bidi mismatch must be in data_anomalies"
        assert len(bidi_parser) == 0, "Bidi mismatch must NOT be in parser_anomalies"


# ===========================================================================
# AC8: Parser Module Split — MESSI Rule 6 Compliance
# ===========================================================================


class TestModuleSplitImports:
    """All 4 modules importable; all public API methods return correct tuple shapes."""

    def test_all_four_modules_importable(self) -> None:
        """dep_map_mcp_parser, dep_map_parser_hygiene, dep_map_parser_tables,
        dep_map_parser_graph are all importable without error."""
        from code_indexer.server.services import dep_map_mcp_parser
        from code_indexer.server.services import dep_map_parser_hygiene
        from code_indexer.server.services import dep_map_parser_tables
        from code_indexer.server.services import dep_map_parser_graph

        for mod in (
            dep_map_mcp_parser,
            dep_map_parser_hygiene,
            dep_map_parser_tables,
            dep_map_parser_graph,
        ):
            assert mod is not None

    def test_all_public_api_methods_return_correct_tuple_shapes(
        self, dep_map_root: Path
    ) -> None:
        """find_consumers, get_repo_domains, get_stale_domains return 2-tuples;
        get_domain_summary returns 2-tuple with None for unknown domain.
        Public API unchanged by module split."""
        d = dep_map_root / "dependency-map"
        (d / "_domains.json").write_text("[]", encoding="utf-8")
        parser = make_parser(dep_map_root)

        for method, args in (
            ("find_consumers", ("any",)),
            ("get_repo_domains", ("any",)),
            ("get_stale_domains", (0,)),
        ):
            result = getattr(parser, method)(*args)
            assert isinstance(result, tuple) and len(result) == 2, (
                f"{method} must return 2-tuple, got {len(result)}-tuple"
            )

        summary_result = parser.get_domain_summary("nonexistent")
        assert isinstance(summary_result, tuple) and len(summary_result) == 2
        assert summary_result[0] is None


class TestModuleLineCounts:
    """Each of the 4 modules must be ≤500 lines (MESSI Rule 6)."""

    def test_all_modules_under_500_lines(self) -> None:
        """dep_map_mcp_parser, dep_map_parser_hygiene, dep_map_parser_tables,
        dep_map_parser_graph each have ≤500 lines."""
        import code_indexer.server.services.dep_map_mcp_parser as m1
        import code_indexer.server.services.dep_map_parser_hygiene as m2
        import code_indexer.server.services.dep_map_parser_tables as m3
        import code_indexer.server.services.dep_map_parser_graph as m4

        for mod in (m1, m2, m3, m4):
            lines = len(Path(mod.__file__).read_text(encoding="utf-8").splitlines())
            assert lines <= 500, (
                f"{Path(mod.__file__).name} has {lines} lines — exceeds 500-line cap"
            )


# ===========================================================================
# NEW-2 (ITEM D): Aggregates must NOT be silently dropped from channel split
# ===========================================================================


def _write_many_prose_fragment_rows(dep_map_dir: Path, count: int = 10) -> None:
    """Write a domain file with *count* prose-fragment outgoing rows.

    Each row has a unique garbage target_domain that passes is_prose_fragment()
    (uses 3+ consecutive spaces to trigger the heuristic). This produces *count*
    distinct GARBAGE_DOMAIN_REJECTED AnomalyEntry values, all of the same type.
    Aggregate threshold is 5, so count=10 guarantees aggregation.
    """
    import json

    domains = [
        {"name": "src-domain", "description": "d", "participating_repos": []},
    ]
    (dep_map_dir / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")

    rows = "".join(
        f"| repo-a | repo-b | This is prose fragment {i:02d}   extra | Code-level | why | ev |\n"
        for i in range(count)
    )
    content = (
        "---\nname: src-domain\n---\n"
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n" + rows + "\n### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
    )
    (dep_map_dir / "src-domain.md").write_text(content, encoding="utf-8")


class TestAggregatesInChannelSplit:
    """NEW-2 (ITEM D): AnomalyAggregate entries must reach channel lists, not be dropped.

    build_graph_anomalies() currently filters aggregated to AnomalyEntry only before
    channel split. When a type crosses the aggregation threshold (>5), the aggregate
    replaces all individual entries. If channel split only processes AnomalyEntry,
    the aggregate silently vanishes from both parser_anomalies and data_anomalies
    while appearing in all_anomalies — a Rule 13 (silent failure) violation.

    Fix (Approach B): route each aggregate to its channel via AnomalyType.channel.
    """

    def test_aggregate_appears_in_data_anomalies_not_silently_dropped(
        self, dep_map_root: Path, AnomalyType
    ) -> None:
        """10 prose-fragment anomalies trigger GARBAGE_DOMAIN_REJECTED aggregation.
        The AnomalyAggregate for GARBAGE_DOMAIN_REJECTED must appear in data_anomalies.

        Without the fix: data_anomalies=[] while all_anomalies has the aggregate.
        After fix: data_anomalies contains the AnomalyAggregate (GARBAGE_DOMAIN_REJECTED
        has channel='data' per AnomalyType enum).
        """
        from code_indexer.server.services.dep_map_parser_hygiene import AnomalyAggregate

        d = dep_map_root / "dependency-map"
        _write_many_prose_fragment_rows(d, count=10)

        _, all_anomalies, parser_anomalies, data_anomalies = make_parser(
            dep_map_root
        ).get_cross_domain_graph_with_channels()

        # Confirm GARBAGE_DOMAIN_REJECTED aggregation happened in all_anomalies
        garbage_aggregates_all = [
            a
            for a in all_anomalies
            if isinstance(a, AnomalyAggregate)
            and a.type == AnomalyType.GARBAGE_DOMAIN_REJECTED
        ]
        assert len(garbage_aggregates_all) >= 1, (
            f"Expected AnomalyAggregate for GARBAGE_DOMAIN_REJECTED in all_anomalies "
            f"(10 rows > threshold 5). all_anomalies={all_anomalies}"
        )

        # The GARBAGE_DOMAIN_REJECTED aggregate must be present in data_anomalies
        garbage_aggregates_data = [
            a
            for a in data_anomalies
            if isinstance(a, AnomalyAggregate)
            and a.type == AnomalyType.GARBAGE_DOMAIN_REJECTED
        ]
        assert len(garbage_aggregates_data) >= 1, (
            f"AnomalyAggregate for GARBAGE_DOMAIN_REJECTED (data channel) must appear "
            f"in data_anomalies, but got data_anomalies={data_anomalies}. "
            f"all_anomalies has {len(garbage_aggregates_all)} aggregate(s). "
            f"NEW-2 (ITEM D): aggregates silently dropped from channel split."
        )

        # GARBAGE_DOMAIN_REJECTED aggregate must NOT appear in parser_anomalies
        garbage_aggregates_parser = [
            a
            for a in parser_anomalies
            if isinstance(a, AnomalyAggregate)
            and a.type == AnomalyType.GARBAGE_DOMAIN_REJECTED
        ]
        assert len(garbage_aggregates_parser) == 0, (
            f"GARBAGE_DOMAIN_REJECTED (data channel) must not appear in parser_anomalies. "
            f"Got {garbage_aggregates_parser}"
        )
