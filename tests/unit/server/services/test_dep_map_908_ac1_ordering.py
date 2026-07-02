"""
Story #908 AC1: Phase 3.7 skeleton runs between Phase 3.5 and Phase 4.

Tests verify:
- Progress event at 78% fires between 75% (Phase 3.5) and 80% (Phase 4)
- Progress info string contains "Phase 3.7"
- Phase 3.7 is a no-op on a clean fixture (no journal writes, no fixed/errors)
"""

import os
from typing import List
from unittest.mock import patch

import pytest

from tests.unit.server.services.test_dep_map_908_builders import make_executor
from tests.unit.server.services.test_dep_map_908_helpers import make_minimal_dep_map

# Named progress constants — matches the values in _run_branch_a_dep_map.
PHASE35_PROGRESS = 75
PHASE37_PROGRESS = 78
PHASE4_PROGRESS = 80
_PHASE_WINDOW_LOW = 70
_PHASE_WINDOW_HIGH = 85


@pytest.fixture()
def progress_events(tmp_path):
    """Run _run_branch_a_dep_map on a clean fixture and return captured progress events."""
    output_dir = tmp_path / "dependency-map"
    make_minimal_dep_map(output_dir)

    events: List[tuple] = []
    executor = make_executor(
        enable_graph_channel_repair=True,
        progress_callback=lambda p, i: events.append((p, i)),
    )

    from code_indexer.server.services.dep_map_health_detector import (
        DepMapHealthDetector,
    )

    health_report = DepMapHealthDetector().detect(output_dir)
    executor._run_branch_a_dep_map(output_dir, health_report, [], [])
    return events


class TestAC1Phase37Ordering:
    """AC1: Phase 3.7 inserts between Phase 3.5 and Phase 4."""

    def test_phase37_progress_event_fires_between_phase35_and_phase4(
        self, progress_events
    ):
        """Progress events appear in correct order: 75 (3.5) -> 78 (3.7) -> 80 (4)."""
        phase_pcts = [
            pct
            for pct, _ in progress_events
            if _PHASE_WINDOW_LOW <= pct <= _PHASE_WINDOW_HIGH
        ]
        assert PHASE35_PROGRESS in phase_pcts, (
            f"Phase 3.5 ({PHASE35_PROGRESS}%) missing. Got: {phase_pcts}"
        )
        assert PHASE37_PROGRESS in phase_pcts, (
            f"Phase 3.7 ({PHASE37_PROGRESS}%) missing. Got: {phase_pcts}"
        )
        assert PHASE4_PROGRESS in phase_pcts, (
            f"Phase 4 ({PHASE4_PROGRESS}%) missing. Got: {phase_pcts}"
        )

        idx_35 = phase_pcts.index(PHASE35_PROGRESS)
        idx_37 = phase_pcts.index(PHASE37_PROGRESS)
        idx_4 = phase_pcts.index(PHASE4_PROGRESS)
        assert idx_35 < idx_37 < idx_4, (
            f"Ordering wrong: {PHASE35_PROGRESS}@{idx_35}, "
            f"{PHASE37_PROGRESS}@{idx_37}, {PHASE4_PROGRESS}@{idx_4}"
        )

    def test_phase37_progress_info_string_correct(self, progress_events):
        """Progress event at 78% carries 'Phase 3.7' in its info string."""
        phase37_events = [(p, i) for p, i in progress_events if p == PHASE37_PROGRESS]
        assert len(phase37_events) == 1, (
            f"Expected one {PHASE37_PROGRESS}% event. Got: {phase37_events}"
        )
        assert "Phase 3.7" in phase37_events[0][1], (
            f"Expected 'Phase 3.7' in info. Got: {phase37_events[0][1]!r}"
        )

    def test_phase37_noop_on_clean_fixture(self, tmp_path):
        """Clean fixture: fixed[], errors[] unchanged and no journal written."""
        output_dir = tmp_path / "dependency-map"
        make_minimal_dep_map(output_dir)

        journal_dir = tmp_path / "journal-dir"
        journal_dir.mkdir()
        journal_path = journal_dir / "dep_map_repair_journal.jsonl"

        executor = make_executor(enable_graph_channel_repair=True)
        fixed = ["pre-existing"]
        errors = ["pre-existing-error"]

        with patch.dict(os.environ, {"CIDX_DATA_DIR": str(journal_dir)}):
            executor._run_phase37(output_dir, fixed, errors)

        assert fixed == ["pre-existing"], f"fixed[] was modified: {fixed}"
        assert errors == ["pre-existing-error"], f"errors[] was modified: {errors}"
        assert not journal_path.exists() or journal_path.read_text() == "", (
            "Journal must not be written for a clean fixture"
        )
