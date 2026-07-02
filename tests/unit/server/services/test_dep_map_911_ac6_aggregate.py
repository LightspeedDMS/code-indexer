"""
Story #911 AC6: AnomalyAggregate Is Iterated Correctly.

Tests that when _repair_garbage_domain_rejected receives an AnomalyAggregate,
it processes every AnomalyEntry inside it rather than treating the aggregate
as a single anomaly.
"""

import pytest

from code_indexer.server.services.dep_map_parser_hygiene import (
    AnomalyAggregate,
    AnomalyEntry,
    AnomalyType,
)
from tests.unit.server.services.test_dep_map_911_builders import (
    make_domains_json_911,
    make_executor_911,
    make_source_domain_file,
    make_target_domain_file,
)

_PROSE_A = "order-service alpha path"
_PROSE_B = "order-service beta path"
_SOURCE_A = "domain-a"
_SOURCE_B = "domain-b"
_TARGET_STEM = "order-fulfillment"
_DOMAINS = [
    {"name": _TARGET_STEM, "participating_repos": ["order-service"]},
]


def _make_entry(filename: str, prose: str) -> AnomalyEntry:
    return AnomalyEntry(
        type=AnomalyType.GARBAGE_DOMAIN_REJECTED,
        file=filename,
        message=f"prose-fragment target domain rejected: {prose!r}",
        channel="data",
        count=1,
    )


@pytest.fixture()
def aggregate_setup(tmp_path):
    """Two source files + shared target; aggregate bundles both anomalies."""
    make_source_domain_file(tmp_path, _SOURCE_A, _PROSE_A)
    make_source_domain_file(tmp_path, _SOURCE_B, _PROSE_B)
    make_target_domain_file(tmp_path, _TARGET_STEM)
    make_domains_json_911(tmp_path, _DOMAINS)
    aggregate = AnomalyAggregate(
        type=AnomalyType.GARBAGE_DOMAIN_REJECTED,
        count=2,
        examples=[
            _make_entry(f"{_SOURCE_A}.md", _PROSE_A),
            _make_entry(f"{_SOURCE_B}.md", _PROSE_B),
        ],
    )
    executor = make_executor_911()
    return tmp_path, aggregate, executor


def test_aggregate_both_source_files_rewritten(aggregate_setup):
    """Both source files have their prose fragment replaced after one aggregate call."""
    tmp_path, aggregate, executor = aggregate_setup

    executor._repair_garbage_domain_rejected(tmp_path, aggregate, [], [])

    content_a = (tmp_path / f"{_SOURCE_A}.md").read_text()
    content_b = (tmp_path / f"{_SOURCE_B}.md").read_text()
    assert _PROSE_A not in content_a
    assert _PROSE_B not in content_b


def test_aggregate_target_gets_two_incoming_rows(aggregate_setup):
    """Target file receives one mirror row per aggregate example."""
    from code_indexer.server.services.dep_map_repair_garbage_domain import (
        find_existing_incoming_row,
    )

    tmp_path, aggregate, executor = aggregate_setup

    executor._repair_garbage_domain_rejected(tmp_path, aggregate, [], [])

    content = (tmp_path / f"{_TARGET_STEM}.md").read_text()
    assert find_existing_incoming_row(content, _SOURCE_A, "Service integration")
    assert find_existing_incoming_row(content, _SOURCE_B, "Service integration")


def test_aggregate_fixed_has_two_entries(aggregate_setup):
    """fixed[] accumulates one entry per successfully repaired example."""
    tmp_path, aggregate, executor = aggregate_setup
    fixed: list = []

    executor._repair_garbage_domain_rejected(tmp_path, aggregate, fixed, [])

    assert len(fixed) == 2
