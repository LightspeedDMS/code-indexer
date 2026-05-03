"""
Story #911 AC4: Mirror Backfill Is Idempotent.

Tests that a second call to _repair_garbage_domain_rejected on the same anomaly:
- Does NOT append a duplicate mirror row to the target file
- find_existing_incoming_row correctly detects an already-present row (positive case)
- find_existing_incoming_row returns False when row is absent (negative case)
"""

import pytest

from tests.unit.server.services.test_dep_map_911_builders import (
    make_domains_json_911,
    make_executor_911,
    make_garbage_domain_anomaly,
    make_source_domain_file,
    make_target_domain_file,
)

_PROSE = "order-service legacy payment hook"
_SOURCE_STEM = "domain-a"
_TARGET_STEM = "order-fulfillment"
_ANOMALY_FILE = f"{_SOURCE_STEM}.md"
_DOMAINS = [
    {"name": _TARGET_STEM, "participating_repos": ["order-service"]},
]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _count_incoming_data_rows(content: str) -> int:
    """Count non-header, non-separator rows in the Incoming Dependencies section."""
    in_incoming = False
    count = 0
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped == "### Incoming Dependencies":
            in_incoming = True
            continue
        if in_incoming and stripped.startswith("#"):
            break
        if in_incoming and stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if (
                len(cells) >= 4
                and cells[0] != "External Repo"
                and not (set(cells[0]) <= frozenset("-"))
            ):
                count += 1
    return count


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def idempotent_setup(tmp_path):
    """Source + target domain files with a unique domain match."""
    make_source_domain_file(tmp_path, _SOURCE_STEM, _PROSE)
    make_target_domain_file(tmp_path, _TARGET_STEM)
    make_domains_json_911(tmp_path, _DOMAINS)
    anomaly = make_garbage_domain_anomaly(_ANOMALY_FILE, _PROSE)
    executor = make_executor_911()
    return tmp_path, anomaly, executor


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_first_call_adds_exactly_one_mirror_row(idempotent_setup):
    """After the first repair, target file has exactly one incoming data row."""
    tmp_path, anomaly, executor = idempotent_setup

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, [], [])

    content = (tmp_path / f"{_TARGET_STEM}.md").read_text()
    assert _count_incoming_data_rows(content) == 1


def test_second_call_does_not_add_duplicate_row(idempotent_setup):
    """Second call is a true no-op: one incoming row, zero errors."""
    tmp_path, anomaly, executor = idempotent_setup
    errors: list = []

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, [], [])
    executor._repair_garbage_domain_rejected(tmp_path, anomaly, [], errors)

    content = (tmp_path / f"{_TARGET_STEM}.md").read_text()
    assert _count_incoming_data_rows(content) == 1
    assert errors == []


def test_find_existing_incoming_row_detects_present_row(idempotent_setup):
    """find_existing_incoming_row returns True when the mirror row already exists."""
    from code_indexer.server.services.dep_map_repair_garbage_domain import (
        find_existing_incoming_row,
    )

    tmp_path, anomaly, executor = idempotent_setup

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, [], [])

    content = (tmp_path / f"{_TARGET_STEM}.md").read_text()
    assert (
        find_existing_incoming_row(content, _SOURCE_STEM, "Service integration") is True
    )


def test_find_existing_incoming_row_returns_false_when_absent():
    """find_existing_incoming_row returns False for a row that has not been inserted."""
    from code_indexer.server.services.dep_map_repair_garbage_domain import (
        find_existing_incoming_row,
    )

    content = (
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
    )
    assert (
        find_existing_incoming_row(content, "domain-a", "Service integration") is False
    )


def test_first_call_repairs_anomaly_row_despite_preexisting_target_domain_row(tmp_path):
    """A pre-existing row with cells[2]==target_domain must not suppress anomaly repair.

    Regression for H1 (Codex review): _find_remapped_outgoing_cells must check that
    the prose_fragment is absent before firing the idempotence short-circuit. Without
    this check, a legitimate row already pointing to the target domain causes the
    anomaly row to never be repaired on the first call.
    """
    from tests.unit.server.services.test_dep_map_911_builders import (
        make_domains_json_911,
        make_executor_911,
        make_garbage_domain_anomaly,
        make_target_domain_file,
    )

    prose = "order-service legacy payment hook"
    stem = "domain-a"
    target = "order-fulfillment"

    # Two outgoing rows: a legitimate row already pointing to target, plus the anomaly row.
    content = (
        f"---\nname: {stem}\n---\n\n## Dependencies\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        f"| service-a | other-api | {target} | Sync | pre-existing | ref-1 |\n"
        f"| service-a | order-api | {prose} | Service integration | legacy | ticket-42 |\n\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
    )
    (tmp_path / f"{stem}.md").write_text(content, encoding="utf-8")
    make_target_domain_file(tmp_path, target)
    make_domains_json_911(
        tmp_path, [{"name": target, "participating_repos": ["order-service"]}]
    )

    anomaly = make_garbage_domain_anomaly(f"{stem}.md", prose)
    executor = make_executor_911()
    errors: list = []

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, [], errors)

    result = (tmp_path / f"{stem}.md").read_text()
    assert prose not in result, "anomaly row must be rewritten"
    assert "pre-existing" in result, "legitimate row must be left intact"
    assert errors == []
