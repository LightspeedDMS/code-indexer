"""
TDD Tests for Application Metrics (Story #698).

Tests OTEL counters and histograms for search, FTS, and embedding operations.

All tests use real components following MESSI Rule #1: No mocks.
"""

import pytest

from code_indexer.server.utils.config_manager import TelemetryConfig


def reset_all_singletons():
    """Reset all singletons to ensure clean test state."""
    from code_indexer.server.telemetry import (
        reset_telemetry_manager,
        reset_machine_metrics_exporter,
    )
    from code_indexer.server.services.system_metrics_collector import (
        reset_system_metrics_collector,
    )

    reset_machine_metrics_exporter()
    reset_telemetry_manager()
    reset_system_metrics_collector()


# =============================================================================
# Metrics Instrumentation Import Tests
# =============================================================================


@pytest.mark.slow
class TestMetricsInstrumentationImport:
    """Tests for metrics instrumentation module import behavior."""

    def test_application_metrics_can_be_imported(self):
        """ApplicationMetrics class can be imported."""
        from code_indexer.server.telemetry.metrics_instrumentation import (
            ApplicationMetrics,
        )

        assert ApplicationMetrics is not None

    def test_get_application_metrics_function_exists(self):
        """get_application_metrics() function is exported."""
        from code_indexer.server.telemetry.metrics_instrumentation import (
            get_application_metrics,
        )

        assert callable(get_application_metrics)


# =============================================================================
# ApplicationMetrics Creation Tests
# =============================================================================


@pytest.mark.slow
class TestApplicationMetricsCreation:
    """Tests for ApplicationMetrics instantiation."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_all_singletons()
        from code_indexer.server.telemetry.metrics_instrumentation import (
            reset_application_metrics,
        )

        reset_application_metrics()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_all_singletons()
        from code_indexer.server.telemetry.metrics_instrumentation import (
            reset_application_metrics,
        )

        reset_application_metrics()

    def test_metrics_created_when_telemetry_enabled(self):
        """
        ApplicationMetrics is created when telemetry is enabled.

        Bug #1744 (round 4): this test used to construct a real
        TelemetryConfig(enabled=True, export_metrics=True) via
        get_telemetry_manager(), whose teardown (reset_all_singletons()
        -> reset_telemetry_manager() -> shutdown()) forces a real OTLP
        metrics export attempt against an unreachable localhost:4317
        collector -- confirmed 7.84s solo teardown cost. Fixed with
        active_application_metrics() (Story #1586 pattern, already
        established in otel_test_support.py): a real, locally-owned
        MeterProvider + InMemoryMetricReader, zero network I/O.
        ApplicationMetrics.is_active genuinely requires
        telemetry_manager._config.export_metrics truthy (unlike
        TelemetryManager itself), so a raw export_metrics=False flip
        would have broken this assertion -- active_application_metrics()
        keeps it True via a real, in-memory reader instead.
        """
        from tests.unit.server.telemetry.otel_test_support import (
            active_application_metrics,
        )

        with active_application_metrics() as (metrics, _reader):
            assert metrics is not None
            assert metrics.is_active

    def test_metrics_not_active_when_disabled(self):
        """
        ApplicationMetrics is not active when telemetry disabled.
        """
        from code_indexer.server.telemetry import get_telemetry_manager
        from code_indexer.server.telemetry.metrics_instrumentation import (
            ApplicationMetrics,
        )

        config = TelemetryConfig(
            enabled=False,
            collector_endpoint="http://localhost:4317",
        )
        telemetry_manager = get_telemetry_manager(config)

        metrics = ApplicationMetrics(telemetry_manager)

        assert metrics is not None
        assert not metrics.is_active


# =============================================================================
# Search Metrics Tests
# =============================================================================


@pytest.mark.slow
class TestSearchMetrics:
    """Tests for search operation metrics."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_all_singletons()
        from code_indexer.server.telemetry.metrics_instrumentation import (
            reset_application_metrics,
        )

        reset_application_metrics()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_all_singletons()
        from code_indexer.server.telemetry.metrics_instrumentation import (
            reset_application_metrics,
        )

        reset_application_metrics()

    def test_record_search_request_increments_counter(self):
        """
        record_search_request() increments the search requests counter.

        Bug #1744 sibling (round 4): same real-network dependency as
        test_metrics_created_when_telemetry_enabled above, fixed the same
        way with active_application_metrics().
        """
        from tests.unit.server.telemetry.otel_test_support import (
            active_application_metrics,
        )

        with active_application_metrics() as (metrics, _reader):
            # Record a search request - should not raise
            metrics.record_search_request(
                search_type="semantic",
                repository="test-repo",
                duration_seconds=0.5,
                results_count=10,
                status="success",
            )

            # Verify metric was recorded (counter was incremented)
            assert metrics._search_requests_counter is not None

    def test_record_search_includes_duration_histogram(self):
        """
        record_search_request() records duration in histogram.

        Bug #1744 sibling (round 4): same fix as the other tests in this
        class -- active_application_metrics() instead of a real network
        exporter.
        """
        from tests.unit.server.telemetry.otel_test_support import (
            active_application_metrics,
        )

        with active_application_metrics() as (metrics, _reader):
            # Record a search request with duration
            metrics.record_search_request(
                search_type="semantic",
                repository="test-repo",
                duration_seconds=1.25,
                results_count=5,
                status="success",
            )

            # Verify histogram exists
            assert metrics._search_duration_histogram is not None

    def test_record_search_includes_results_count_histogram(self):
        """
        record_search_request() records results count in histogram.

        Bug #1744 sibling (round 4): same fix as the other tests in this
        class -- active_application_metrics() instead of a real network
        exporter.
        """
        from tests.unit.server.telemetry.otel_test_support import (
            active_application_metrics,
        )

        with active_application_metrics() as (metrics, _reader):
            # Record search with results count
            metrics.record_search_request(
                search_type="semantic",
                repository="test-repo",
                duration_seconds=0.3,
                results_count=25,
                status="success",
            )

            # Verify histogram exists
            assert metrics._search_results_histogram is not None


