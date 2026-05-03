"""
Story #908 AC4: Multiple SELF_LOOPs across different domains repaired in single run.

Tests verify:
- _run_phase37 repairs all 3 domains with self-loops in one call
- _repair_self_loop is idempotent: second call produces no repair effect
"""

import json
import os
from typing import List
from unittest.mock import patch

import pytest

from tests.unit.server.services.test_dep_map_908_builders import (
    make_executor,
    make_self_loop_anomaly,
)
from tests.unit.server.services.test_dep_map_908_helpers import (
    make_domain_with_self_loop,
)


def _outgoing_targets(content: str) -> List[str]:
    """Parse target domain names from the Outgoing Dependencies table in a domain .md."""
    return [
        cells[3].strip()
        for line in content.splitlines()
        if line.startswith("| repo-a |")
        for cells in [line.split("|")]
        if len(cells) > 3
    ]


@pytest.fixture()
def three_domain_self_loop_fixture(tmp_path):
    """Dep-map with self-loops in domain-x, domain-y, domain-z.

    Returns (output_dir, journal_dir) ready for _run_phase37 execution.
    """
    output_dir = tmp_path / "dependency-map"
    output_dir.mkdir(parents=True, exist_ok=True)
    for dn in ("domain-x", "domain-y", "domain-z"):
        make_domain_with_self_loop(output_dir, dn, [dn, "domain-other"])
    domains = [{"name": dn} for dn in ("domain-x", "domain-y", "domain-z")]
    (output_dir / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")
    journal_dir = tmp_path / "journal-dir"
    journal_dir.mkdir()
    return output_dir, journal_dir


class TestAC4MultipleSelfLoops:
    """AC4: Multiple self-loops across files — all repaired in one Phase 3.7 call."""

    def test_three_domains_all_repaired_in_single_phase37_call(
        self, three_domain_self_loop_fixture
    ):
        """_run_phase37 repairs all 3 self-loop domains; journal has 3 entries."""
        output_dir, journal_dir = three_domain_self_loop_fixture
        executor = make_executor(enable_graph_channel_repair=True)

        fixed: List[str] = []
        errors: List[str] = []
        with patch.dict(os.environ, {"CIDX_DATA_DIR": str(journal_dir)}):
            executor._run_phase37(output_dir, fixed, errors)

        for dn in ("domain-x", "domain-y", "domain-z"):
            content = (output_dir / f"{dn}.md").read_text(encoding="utf-8")
            targets = _outgoing_targets(content)
            assert dn not in targets, (
                f"Self-loop still present in {dn}.md. Targets: {targets}"
            )
            assert "domain-other" in targets, (
                f"Non-self-loop row removed from {dn}.md. Targets: {targets}"
            )

        journal_path = journal_dir / "dep_map_repair_journal.jsonl"
        assert journal_path.exists(), "Journal file not created"
        lines = journal_path.read_text().strip().splitlines()
        assert len(lines) == 3, f"Expected 3 journal entries. Got: {len(lines)}"
        for line in lines:
            assert json.loads(line)["action"] == "self_loop_deleted"

    def test_repair_is_idempotent(self, tmp_path):
        """Second _repair_self_loop call on already-fixed file is a true no-op."""
        output_dir = tmp_path / "dependency-map"
        output_dir.mkdir(parents=True, exist_ok=True)
        make_domain_with_self_loop(output_dir, "domain-a", ["domain-a"])
        (output_dir / "_domains.json").write_text(
            json.dumps([{"name": "domain-a"}]), encoding="utf-8"
        )
        journal_path = tmp_path / "journal.jsonl"
        executor = make_executor(enable_graph_channel_repair=True)
        anomaly = make_self_loop_anomaly("domain-a")

        # First call — removes the self-loop
        fixed1: List[str] = []
        errors1: List[str] = []
        executor._repair_self_loop(
            output_dir, anomaly, fixed1, errors1, journal_path=journal_path
        )
        assert len(fixed1) == 1, f"First call should fix. Got fixed={fixed1}"
        assert errors1 == [], f"First call should not error. Got errors={errors1}"
        lines_after_first = journal_path.read_text().strip().splitlines()
        assert len(lines_after_first) == 1, (
            f"Expected 1 journal entry after first call. Got: {len(lines_after_first)}"
        )

        # Second call — already removed; must produce no repair effect
        fixed2: List[str] = []
        errors2: List[str] = []
        executor._repair_self_loop(
            output_dir, anomaly, fixed2, errors2, journal_path=journal_path
        )
        assert errors2 == [], f"Second call must not error. Got: {errors2}"
        assert fixed2 == [], f"Second call must not add to fixed[]. Got: {fixed2}"

        # Journal must not have gained extra entries
        lines_after_second = journal_path.read_text().strip().splitlines()
        assert len(lines_after_second) == len(lines_after_first), (
            f"Journal grew after idempotent re-run: "
            f"{len(lines_after_first)} -> {len(lines_after_second)}"
        )

        # File must still have no self-loop
        content = (output_dir / "domain-a.md").read_text(encoding="utf-8")
        assert "domain-a" not in _outgoing_targets(content), (
            "Self-loop reintroduced after idempotent re-run"
        )
