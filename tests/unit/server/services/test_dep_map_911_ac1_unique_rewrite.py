"""
Story #911 AC1: Unique-Mapping Cell Rewrite And Mirror Backfill.

Tests that _repair_garbage_domain_rejected rewrites the prose-fragment cell
in the source outgoing table and backfills a mirror row in the target's
incoming table when exactly one domain matches.
"""

import pytest

from tests.unit.server.services.test_dep_map_911_builders import (
    make_domains_json_911,
    make_executor_911,
    make_garbage_domain_anomaly,
    make_source_domain_file,
    make_target_domain_file,
)

_PROSE = "the order-service repo handles order events"
_SOURCE_STEM = "domain-a"
_TARGET_STEM = "order-fulfillment"
_DOMAINS = [
    {"name": _SOURCE_STEM, "participating_repos": ["service-a"]},
    {"name": _TARGET_STEM, "participating_repos": ["order-service"]},
]


@pytest.fixture()
def unique_mapping_setup(tmp_path):
    """Arrange source file, target file, domains JSON, anomaly, and executor."""
    make_source_domain_file(
        tmp_path, _SOURCE_STEM, _PROSE, dep_type="Service integration"
    )
    make_target_domain_file(tmp_path, _TARGET_STEM)
    make_domains_json_911(tmp_path, _DOMAINS)
    anomaly = make_garbage_domain_anomaly(f"{_SOURCE_STEM}.md", _PROSE)
    executor = make_executor_911()
    return tmp_path, anomaly, executor


def test_unique_mapping_rewrites_source_cell(unique_mapping_setup):
    """Unique domain match rewrites the prose-fragment cell to the canonical domain name."""
    tmp_path, anomaly, executor = unique_mapping_setup
    fixed: list = []
    errors: list = []

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, fixed, errors)

    source_content = (tmp_path / f"{_SOURCE_STEM}.md").read_text()
    assert _TARGET_STEM in source_content
    assert _PROSE not in source_content
    assert not errors


def test_unique_mapping_backfills_target_incoming_row(unique_mapping_setup):
    """Mirror row with source domain, repo, and dep type is added to target incoming table."""
    tmp_path, anomaly, executor = unique_mapping_setup

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, [], [])

    target_content = (tmp_path / f"{_TARGET_STEM}.md").read_text()
    assert _SOURCE_STEM in target_content
    assert "service-a" in target_content
    assert "Service integration" in target_content


def test_unique_mapping_rescue_message_in_fixed(unique_mapping_setup):
    """fixed[] contains the canonical rescue message referencing source and target."""
    tmp_path, anomaly, executor = unique_mapping_setup
    fixed: list = []

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, fixed, [])

    assert any(
        f"Phase 3.7: rescued garbage-domain cell in {_SOURCE_STEM}.md -> {_TARGET_STEM}"
        in f
        for f in fixed
    )