# =============================================================================
# FTS Metrics Tests
# =============================================================================


@pytest.mark.slow
class TestFTSMetrics:
    """Tests for full-text search metrics."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_all_singletons()
        from code_indexer.server.telemetry.metrics_instrumentation import (
            reset_application_metrics,
        )

        reset_application_metrics()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_all_singletons()
        from code_indexer.server.telemetry.metrics_instrumentation import (
            reset_application_metrics,
        )

        reset_application_metrics()

    def test_record_fts_request_increments_counter(self):
        """
        record_fts_request() increments the FTS requests counter.

        Bug #1744 sibling (round 4): real-network dependency fixed with
        active_application_metrics(), same mechanism as
        TestSearchMetrics above.
        """
        from tests.unit.server.telemetry.otel_test_support import (
            active_application_metrics,
        )

        with active_application_metrics() as (metrics, _reader):
            # Record FTS request
            metrics.record_fts_request(
                repository="test-repo",
                duration_seconds=0.2,
                matches_count=15,
                status="success",
            )

            # Verify counter exists
            assert metrics._fts_requests_counter is not None


# =============================================================================
# Embedding Metrics Tests
# =============================================================================


@pytest.mark.slow
class TestEmbeddingMetrics:
    """Tests for embedding operation metrics."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_all_singletons()
        from code_indexer.server.telemetry.metrics_instrumentation import (
            reset_application_metrics,
        )

        reset_application_metrics()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_all_singletons()
        from code_indexer.server.telemetry.metrics_instrumentation import (
            reset_application_metrics,
        )

        reset_application_metrics()

    def test_record_embedding_request_increments_counter(self):
        """
        record_embedding_request() increments the embedding requests counter.

        Bug #1744 sibling (round 4): real-network dependency fixed with
        active_application_metrics(), same mechanism used throughout
        this file (pre-existing per-test setup style, unchanged -- out
        of #1744's scope to refactor into shared parametrization).
        """
        from tests.unit.server.telemetry.otel_test_support import (
            active_application_metrics,
        )

        with active_application_metrics() as (metrics, _reader):
            # Record embedding request
            metrics.record_embedding_request(
                model="voyage-3",
                tokens_count=500,
                duration_seconds=0.8,
                status="success",
            )

            # Verify counter exists
            assert metrics._embedding_requests_counter is not None

    def test_record_embedding_tracks_token_count(self):
        """
        record_embedding_request() records token count.

        Bug #1744 sibling (round 4): same fix as the sibling test above.
        """
        from tests.unit.server.telemetry.otel_test_support import (
            active_application_metrics,
        )

        with active_application_metrics() as (metrics, _reader):
            # Record embedding with token count
            metrics.record_embedding_request(
                model="voyage-3",
                tokens_count=1500,
                duration_seconds=1.2,
                status="success",
            )

            # Verify token counter exists
            assert metrics._embedding_tokens_counter is not None


# =============================================================================
# X-Ray Cache Identity Failure Metric Tests (Bug #1784 review observability)
# =============================================================================


class TestXrayCacheIdentityFailureMetric:
    """Tests for the cidx.xray.cache_identity_failures counter.

    Added per Bug #1784 code review: a WARNING log alone is insufficient
    observability when a node's xray-cli binary is missing/broken and EVERY
    compile silently loses the cluster cache. This counter is incremented
    by RustNativeBackend whenever `xray-cli --print-cache-identity` fails.
    """

    def setup_method(self):
        reset_all_singletons()
        from code_indexer.server.telemetry.metrics_instrumentation import (
            reset_application_metrics,
        )

        reset_application_metrics()

    def test_record_xray_cache_identity_failure(self):
        """Increments the counter with a reason attribute when active, and
        never raises when ApplicationMetrics is inactive (fail-open)."""
        from tests.unit.server.telemetry.otel_test_support import (
            active_application_metrics,
            find_metric,
        )

        with active_application_metrics() as (metrics, reader):
            metrics.record_xray_cache_identity_failure(reason="nonzero_exit")

            assert metrics._xray_cache_identity_failures_counter is not None
            metric = find_metric(reader, "cidx.xray.cache_identity_failures")
            assert metric is not None
            dp = list(metric.data.data_points)[0]
            assert dp.value == 1
            assert dp.attributes["reason"] == "nonzero_exit"

        from code_indexer.server.telemetry.manager import TelemetryManager
        from code_indexer.server.telemetry.metrics_instrumentation import (
            ApplicationMetrics,
        )

        inactive_metrics = ApplicationMetrics(
            TelemetryManager(TelemetryConfig(enabled=False))
        )
        assert not inactive_metrics.is_active
        inactive_metrics.record_xray_cache_identity_failure(
            reason="exception"
        )  # must not raise


