"""
RED tests for Story #887 AC5 — Anomaly Aggregation by Count.

aggregate_anomalies() threshold boundary, count invariant, examples, per-type.
Blocker 1: pipeline integration test — aggregate_anomalies must be called in
get_cross_domain_graph() so callers see AnomalyAggregate entries when count > threshold.
"""

import json
from pathlib import Path
from typing import List, Tuple

import pytest

from tests.unit.server.services.test_dep_map_887_fixtures import (
    import_hygiene_symbol,
    make_parser,
)

DEFAULT_THRESHOLD = 5
ABOVE_THRESHOLD = 6
AT_THRESHOLD = 5
LARGE_COUNT = 10
ONE_AGGREGATE = 1
TWO_AGGREGATES = 2


@pytest.fixture
def AnomalyType():
    return import_hygiene_symbol("AnomalyType")


@pytest.fixture
def AnomalyEntry():
    return import_hygiene_symbol("AnomalyEntry")


@pytest.fixture
def AnomalyAggregate():
    return import_hygiene_symbol("AnomalyAggregate")


@pytest.fixture
def aggregate_fn():
    return import_hygiene_symbol("aggregate_anomalies")


def _make_entries(anomaly_type, count: int, AnomalyEntry) -> List:
    """Build AnomalyEntry list with distinct messages."""
    return [
        AnomalyEntry(
            type=anomaly_type,
            file=f"f{i}.md",
            message=f"msg-{i:02d}",
            channel="data",
        )
        for i in range(count)
    ]


class TestAggregationThreshold:
    """Threshold boundary: >threshold aggregates, <=threshold does not."""

    def test_aggregates_when_count_exceeds_threshold(
        self, AnomalyType, AnomalyEntry, AnomalyAggregate, aggregate_fn
    ) -> None:
        entries = _make_entries(
            AnomalyType.GARBAGE_DOMAIN_REJECTED, ABOVE_THRESHOLD, AnomalyEntry
        )
        result = aggregate_fn(entries, threshold=DEFAULT_THRESHOLD)

        agg_list = [r for r in result if isinstance(r, AnomalyAggregate)]
        assert len(agg_list) == ONE_AGGREGATE
        assert agg_list[0].count == ABOVE_THRESHOLD

    def test_does_not_aggregate_at_threshold(
        self, AnomalyType, AnomalyEntry, AnomalyAggregate, aggregate_fn
    ) -> None:
        """AT_THRESHOLD entries must NOT aggregate (>threshold required, not >=)."""
        entries = _make_entries(
            AnomalyType.GARBAGE_DOMAIN_REJECTED, AT_THRESHOLD, AnomalyEntry
        )
        result = aggregate_fn(entries, threshold=DEFAULT_THRESHOLD)

        assert not any(isinstance(r, AnomalyAggregate) for r in result)
        assert len(result) == AT_THRESHOLD

    def test_default_threshold_boundary(
        self, AnomalyType, AnomalyEntry, AnomalyAggregate, aggregate_fn
    ) -> None:
        """Default threshold == DEFAULT_THRESHOLD: AT does not aggregate, ABOVE does."""
        at_result = aggregate_fn(
            _make_entries(AnomalyType.SELF_LOOP, AT_THRESHOLD, AnomalyEntry)
        )
        above_result = aggregate_fn(
            _make_entries(AnomalyType.SELF_LOOP, ABOVE_THRESHOLD, AnomalyEntry)
        )

        assert not any(isinstance(r, AnomalyAggregate) for r in at_result), (
            f"AT_THRESHOLD={AT_THRESHOLD} must NOT aggregate with default"
        )
        assert any(isinstance(r, AnomalyAggregate) for r in above_result), (
            f"ABOVE_THRESHOLD={ABOVE_THRESHOLD} must aggregate with default"
        )


