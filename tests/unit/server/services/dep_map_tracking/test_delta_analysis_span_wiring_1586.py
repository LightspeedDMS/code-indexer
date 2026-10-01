"""Story #1586 AC5: cidx.depmap.run_delta_analysis custom OTEL span wired
into DependencyMapService.run_delta_analysis().

Proves the WIRING -- a real call into run_delta_analysis() emits a real
OTEL span via create_span() -- not just that create_span() works standalone.
Reuses the exact same real-service fixture recipe as the sibling Story #312
job-tracking tests in this directory (make_service / mock_config_manager*
/ job_tracker) -- MESSI Rule #1: no mocks of the code under test itself.
The failure path is forced via the injected mock_analyzer dependency
(run_pass_1_synthesis.side_effect), never by mocking a method on the
DependencyMapService instance under test.
"""

import pytest
from opentelemetry.trace import StatusCode

from tests.unit.server.telemetry.otel_test_support import active_span_exporter

from .conftest import make_service


def _find_span(exporter, name: str):
    for span in exporter.get_finished_spans():
        if span.name == name:
            return span
    return None


class TestDeltaAnalysisSpanSuccess:
    def test_run_delta_analysis_emits_span_when_disabled(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )

        with active_span_exporter() as exporter:
            service.run_delta_analysis()

        span = _find_span(exporter, "cidx.depmap.run_delta_analysis")
        assert span is not None, "cidx.depmap.run_delta_analysis span not emitted"
        assert span.status.status_code == StatusCode.UNSET


class TestDeltaAnalysisSpanFailure:
    def test_run_delta_analysis_failure_records_error_span(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        mock_tracking_backend.get_tracking.side_effect = RuntimeError("DB error")
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )

        with active_span_exporter() as exporter:
            with pytest.raises(RuntimeError, match="DB error"):
                service.run_delta_analysis()

        span = _find_span(exporter, "cidx.depmap.run_delta_analysis")
        assert span is not None, "cidx.depmap.run_delta_analysis span not emitted"
        assert span.status.status_code == StatusCode.ERROR
        assert len(span.events) >= 1, "exception must be recorded on the span"
        assert span.events[0].name == "exception"