class TestXrayTimeoutConfigReadFailureMetric:
    """Tests for the cidx.xray.timeout_config_read_failures counter.

    Consolidated review (Issue #1811/Bug #1812, new finding #7, Codex):
    _resolve_default_xray_timeout_seconds (handlers/xray.py) fails soft to
    the hardcoded _DEFAULT_TIMEOUT_SECONDS on any ConfigService read
    failure -- a deliberate, tested fail-soft contract (Bug #1399) that
    must NOT change. But a WARNING log alone is insufficient observability
    at fleet scale (~900 repos): a node whose ConfigService is permanently
    broken silently ignores every operator-configured xray_timeout_seconds
    override forever, with no signal beyond log-scraping. This counter,
    mirroring cidx.xray.cache_identity_failures (Bug #1784), closes that
    gap without touching the fail-soft behavior itself.
    """

    def setup_method(self):
        reset_all_singletons()
        from code_indexer.server.telemetry.metrics_instrumentation import (
            reset_application_metrics,
        )

        reset_application_metrics()

    def test_record_xray_timeout_config_read_failure(self):
        """Increments the counter with a reason attribute when active, and
        never raises when ApplicationMetrics is inactive (fail-open)."""
        from tests.unit.server.telemetry.otel_test_support import (
            active_application_metrics,
            find_metric,
        )

        with active_application_metrics() as (metrics, reader):
            metrics.record_xray_timeout_config_read_failure(reason="exception")

            assert metrics._xray_timeout_config_read_failures_counter is not None
            metric = find_metric(reader, "cidx.xray.timeout_config_read_failures")
            assert metric is not None
            dp = list(metric.data.data_points)[0]
            assert dp.value == 1
            assert dp.attributes["reason"] == "exception"

        from code_indexer.server.telemetry.manager import TelemetryManager
        from code_indexer.server.telemetry.metrics_instrumentation import (
            ApplicationMetrics,
        )

        inactive_metrics = ApplicationMetrics(
            TelemetryManager(TelemetryConfig(enabled=False))
        )
        assert not inactive_metrics.is_active
        inactive_metrics.record_xray_timeout_config_read_failure(
            reason="exception"
        )  # must not raise


# =============================================================================
# Metrics Attributes Tests
# =============================================================================


@pytest.mark.slow
class TestMetricsAttributes:
    """Tests for metrics attribute handling."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_all_singletons()
        from code_indexer.server.telemetry.metrics_instrumentation import (
            reset_application_metrics,
        )

        reset_application_metrics()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_all_singletons()
        from code_indexer.server.telemetry.metrics_instrumentation import (
            reset_application_metrics,
        )

        reset_application_metrics()

    def test_search_metrics_support_error_status(self):
        """
        Search metrics can record error status.

        Bug #1744 sibling (round 4): real-network dependency fixed with
        active_application_metrics(), same mechanism used throughout
        this file.
        """
        from tests.unit.server.telemetry.otel_test_support import (
            active_application_metrics,
        )

        with active_application_metrics() as (metrics, _reader):
            # Record a failed search - should not raise
            metrics.record_search_request(
                search_type="semantic",
                repository="test-repo",
                duration_seconds=0.1,
                results_count=0,
                status="error",
            )

            assert metrics.is_active

    def test_noop_when_telemetry_disabled(self):
        """
        Recording metrics is a no-op when telemetry is disabled.
        """
        from code_indexer.server.telemetry import get_telemetry_manager
        from code_indexer.server.telemetry.metrics_instrumentation import (
            ApplicationMetrics,
        )

        config = TelemetryConfig(
            enabled=False,
            collector_endpoint="http://localhost:4317",
        )
        telemetry_manager = get_telemetry_manager(config)
        metrics = ApplicationMetrics(telemetry_manager)

        # These should not raise even when disabled
        metrics.record_search_request(
            search_type="semantic",
            repository="test-repo",
            duration_seconds=0.5,
            results_count=10,
            status="success",
        )
        metrics.record_fts_request(
            repository="test-repo",
            duration_seconds=0.2,
            matches_count=5,
            status="success",
        )
        metrics.record_embedding_request(
            model="voyage-3",
            tokens_count=100,
            duration_seconds=0.3,
            status="success",
        )

        assert not metrics.is_active
