"""
Story #911 AC3: No Mapping Found Routes To Manual Review.

Tests that when prose-fragment tokens match NO domain in _domains.json:
- source file bytes are identical to original
- errors[] is exactly ["Phase 3.7: no mapping in <file>; manual review required"]
- log_messages contains exactly the no-mapping log string with candidates = []
- fixed[] is exactly []
"""

import pytest

from tests.unit.server.services.test_dep_map_911_builders import (
    make_domains_json_911,
    make_executor_911,
    make_garbage_domain_anomaly,
    make_source_domain_file,
)

_PROSE = "completely-unknown-service does something unrelated"
_SOURCE_STEM = "domain-a"
_ANOMALY_FILE = f"{_SOURCE_STEM}.md"
_DOMAINS = [
    {"name": "order-fulfillment", "participating_repos": ["order-service"]},
]

_EXPECTED_ERROR = f"Phase 3.7: no mapping in {_ANOMALY_FILE}; manual review required"
_EXPECTED_LOG = (
    f"Phase 3.7: no garbage-domain mapping in {_ANOMALY_FILE}: candidates = []"
)


@pytest.fixture()
def no_mapping_setup(tmp_path):
    """Source file with tokens that match no domain participating_repos."""
    make_source_domain_file(tmp_path, _SOURCE_STEM, _PROSE)
    make_domains_json_911(tmp_path, _DOMAINS)
    anomaly = make_garbage_domain_anomaly(_ANOMALY_FILE, _PROSE)
    log_messages: list = []
    executor = make_executor_911(journal_callback=log_messages.append)
    original_bytes = (tmp_path / f"{_SOURCE_STEM}.md").read_bytes()
    return tmp_path, anomaly, executor, log_messages, original_bytes


def test_no_mapping_source_file_bytes_unchanged(no_mapping_setup):
    """Source file bytes are identical to original when no domain matches."""
    tmp_path, anomaly, executor, _, original_bytes = no_mapping_setup

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, [], [])

    assert (tmp_path / f"{_SOURCE_STEM}.md").read_bytes() == original_bytes


def test_no_mapping_exact_error_appended(no_mapping_setup):
    """errors[] contains exactly the expected no-mapping error string."""
    tmp_path, anomaly, executor, _, _ = no_mapping_setup
    errors: list = []

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, [], errors)

    assert errors == [_EXPECTED_ERROR]


def test_no_mapping_exact_log_message_present(no_mapping_setup):
    """The exact no-mapping log string with candidates = [] is in log_messages."""
    tmp_path, anomaly, executor, log_messages, _ = no_mapping_setup

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, [], [])

    assert _EXPECTED_LOG in log_messages


def test_no_mapping_fixed_is_empty(no_mapping_setup):
    """fixed[] remains exactly empty when no mapping is found."""
    tmp_path, anomaly, executor, _, _ = no_mapping_setup
    fixed: list = []

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, fixed, [])

    assert fixed == []