class TestAggregationInvariantsAndExamples:
    """Count invariant, examples ordering, per-type separation."""

    def test_invariant_aggregate_count_equals_total(
        self, AnomalyType, AnomalyEntry, AnomalyAggregate, aggregate_fn
    ) -> None:
        entries = _make_entries(
            AnomalyType.GARBAGE_DOMAIN_REJECTED, LARGE_COUNT, AnomalyEntry
        )
        result = aggregate_fn(entries, threshold=DEFAULT_THRESHOLD)

        agg_list = [r for r in result if isinstance(r, AnomalyAggregate)]
        assert len(agg_list) == ONE_AGGREGATE
        assert agg_list[0].count == LARGE_COUNT

    def test_first_example_is_first_entry(
        self, AnomalyType, AnomalyEntry, AnomalyAggregate, aggregate_fn
    ) -> None:
        entries = _make_entries(
            AnomalyType.GARBAGE_DOMAIN_REJECTED, LARGE_COUNT, AnomalyEntry
        )
        result = aggregate_fn(entries, threshold=DEFAULT_THRESHOLD)

        agg = next(r for r in result if isinstance(r, AnomalyAggregate))
        assert len(agg.examples) >= ONE_AGGREGATE
        assert agg.examples[0].message == "msg-00"

    def test_two_types_produce_exactly_two_aggregates(
        self, AnomalyType, AnomalyEntry, AnomalyAggregate, aggregate_fn
    ) -> None:
        entries = _make_entries(
            AnomalyType.GARBAGE_DOMAIN_REJECTED, ABOVE_THRESHOLD, AnomalyEntry
        ) + _make_entries(AnomalyType.SELF_LOOP, ABOVE_THRESHOLD, AnomalyEntry)
        result = aggregate_fn(entries, threshold=DEFAULT_THRESHOLD)

        agg_list = [r for r in result if isinstance(r, AnomalyAggregate)]
        assert len(agg_list) == TWO_AGGREGATES
        agg_types = {r.type for r in agg_list}
        assert AnomalyType.GARBAGE_DOMAIN_REJECTED in agg_types
        assert AnomalyType.SELF_LOOP in agg_types


# ===========================================================================
# Blocker 1: AC5 aggregation wired into production pipeline
# ===========================================================================

_N_GARBAGE_DOMAINS = 10  # strictly > DEFAULT_THRESHOLD=5


def _write_prose_garbage_fixture(dep_map_dir: Path, n_domains: int) -> None:
    """Write N domains each with one prose-fragment outgoing target (-> GARBAGE anomaly)."""
    domains = [
        {"name": f"dom{i}", "description": "d", "participating_repos": []}
        for i in range(n_domains)
    ]
    (dep_map_dir / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")
    prose_target = "This is a prose fragment with spaces (parenthesis)"
    for i in range(n_domains):
        content = (
            f"---\nname: dom{i}\n---\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            f"| repo | repo2 | {prose_target} | Code-level | why | ev |\n\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
        )
        (dep_map_dir / f"dom{i}.md").write_text(content, encoding="utf-8")


@pytest.fixture
def _garbage_aggregates(tmp_path: Path) -> Tuple[List, type]:
    """Build 10-domain prose-garbage fixture; return (agg_list, AnomalyAggregate class).

    agg_list contains only AnomalyAggregate entries from the pipeline output so
    tests can assert directly without repeating the filter expression.
    """
    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    _write_prose_garbage_fixture(dep_map_dir, _N_GARBAGE_DOMAINS)
    AnomalyAggregate = import_hygiene_symbol("AnomalyAggregate")
    _, all_anomalies, _, _ = make_parser(
        tmp_path
    ).get_cross_domain_graph_with_channels()
    agg_list = [a for a in all_anomalies if isinstance(a, AnomalyAggregate)]
    return agg_list, AnomalyAggregate


class TestAC5PipelineIntegration:
    """Blocker 1: aggregate_anomalies must be called in get_cross_domain_graph() pipeline.

    When N > threshold same-type anomalies occur, the returned anomaly list must
    contain AnomalyAggregate entries (not N individual AnomalyEntry objects).
    Without the fix, get_cross_domain_graph never calls aggregate_anomalies.
    """

    def test_pipeline_aggregate_anomalies_when_threshold_exceeded(
        self, _garbage_aggregates: Tuple[List, type]
    ) -> None:
        """10 GARBAGE_DOMAIN_REJECTED anomalies (> threshold=5) must produce
        at least one AnomalyAggregate with count >= 6 in returned anomalies."""
        agg_list, _ = _garbage_aggregates
        assert len(agg_list) >= 1, (
            f"Expected at least one AnomalyAggregate when {_N_GARBAGE_DOMAINS} "
            f"same-type anomalies exceed threshold=5, but got 0 aggregates. "
            f"aggregate_anomalies() is not wired into the production pipeline."
        )
        assert agg_list[0].count >= 6, (
            f"AnomalyAggregate.count must be >= 6, got {agg_list[0].count}"
        )

    def test_pipeline_aggregate_examples_capped_at_default_limit(
        self, _garbage_aggregates: Tuple[List, type]
    ) -> None:
        """AnomalyAggregate.examples must be capped at _DEFAULT_ANOMALY_EXAMPLE_LIMIT=3."""
        agg_list, _ = _garbage_aggregates
        assert len(agg_list) >= 1, (
            "Need at least one AnomalyAggregate to verify examples cap"
        )
        assert len(agg_list[0].examples) <= 3, (
            f"AnomalyAggregate.examples must be capped at 3, "
            f"got {len(agg_list[0].examples)}"
        )
