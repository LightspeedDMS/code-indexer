"""
Story #910 AC4: Idempotent re-emit — second call is a no-op.

After a successful repair, running _repair_malformed_yaml again must not
write the file again, must not add entries to fixed[], and must log
a "no-op" message specifically on the second (not first) call.

TestAC4Idempotent (2 methods):
  test_second_repair_produces_no_file_write
    Uses extract_body_bytes for byte-level idempotency assertion.
  test_second_repair_log_contains_noop_message
    Asserts first-repair logs do NOT contain no-op; second-repair delta DOES.

Shared setup via ac4_fixture (returns output_dir, anomaly).
"""

from pathlib import Path
from typing import List, Tuple, TYPE_CHECKING

import pytest

from tests.unit.server.services.test_dep_map_910_builders import (
    make_domains_json,
    make_executor_910,
    make_malformed_domain_file,
    make_malformed_yaml_anomaly,
)
from tests.unit.server.services.test_dep_map_910_helpers import (
    extract_body_bytes,
    run_repair_and_read,
)

if TYPE_CHECKING:
    from tests.unit.server.services.test_dep_map_910_builders import AnomalyEntry

_DOMAIN_INFO = {
    "name": "domain-z",
    "last_analyzed": "2024-06-01T12:00:00",
    "participating_repos": ["repo-x", "repo-y"],
}


@pytest.fixture()
def ac4_fixture(tmp_path) -> Tuple[Path, "AnomalyEntry"]:
    """Return (output_dir, anomaly) with domain file and _domains.json set up."""
    output_dir = tmp_path / "dependency-map"
    output_dir.mkdir(parents=True, exist_ok=True)
    make_malformed_domain_file(output_dir, "domain-z")
    make_domains_json(output_dir, [_DOMAIN_INFO])
    anomaly = make_malformed_yaml_anomaly("domain-z.md")
    return output_dir, anomaly


class TestAC4Idempotent:
    """AC4: second _repair_malformed_yaml call on already-repaired file is a no-op."""

    def test_second_repair_produces_no_file_write(self, ac4_fixture):
        """Second repair: fixed[] empty, file and body bytes unchanged."""
        output_dir, anomaly = ac4_fixture
        executor = make_executor_910()
        md_path = output_dir / "domain-z.md"

        # First repair — establishes repaired state
        run_repair_and_read(output_dir, executor, anomaly, "domain-z")
        full_bytes_after_first = md_path.read_bytes()
        body_bytes_after_first = extract_body_bytes(full_bytes_after_first)

        # Second repair — must be a no-op
        fixed: List[str] = []
        errors: List[str] = []
        executor._repair_malformed_yaml(output_dir, anomaly, fixed, errors)

        assert not errors, f"Unexpected errors on second repair: {errors}"
        assert fixed == [], f"fixed[] must be empty on no-op, got: {fixed}"
        assert md_path.read_bytes() == full_bytes_after_first, (
            "File bytes changed on idempotent second repair"
        )
        assert extract_body_bytes(md_path.read_bytes()) == body_bytes_after_first, (
            "Body bytes changed on idempotent second repair"
        )

    def test_second_repair_log_contains_noop_message(self, ac4_fixture):
        """No-op log appears only in second-repair delta, not first-repair logs."""
        output_dir, anomaly = ac4_fixture
        log_messages: List[str] = []
        executor = make_executor_910(journal_callback=log_messages.append)

        # First repair — success; must NOT produce a no-op log
        executor._repair_malformed_yaml(output_dir, anomaly, [], [])
        logs_after_first = list(log_messages)
        first_noop_logs = [
            m for m in logs_after_first if "no-op" in m.lower() and "domain-z" in m
        ]
        assert first_noop_logs == [], (
            f"First repair must NOT emit no-op log, but got: {first_noop_logs}"
        )

        # Second repair — same file already repaired; must produce no-op log
        executor._repair_malformed_yaml(output_dir, anomaly, [], [])
        second_repair_logs = log_messages[len(logs_after_first) :]
        noop_logs = [
            m for m in second_repair_logs if "no-op" in m.lower() and "domain-z" in m
        ]
        assert noop_logs, (
            f"Expected no-op log from second repair with 'domain-z'.\n"
            f"Second-repair logs: {second_repair_logs}"
        )
