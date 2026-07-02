"""
Story #908 AC3: SELF_LOOP anomaly located, deleted, journaled.

Tests verify:
- Self-loop row removed; surrounding rows (domain-b, domain-c) preserved
- fixed[] receives a "Phase 3.7" entry after repair
- Journal receives exactly one entry with action="self_loop_deleted"
"""

import json
from typing import List

import pytest

from tests.unit.server.services.test_dep_map_908_builders import (
    make_executor,
    make_self_loop_anomaly,
)
from tests.unit.server.services.test_dep_map_908_helpers import (
    make_domain_with_self_loop,
)


@pytest.fixture()
def single_self_loop_fixture(tmp_path):
    """Return (output_dir, journal_path, executor, anomaly) for domain-a with self-loop."""
    output_dir = tmp_path / "dependency-map"
    output_dir.mkdir(parents=True, exist_ok=True)
    make_domain_with_self_loop(
        output_dir, "domain-a", ["domain-b", "domain-a", "domain-c"]
    )
    import json as _json

    (output_dir / "_domains.json").write_text(
        _json.dumps([{"name": "domain-a", "participating_repos": ["repo-a"]}]),
        encoding="utf-8",
    )
    journal_path = tmp_path / "journal.jsonl"
    executor = make_executor(enable_graph_channel_repair=True)
    anomaly = make_self_loop_anomaly("domain-a")
    return output_dir, journal_path, executor, anomaly


class TestAC3SelfLoopRepair:
    """AC3: Single SELF_LOOP row removed, surrounding rows preserved."""

    def test_self_loop_row_removed_surrounding_rows_preserved(
        self, single_self_loop_fixture
    ):
        """domain-a self-loop removed; domain-b and domain-c rows stay intact."""
        output_dir, journal_path, executor, anomaly = single_self_loop_fixture

        executor._repair_self_loop(
            output_dir, anomaly, [], [], journal_path=journal_path
        )

        content = (output_dir / "domain-a.md").read_text(encoding="utf-8")
        # Parse outgoing table target column (index 2 after splitting on |)
        outgoing_targets = [
            cells[3].strip()
            for line in content.splitlines()
            if line.startswith("| repo-a |")
            for cells in [line.split("|")]
            if len(cells) > 3
        ]
        assert "domain-a" not in outgoing_targets, (
            f"Self-loop row still present. Targets: {outgoing_targets}"
        )
        assert "domain-b" in outgoing_targets, (
            f"domain-b row was incorrectly removed. Targets: {outgoing_targets}"
        )
        assert "domain-c" in outgoing_targets, (
            f"domain-c row was incorrectly removed. Targets: {outgoing_targets}"
        )

    def test_self_loop_repair_adds_fixed_entry(self, single_self_loop_fixture):
        """fixed[] receives a 'Phase 3.7' entry containing the domain file name."""
        output_dir, journal_path, executor, anomaly = single_self_loop_fixture

        fixed: List[str] = []
        executor._repair_self_loop(
            output_dir, anomaly, fixed, [], journal_path=journal_path
        )

        assert len(fixed) == 1, f"Expected 1 fixed entry. Got: {fixed}"
        assert "Phase 3.7" in fixed[0], f"'Phase 3.7' prefix missing: {fixed[0]!r}"
        assert "domain-a" in fixed[0], (
            f"Domain name missing in fixed entry: {fixed[0]!r}"
        )

    def test_self_loop_repair_appends_journal_entry(self, single_self_loop_fixture):
        """Journal receives exactly one entry with action=self_loop_deleted."""
        output_dir, journal_path, executor, anomaly = single_self_loop_fixture

        executor._repair_self_loop(
            output_dir, anomaly, [], [], journal_path=journal_path
        )

        assert journal_path.exists(), "Journal file not created after repair"
        lines = journal_path.read_text().strip().splitlines()
        assert len(lines) == 1, f"Expected 1 journal line. Got: {len(lines)}"
        entry = json.loads(lines[0])
        assert entry["action"] == "self_loop_deleted", (
            f"Wrong action: {entry['action']!r}"
        )
        assert entry["anomaly_type"] == "SELF_LOOP", (
            f"Wrong anomaly_type: {entry['anomaly_type']!r}"
        )
