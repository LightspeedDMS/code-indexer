"""
Bug #1388 (remediation): run_with_popen_progress must scrape a real child
subprocess's stderr for HNSW_ORPHAN_REPAIR_MARKER-prefixed lines and forward
them to a dedicated `orphan_event_callback` parameter -- entirely separate
from the percentage `progress_callback`/`_emit` channel, which applies a
monotonic high-water-mark suppression that would otherwise silently drop a
`total=0`-mapped event once the phase is nearly complete (the mechanism
that sank the first, rejected #1388 attempt).

These tests spawn a REAL subprocess (sys.executable -c <script>, matching
this module's own existing test conventions) and drive the REAL
run_with_popen_progress + its REAL `_emit` monotonic-guard logic -- not a
hand-rolled bridge or a strict-mock substitute.
"""

import sys

from code_indexer.services.progress_subprocess_runner import (
    run_with_popen_progress,
)
from code_indexer.services.progress_phase_allocator import ProgressPhaseAllocator
from code_indexer.storage.hnsw_index_manager import HNSW_ORPHAN_REPAIR_MARKER

_MARKER_LINE = (
    f"{HNSW_ORPHAN_REPAIR_MARKER}: context=rebuild_from_vectors:/x/y "
    f"orphan_count=3 repaired=true"
)


def _allocator() -> ProgressPhaseAllocator:
    allocator = ProgressPhaseAllocator()
    allocator.calculate_weights(
        index_types=["semantic"], file_count=100, commit_count=0
    )
    return allocator


def test_orphan_event_callback_receives_marker_from_real_subprocess_stderr():
    """A real child process that writes the marker to stderr (and ordinary
    JSON progress to stdout) must have the marker line forwarded to
    orphan_event_callback, while progress_callback keeps receiving the
    normal percentage-mapped events unaffected.
    """
    script = (
        "import json, sys\n"
        'print(json.dumps({"current": 2, "total": 4, "info": "mid"}))\n'
        f"print({_MARKER_LINE!r}, file=sys.stderr, flush=True)\n"
        'print(json.dumps({"current": 4, "total": 4, "info": "done"}))\n'
    )
    command = [sys.executable, "-c", script]

    progress_calls = []
    orphan_events: list[str] = []

    run_with_popen_progress(
        command=command,
        phase_name="semantic",
        allocator=_allocator(),
        progress_callback=lambda pct, phase=None, detail=None: progress_calls.append(
            pct
        ),
        all_stdout=[],
        all_stderr=[],
        cwd=None,
        orphan_event_callback=orphan_events.append,
    )

    assert len(progress_calls) >= 2, (
        f"expected normal progress calls, got: {progress_calls}"
    )
    assert orphan_events == [_MARKER_LINE], (
        f"expected the marker line forwarded exactly once to "
        f"orphan_event_callback, got: {orphan_events}"
    )


def test_orphan_event_callback_fires_even_when_monotonic_guard_suppresses_percentage_channel():
    """Bug #1388 Finding 2 proof: HNSW finalize happens at the END of a
    phase, when the parent's monotonic high-water mark is already near
    range_end -- the exact condition that silently swallowed the first,
    rejected implementation's total=0 marker on the percentage channel.
    Setting last_reported=99 reproduces that condition against the REAL
    `_emit` monotonic guard: every percentage-channel call (including the
    phase-start marker) must be suppressed, while orphan_event_callback
    -- a separate, non-monotonic channel -- must still fire.
    """
    script = f"import sys\nprint({_MARKER_LINE!r}, file=sys.stderr, flush=True)\n"
    command = [sys.executable, "-c", script]

    progress_calls = []
    orphan_events: list[str] = []

    run_with_popen_progress(
        command=command,
        phase_name="semantic",
        allocator=_allocator(),
        progress_callback=lambda pct, phase=None, detail=None: progress_calls.append(
            pct
        ),
        all_stdout=[],
        all_stderr=[],
        cwd=None,
        last_reported=99,
        orphan_event_callback=orphan_events.append,
    )

    assert progress_calls == [], (
        f"expected the monotonic guard to suppress every percentage-channel "
        f"call at last_reported=99, but got: {progress_calls}"
    )
    assert orphan_events == [_MARKER_LINE], (
        f"expected the marker to survive the monotonic-guard-suppressed "
        f"scenario via the separate orphan_event_callback channel, got: "
        f"{orphan_events}"
    )
