"""
Tests for Bug 1B: Monotonic progress guard in run_with_popen_progress.

Problem: When a second phase starts, phase_start returns the beginning of that
phase's range (e.g., 30%), which may be LOWER than the last reported value from
the first phase (e.g., 90%). This causes visible progress regressions in the UI.

Fix: run_with_popen_progress must track the last reported progress value and
never call progress_callback with a value lower than the last reported.
"""
import json
import sys
import tempfile
from pathlib import Path
from typing import List

import pytest


class TestMonotonicProgressGuard:
    """Verify run_with_popen_progress never reports decreasing progress."""

    def test_phase_start_does_not_regress_below_last_reported(self):
        """
        The phase_start emission at the beginning of run_with_popen_progress
        must be suppressed if it is lower than the last value already reported.

        Scenario: caller reports 90% (end of phase A), then calls
        run_with_popen_progress for phase B whose range starts at 30%.
        The 30% must NOT be emitted since it would cause a visible regression.

        This is the core monotonic guard requirement: the function must accept
        a `last_reported` parameter (or equivalent) and skip any call that would
        be lower.
        """
        from code_indexer.services.progress_subprocess_runner import (
            run_with_popen_progress,
        )
        from code_indexer.services.progress_phase_allocator import ProgressPhaseAllocator

        allocator = ProgressPhaseAllocator()
        # Two phases: semantic (0-60%) and fts (60-90%) + cow appended automatically
        allocator.calculate_weights(
            index_types=["semantic", "fts"],
            file_count=100,
            commit_count=0,
        )

        received: List[int] = []

        def cb(pct, phase=None, detail=None):
            received.append(pct)

        # Script emits progress that maps to near-end of semantic phase
        script = (
            "import json, sys\n"
            'print(json.dumps({"current": 100, "total": 100, "info": "done"}))\n'
        )
        command = [sys.executable, "-c", script]

        # First call: semantic phase ends near its phase_end
        run_with_popen_progress(
            command=command,
            phase_name="semantic",
            allocator=allocator,
            progress_callback=cb,
            all_stdout=[],
            all_stderr=[],
            cwd=None,
        )

        last_after_semantic = max(received) if received else 0

        # Second call: fts phase starts — phase_start("fts") > semantic values
        # but if semantic went to 100% of its range and fts starts fresh, the
        # guard should prevent any decrease
        script2 = (
            "import json, sys\n"
            'print(json.dumps({"current": 0, "total": 100, "info": "starting"}))\n'
            'print(json.dumps({"current": 50, "total": 100, "info": "mid"}))\n'
        )
        command2 = [sys.executable, "-c", script2]
        received_before_fts = len(received)

        run_with_popen_progress(
            command=command2,
            phase_name="fts",
            allocator=allocator,
            progress_callback=cb,
            all_stdout=[],
            all_stderr=[],
            cwd=None,
        )

        # After fts phase, no value should be lower than the last value from semantic
        fts_values = received[received_before_fts:]
        for val in fts_values:
            assert val >= last_after_semantic, (
                f"Monotonic guard violated: fts emitted {val} which is lower than "
                f"last semantic value {last_after_semantic}. "
                f"Full sequence: {received}"
            )

    def test_within_single_phase_progress_never_decreases(self):
        """
        Within a single phase, the emitted values from JSON lines are already
        expected to be monotonically increasing (0->100%). The guard must not
        interfere with valid increasing sequences.
        """
        from code_indexer.services.progress_subprocess_runner import (
            run_with_popen_progress,
        )
        from code_indexer.services.progress_phase_allocator import ProgressPhaseAllocator

        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=["semantic"],
            file_count=100,
            commit_count=0,
        )

        received: List[int] = []

        def cb(pct, phase=None, detail=None):
            received.append(pct)

        script = (
            "import json, sys\n"
            'print(json.dumps({"current": 0, "total": 4}))\n'
            'print(json.dumps({"current": 1, "total": 4}))\n'
            'print(json.dumps({"current": 2, "total": 4}))\n'
            'print(json.dumps({"current": 3, "total": 4}))\n'
            'print(json.dumps({"current": 4, "total": 4}))\n'
        )
        command = [sys.executable, "-c", script]

        run_with_popen_progress(
            command=command,
            phase_name="semantic",
            allocator=allocator,
            progress_callback=cb,
            all_stdout=[],
            all_stderr=[],
            cwd=None,
        )

        # All values should be non-decreasing
        for i in range(1, len(received)):
            assert received[i] >= received[i - 1], (
                f"Progress decreased at index {i}: {received[i-1]} -> {received[i]}. "
                f"Full sequence: {received}"
            )

    def test_phase_start_suppressed_when_below_last_reported(self):
        """
        When run_with_popen_progress is called with a last_reported value
        greater than phase_start, the phase_start callback must be suppressed.

        This tests the new `last_reported` parameter that enables the caller
        to pass the current high-water mark.
        """
        from code_indexer.services.progress_subprocess_runner import (
            run_with_popen_progress,
        )
        from code_indexer.services.progress_phase_allocator import ProgressPhaseAllocator

        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=["semantic", "fts"],
            file_count=100,
            commit_count=0,
        )

        received: List[int] = []

        def cb(pct, phase=None, detail=None):
            received.append(pct)

        # semantic phase_end is roughly 60% (before fts starts)
        # Simulate: last_reported = 55 (mid-semantic), now starting fts which starts at ~60
        # phase_start("fts") should be > 55 so it should NOT be suppressed
        # But if we simulate last_reported = 80 and fts starts at 60, it SHOULD be suppressed
        fts_start = int(allocator.phase_start("fts"))

        # Set last_reported higher than fts_start to trigger suppression
        last_reported_high = fts_start + 20  # definitely above fts start

        script = 'import json; print(json.dumps({"current": 100, "total": 100}))'
        command = [sys.executable, "-c", script]

        run_with_popen_progress(
            command=command,
            phase_name="fts",
            allocator=allocator,
            progress_callback=cb,
            all_stdout=[],
            all_stderr=[],
            cwd=None,
            last_reported=last_reported_high,
        )

        # The phase_start value (which is fts_start < last_reported_high)
        # must NOT appear in received
        for val in received:
            assert val >= last_reported_high or val == fts_start + (
                int(allocator.phase_end("fts")) - fts_start
            ), (
                f"Expected no value below last_reported={last_reported_high}, "
                f"but got {val}. Full received: {received}"
            )
        # More precisely: no value should be below last_reported_high
        below = [v for v in received if v < last_reported_high]
        assert len(below) == 0, (
            f"Monotonic guard failed: values below last_reported={last_reported_high}: "
            f"{below}. Full sequence: {received}"
        )
