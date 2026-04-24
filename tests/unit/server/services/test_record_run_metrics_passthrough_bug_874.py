"""
Unit tests for Bug #874 Story B: service-layer pass-through.

Verifies that DependencyMapService._record_run_metrics() accepts and forwards
run_type and phase_timings_json to the tracking backend:
  1. Explicit kwargs are forwarded (backend called with run_type="delta", ...).
  2. Legacy call (no new kwargs) still works; backend called with run_type=None,
     phase_timings_json=None.

Only the injected tracking_backend collaborator is mocked — internal service
logic runs real code.

Design note: DependencyMapService is constructed via __new__ so the helper
method can be exercised in isolation without wiring the full constructor chain.
__new__ returns DependencyMapService, so no type: ignore is needed at call sites.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

from code_indexer.server.services.dependency_map_service import DependencyMapService


def _invoke_helper(
    tmp_path: Path,
    run_type: Optional[str] = None,
    phase_timings_json: Optional[str] = None,
) -> Any:
    """Centralised setup and call for _record_run_metrics tests.

    Creates a DependencyMapService with only _tracking_backend injected,
    prepares a minimal output directory, calls _record_run_metrics with the
    supplied kwargs, and returns the mock backend's call_args for assertion.

    Returns: call_args from mock_backend.record_run_metrics (MagicMock call_args).
    """
    mock_backend = MagicMock()
    service: DependencyMapService = DependencyMapService.__new__(DependencyMapService)
    # _tracking_backend is the only collaborator _record_run_metrics uses.
    service._tracking_backend = mock_backend  # noqa: SLF001

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "auth.md").write_text("content")

    service._record_run_metrics(
        output_dir,
        [{"name": "auth"}],
        [{"alias": "repo1"}],
        pass1_duration_s=1.0,
        pass2_duration_s=2.0,
        run_type=run_type,
        phase_timings_json=phase_timings_json,
    )

    mock_backend.record_run_metrics.assert_called_once()
    return mock_backend.record_run_metrics.call_args


class TestRecordRunMetricsPassthroughBug874:
    """_record_run_metrics must forward run_type and phase_timings_json to backend."""

    def test_explicit_kwargs_forwarded_to_backend(self, tmp_path: Path) -> None:
        """run_type and phase_timings_json reach tracking_backend.record_run_metrics."""
        call_args = _invoke_helper(
            tmp_path,
            run_type="delta",
            phase_timings_json='{"detect_s":1.0,"merge_s":2.0}',
        )
        assert call_args.kwargs.get("run_type") == "delta", (
            f"run_type not forwarded: {call_args}"
        )
        assert (
            call_args.kwargs.get("phase_timings_json")
            == '{"detect_s":1.0,"merge_s":2.0}'
        ), f"phase_timings_json not forwarded: {call_args}"

    def test_legacy_call_without_new_kwargs_passes_none(self, tmp_path: Path) -> None:
        """Omitting new kwargs passes run_type=None, phase_timings_json=None to backend."""
        call_args = _invoke_helper(tmp_path)
        assert call_args.kwargs.get("run_type") is None, (
            f"Expected run_type=None for legacy call: {call_args}"
        )
        assert call_args.kwargs.get("phase_timings_json") is None, (
            f"Expected phase_timings_json=None for legacy call: {call_args}"
        )
