"""Story #1586 AC2: shared embedding-provider telemetry helper.

record_embedding_provider_call() is the single, reused wiring point every
embedding provider client (voyage_ai.py, cohere_embedding.py,
cohere_multimodal.py, voyage_multimodal.py) calls exactly once per real
outbound HTTP embedding request. This test proves the helper itself: it
records a real cidx.embedding.* metric when ApplicationMetrics is active,
it never invokes the token-counting callable when metrics are inactive
(no telemetry-only tokenizer overhead), and it never raises even when the
metrics call itself would fail.
"""

from __future__ import annotations

import pytest

from code_indexer.services.embedding_metrics_telemetry import (
    record_embedding_provider_call,
    time_and_record_embedding_call,
)

from tests.unit.server.telemetry.otel_test_support import (
    active_application_metrics_singleton,
    find_metric,
)
from code_indexer.server.telemetry.metrics_instrumentation import (
    reset_application_metrics,
)
from code_indexer.server.telemetry.manager import (
    peek_telemetry_manager,
    reset_telemetry_manager,
)


class TestRecordEmbeddingProviderCallActive:
    def test_success_records_all_three_embedding_metrics(self):
        with active_application_metrics_singleton() as (_metrics, reader):
            record_embedding_provider_call(
                model="voyage-code-3",
                duration_seconds=0.42,
                status="success",
                count_tokens=lambda: 17,
            )

            requests_metric = find_metric(reader, "cidx.embedding.requests")
            assert requests_metric is not None
            dp = list(requests_metric.data.data_points)[0]
            assert dp.value == 1
            assert dp.attributes["model"] == "voyage-code-3"
            assert dp.attributes["status"] == "success"

            tokens_metric = find_metric(reader, "cidx.embedding.tokens")
            assert tokens_metric is not None
            tokens_dp = list(tokens_metric.data.data_points)[0]
            assert tokens_dp.value == 17
            assert tokens_dp.attributes["model"] == "voyage-code-3"
            assert tokens_dp.attributes["status"] == "success"

            duration_metric = find_metric(reader, "cidx.embedding.duration")
            assert duration_metric is not None
            duration_dps = list(duration_metric.data.data_points)
            assert len(duration_dps) == 1
            assert duration_dps[0].attributes["model"] == "voyage-code-3"
            assert duration_dps[0].attributes["status"] == "success"
            assert duration_dps[0].sum == 0.42

    def test_count_tokens_not_invoked_when_metrics_inactive(self):
        """When ApplicationMetrics is inactive (telemetry disabled), the
        token-counting callable must never run -- no tokenizer overhead
        purely for metrics purposes on a disabled deployment."""
        reset_application_metrics()  # ensures get_application_metrics() builds a fresh, disabled instance
        calls = {"n": 0}

        def _count():
            calls["n"] += 1
            return 99

        try:
            # No active_application_metrics_singleton() context here -- the
            # process-wide singleton (freshly reset) is disabled by default.
            record_embedding_provider_call(
                model="voyage-code-3",
                duration_seconds=0.1,
                status="success",
                count_tokens=_count,
            )

            assert calls["n"] == 0
        finally:
            reset_application_metrics()

    def test_error_status_recorded(self):
        with active_application_metrics_singleton() as (_metrics, reader):
            record_embedding_provider_call(
                model="cohere-embed-v3",
                duration_seconds=0.05,
                status="error",
                count_tokens=lambda: 5,
            )

            requests_metric = find_metric(reader, "cidx.embedding.requests")
            assert requests_metric is not None
            dp = list(requests_metric.data.data_points)[0]
            assert dp.attributes["status"] == "error"


class TestRecordEmbeddingProviderCallNeverRaises:
    def test_never_raises_when_count_tokens_itself_raises(self):
        with active_application_metrics_singleton() as (_metrics, _reader):

            def _boom():
                raise RuntimeError("tokenizer exploded")

            # Must not raise -- telemetry failures never break the embedding
            # call path (this project's documented fail-open contract).
            record_embedding_provider_call(
                model="voyage-code-3",
                duration_seconds=0.1,
                status="success",
                count_tokens=_boom,
            )


class TestTimeAndRecordEmbeddingCall:
    """time_and_record_embedding_call() composes timing + recording around
    a real provider call in one line, for call sites that would otherwise
    need their own try/except (Story #1586 AC2)."""

    def test_success_returns_call_result_and_records_success(self):
        with active_application_metrics_singleton() as (_metrics, reader):
            result = time_and_record_embedding_call(
                model="voyage-code-3",
                count_tokens=lambda: 12,
                call_fn=lambda: {"data": "real-response"},
            )

            assert result == {"data": "real-response"}

            requests_metric = find_metric(reader, "cidx.embedding.requests")
            assert requests_metric is not None
            dp = list(requests_metric.data.data_points)[0]
            assert dp.attributes["status"] == "success"
            assert dp.attributes["model"] == "voyage-code-3"

    def test_exception_propagates_unchanged_and_records_error(self):
        with active_application_metrics_singleton() as (_metrics, reader):

            def _boom():
                raise RuntimeError("real provider call failed")

            with pytest.raises(RuntimeError, match="real provider call failed"):
                time_and_record_embedding_call(
                    model="voyage-code-3",
                    count_tokens=lambda: 5,
                    call_fn=_boom,
                )

            requests_metric = find_metric(reader, "cidx.embedding.requests")
            assert requests_metric is not None
            dp = list(requests_metric.data.data_points)[0]
            assert dp.attributes["status"] == "error"

            tokens_metric = find_metric(reader, "cidx.embedding.tokens")
            assert tokens_metric is not None
            tokens_dp = list(tokens_metric.data.data_points)[0]
            assert tokens_dp.value == 0

    def test_count_tokens_not_invoked_when_call_fn_raises(self):
        with active_application_metrics_singleton() as (_metrics, _reader):
            calls = {"n": 0}

            def _count_tokens():
                calls["n"] += 1
                return 5

            def _boom():
                raise RuntimeError("real provider call failed")

            with pytest.raises(RuntimeError):
                time_and_record_embedding_call(
                    model="voyage-code-3",
                    count_tokens=_count_tokens,
                    call_fn=_boom,
                )

            assert calls["n"] == 0


class TestRecordEmbeddingProviderCallNeverWinsTelemetryRace:
    """Story #1586 Finding 3: record_embedding_provider_call() must use
    peek_telemetry_manager() (returns None pre-init), never
    get_telemetry_manager() -- the latter fabricates and permanently CACHES
    a disabled TelemetryConfig on first call when config is None, which
    would poison telemetry server-wide if this call site fires from a
    background scheduler before the real startup config is loaded (Bug
    class already fixed once for job_tracker.py/refresh_scheduler.py; this
    proves the embedding call site got the same fix).
    """

    def test_never_calls_get_telemetry_manager_when_not_yet_initialized(self):
        reset_telemetry_manager()
        reset_application_metrics()
        try:
            assert peek_telemetry_manager() is None  # sanity: truly uninitialized

            record_embedding_provider_call(
                model="voyage-code-3",
                duration_seconds=0.1,
                status="success",
                count_tokens=lambda: 5,
            )

            assert peek_telemetry_manager() is None, (
                "record_embedding_provider_call() must never call "
                "get_telemetry_manager() before real startup config is "
                "loaded -- doing so poisons the singleton with a disabled "
                "fallback for the rest of the process."
            )
        finally:
            reset_telemetry_manager()
            reset_application_metrics()
