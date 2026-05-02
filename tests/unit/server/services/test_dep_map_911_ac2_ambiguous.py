"""
Story #911 AC2: Ambiguous Mapping Routes To Manual Review.

Tests that when a prose-fragment's tokens match more than one domain in
_domains.json, no file write occurs; errors[] records the ambiguous case;
and candidate domain names appear in the executor's log output.
"""

import pytest

from tests.unit.server.services.test_dep_map_911_builders import (
    make_domains_json_911,
    make_executor_911,
    make_garbage_domain_anomaly,
    make_source_domain_file,
    make_target_domain_file,
)

_PROSE = "order-service payment-service both handle transactions"
_SOURCE_STEM = "domain-a"
_DOMAINS = [
    {"name": "order-fulfillment", "participating_repos": ["order-service"]},
    {"name": "payment-processing", "participating_repos": ["payment-service"]},
]


@pytest.fixture()
def ambiguous_setup(tmp_path):
    """Two candidate domains, one prose fragment that matches both."""
    make_source_domain_file(tmp_path, _SOURCE_STEM, _PROSE)
    make_target_domain_file(tmp_path, "order-fulfillment")
    make_target_domain_file(tmp_path, "payment-processing")
    make_domains_json_911(tmp_path, _DOMAINS)
    anomaly = make_garbage_domain_anomaly(f"{_SOURCE_STEM}.md", _PROSE)
    log_messages: list = []
    executor = make_executor_911(journal_callback=log_messages.append)
    original_source = (tmp_path / f"{_SOURCE_STEM}.md").read_text()
    return tmp_path, anomaly, executor, log_messages, original_source


def test_ambiguous_no_source_file_rewrite(ambiguous_setup):
    """Source file is NOT modified when the mapping is ambiguous."""
    tmp_path, anomaly, executor, _, original_source = ambiguous_setup

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, [], [])

    assert (tmp_path / f"{_SOURCE_STEM}.md").read_text() == original_source


def test_ambiguous_appends_error_with_ambiguous_label(ambiguous_setup):
    """errors[] contains an 'ambiguous' label for the source file."""
    tmp_path, anomaly, executor, _, _ = ambiguous_setup
    errors: list = []

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, [], errors)

    assert any("ambiguous" in e for e in errors)


def test_ambiguous_candidate_domains_in_log(ambiguous_setup):
    """Both candidate domain names appear in the executor log output."""
    tmp_path, anomaly, executor, log_messages, _ = ambiguous_setup

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, [], [])

    combined_log = " ".join(log_messages)
    assert "order-fulfillment" in combined_log
    assert "payment-processing" in combined_log


def test_ambiguous_nothing_added_to_fixed(ambiguous_setup):
    """No 'rescued' entry appears in fixed[] when mapping is ambiguous."""
    tmp_path, anomaly, executor, _, _ = ambiguous_setup
    fixed: list = []

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, fixed, [])

    assert not any("rescued" in f for f in fixed)
