"""
Unit tests for Bug #874 Story B: record_run_metrics / get_run_history round-trip.

Verifies that the two new optional kwargs (run_type, phase_timings_json)
are stored and returned correctly for three cases:
  1. Delta run: run_type="delta", phase_timings_json='{"detect_s":1.5,"merge_s":2.5}'
  2. Legacy-compat: no new kwargs -> stored and returned as None
  3. Full run:  run_type="full", phase_timings_json='{"synth_s":10,"per_domain_s":200}'

No mocks — real SQLite via DependencyMapTrackingBackend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, cast


def _base_metrics() -> dict[str, object]:
    """Return a minimal valid metrics dict (all required existing fields)."""
    return {
        "timestamp": "2026-04-23T00:00:00+00:00",
        "domain_count": 3,
        "total_chars": 1000,
        "edge_count": 5,
        "zero_char_domains": 0,
        "repos_analyzed": 2,
        "repos_skipped": 0,
        "pass1_duration_s": 1.0,
        "pass2_duration_s": 2.0,
    }


def _round_trip(
    tmp_path: Path,
    db_name: str,
    *,
    run_type: Optional[str] = None,
    phase_timings_json: Optional[str] = None,
) -> dict[str, object]:
    """Create backend, record one row with given kwargs, fetch and return that row."""
    from code_indexer.server.storage.sqlite_backends import (
        DependencyMapTrackingBackend,
    )

    db_path = str(tmp_path / db_name)
    backend = DependencyMapTrackingBackend(db_path)
    try:
        backend.record_run_metrics(
            _base_metrics(),
            run_type=run_type,
            phase_timings_json=phase_timings_json,
        )
        rows = backend.get_run_history(limit=1)
        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
        # cast needed: get_run_history returns list[Any] (SQLite rows are untyped)
        return cast(dict[str, object], rows[0])
    finally:
        backend.close()


class TestRunHistoryRoundTripBug874:
    """record_run_metrics + get_run_history must round-trip the two new fields."""

    def test_delta_run_type_and_phase_timings_round_trip(self, tmp_path: Path) -> None:
        """run_type='delta' and phase_timings_json are stored and returned."""
        row = _round_trip(
            tmp_path,
            "delta.db",
            run_type="delta",
            phase_timings_json='{"detect_s":1.5,"merge_s":2.5}',
        )
        assert row["run_type"] == "delta", (
            f"Expected run_type='delta', got {row['run_type']!r}"
        )
        assert row["phase_timings_json"] == '{"detect_s":1.5,"merge_s":2.5}', (
            f"Expected phase_timings_json round-trip, got {row['phase_timings_json']!r}"
        )

    def test_legacy_compat_none_none_stored_as_null(self, tmp_path: Path) -> None:
        """Legacy call with no new kwargs stores and returns None for both fields."""
        row = _round_trip(tmp_path, "legacy.db")
        assert row["run_type"] is None, (
            f"Expected run_type=None for legacy call, got {row['run_type']!r}"
        )
        assert row["phase_timings_json"] is None, (
            f"Expected phase_timings_json=None for legacy call, got {row['phase_timings_json']!r}"
        )

    def test_full_run_type_and_phase_timings_round_trip(self, tmp_path: Path) -> None:
        """run_type='full' and full-run phase_timings_json are stored and returned."""
        row = _round_trip(
            tmp_path,
            "full.db",
            run_type="full",
            phase_timings_json='{"synth_s":10,"per_domain_s":200}',
        )
        assert row["run_type"] == "full", (
            f"Expected run_type='full', got {row['run_type']!r}"
        )
        assert row["phase_timings_json"] == '{"synth_s":10,"per_domain_s":200}', (
            f"Expected phase_timings_json round-trip, got {row['phase_timings_json']!r}"
        )
