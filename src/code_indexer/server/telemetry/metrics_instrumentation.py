"""
Application Metrics Instrumentation for OTEL (Story #698).

This module provides counters and histograms for search, FTS, and embedding
operations. Metrics are exported to OTEL when telemetry is enabled.

Metrics exported:
- cidx.search.requests (Counter) - Search request count
- cidx.search.duration (Histogram) - Search latency in seconds
- cidx.search.results_count (Histogram) - Number of results returned
- cidx.fts.requests (Counter) - FTS request count
- cidx.fts.duration (Histogram) - FTS latency in seconds
- cidx.fts.matches (Histogram) - Number of FTS matches
- cidx.embedding.requests (Counter) - Embedding request count
- cidx.embedding.tokens (Counter) - Total tokens processed
- cidx.embedding.duration (Histogram) - Embedding latency in seconds

Usage:
    from code_indexer.server.telemetry.metrics_instrumentation import (
        get_application_metrics,
    )

    metrics = get_application_metrics(telemetry_manager)
    metrics.record_search_request(
        search_type="semantic",
        repository="my-repo",
        duration_seconds=0.5,
        results_count=10,
        status="success",
    )
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from code_indexer.server.telemetry.manager import TelemetryManager
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)

# Singleton instance
_application_metrics: Optional["ApplicationMetrics"] = None


class ApplicationMetrics:
    """
    Application metrics instrumentation for CIDX operations.

    Provides counters and histograms for search, FTS, and embedding
    operations. No-op when telemetry is disabled.
    """

    def __init__(
        self,
        telemetry_manager: "TelemetryManager",
    ) -> None:
        """
        Initialize ApplicationMetrics.

        Args:
            telemetry_manager: TelemetryManager instance for getting meter
        """
        self._telemetry_manager = telemetry_manager
        self._is_active = False

        # Search metrics
        self._search_requests_counter: Optional[Any] = None
        self._search_duration_histogram: Optional[Any] = None
        self._search_results_histogram: Optional[Any] = None

        # FTS metrics
        self._fts_requests_counter: Optional[Any] = None
        self._fts_duration_histogram: Optional[Any] = None
        self._fts_matches_histogram: Optional[Any] = None

        # Embedding metrics
        self._embedding_requests_counter: Optional[Any] = None
        self._embedding_tokens_counter: Optional[Any] = None
        self._embedding_duration_histogram: Optional[Any] = None

        # X-Ray cache-identity-helper failure counter (Bug #1784 review).
        # Optional[Any] matches every other counter/histogram field in this
        # class -- the concrete OTEL Counter type is never imported at
        # module scope here, so this stays consistent with the existing
        # convention rather than introducing a one-off stricter type.
        self._xray_cache_identity_failures_counter: Optional[Any] = None

        if (
            telemetry_manager.is_initialized
            and telemetry_manager._config.export_metrics
        ):
            self._register_metrics()

    def _register_metrics(self) -> None:
        """Register all metrics with the meter."""
        try:
            meter = self._telemetry_manager.get_meter("cidx.application")

            # Search metrics
            self._search_requests_counter = meter.create_counter(
                name="cidx.search.requests",
                description="Number of search requests",
                unit="1",
            )
            self._search_duration_histogram = meter.create_histogram(
                name="cidx.search.duration",
                description="Search request duration",
                unit="s",
            )
            self._search_results_histogram = meter.create_histogram(
                name="cidx.search.results_count",
                description="Number of search results returned",
                unit="1",
            )

            # FTS metrics
            self._fts_requests_counter = meter.create_counter(
                name="cidx.fts.requests",
                description="Number of FTS requests",
                unit="1",
            )
            self._fts_duration_histogram = meter.create_histogram(
                name="cidx.fts.duration",
                description="FTS request duration",
                unit="s",
            )
            self._fts_matches_histogram = meter.create_histogram(
                name="cidx.fts.matches",
                description="Number of FTS matches",
                unit="1",
            )

            # Embedding metrics
            self._embedding_requests_counter = meter.create_counter(
                name="cidx.embedding.requests",
                description="Number of embedding requests",
                unit="1",
            )
            self._embedding_tokens_counter = meter.create_counter(
                name="cidx.embedding.tokens",
                description="Total tokens processed for embeddings",
                unit="1",
            )
            self._embedding_duration_histogram = meter.create_histogram(
                name="cidx.embedding.duration",
                description="Embedding request duration",
                unit="s",
            )

            # X-Ray cache-identity-helper failure metric (Bug #1784 review):
            # a WARNING log alone is insufficient observability at fleet
            # scale when a node's xray-cli binary is missing/broken and
            # EVERY compile silently loses the cluster cache.
            self._xray_cache_identity_failures_counter = meter.create_counter(
                name="cidx.xray.cache_identity_failures",
                description="Number of xray-cli --print-cache-identity helper failures",
                unit="1",
            )

            self._is_active = True
            logger.info("ApplicationMetrics initialized: 10 metrics registered")

        except Exception as e:
            logger.warning(
                format_error_log(
                    "REPO-GENERAL-044", f"Failed to register application metrics: {e}"
                )
            )
            self._is_active = False

    def record_search_request(
        self,
        search_type: str,
        repository: str,
        duration_seconds: float,
        results_count: int,
        status: str,
    ) -> None:
        """
        Record a search request metric.

        Args:
            search_type: Type of search (semantic, hybrid)
            repository: Repository name/ID
            duration_seconds: Request duration in seconds
            results_count: Number of results returned
            status: Request status (success, error)
        """
        if not self._is_active:
            return

        attributes = {
            "search_type": search_type,
            "repository": repository,
            "status": status,
        }

        try:
            assert self._search_requests_counter is not None
            assert self._search_duration_histogram is not None
            assert self._search_results_histogram is not None
            self._search_requests_counter.add(1, attributes)
            self._search_duration_histogram.record(duration_seconds, attributes)
            self._search_results_histogram.record(results_count, attributes)
        except Exception as e:
            logger.debug(f"Failed to record search metrics: {e}")

    def record_fts_request(
        self,
        repository: str,
        duration_seconds: float,
        matches_count: int,
        status: str,
    ) -> None:
        """
        Record an FTS request metric.

        Args:
            repository: Repository name/ID
            duration_seconds: Request duration in seconds
            matches_count: Number of matches found
            status: Request status (success, error)
        """
        if not self._is_active:
            return

        attributes = {
            "repository": repository,
            "status": status,
        }

        try:
            assert self._fts_requests_counter is not None
            assert self._fts_duration_histogram is not None
            assert self._fts_matches_histogram is not None
            self._fts_requests_counter.add(1, attributes)
            self._fts_duration_histogram.record(duration_seconds, attributes)
            self._fts_matches_histogram.record(matches_count, attributes)
        except Exception as e:
            logger.debug(f"Failed to record FTS metrics: {e}")

    def record_embedding_request(
        self,
        model: str,
        tokens_count: int,
        duration_seconds: float,
        status: str,
    ) -> None:
        """
        Record an embedding request metric.

        Args:
            model: Embedding model name
            tokens_count: Number of tokens processed
            duration_seconds: Request duration in seconds
            status: Request status (success, error)
        """
        if not self._is_active:
            return

        attributes = {
            "model": model,
            "status": status,
        }

        try:
            assert self._embedding_requests_counter is not None
            assert self._embedding_tokens_counter is not None
            assert self._embedding_duration_histogram is not None
            self._embedding_requests_counter.add(1, attributes)
            self._embedding_tokens_counter.add(tokens_count, attributes)
            self._embedding_duration_histogram.record(duration_seconds, attributes)
        except Exception as e:
            logger.debug(f"Failed to record embedding metrics: {e}")

    def record_xray_cache_identity_failure(self, reason: str) -> None:
        """
        Record an xray-cli --print-cache-identity helper failure (Bug #1784).

        A WARNING log alone is insufficient observability at fleet scale
        (~900 repos) when a node's xray-cli binary is missing/broken and
        EVERY compile silently degrades to a safe cluster-cache miss. This
        counter, viewed as a rate over time by the exporting monitoring
        system, surfaces repeated failures that a log line alone would not.

        Args:
            reason: Short failure classification, e.g. "nonzero_exit",
                "incomplete_output", or "exception".
        """
        if not self._is_active:
            return

        attributes = {"reason": reason}

        try:
            assert self._xray_cache_identity_failures_counter is not None
            self._xray_cache_identity_failures_counter.add(1, attributes)
        except Exception as e:
            logger.debug(f"Failed to record xray cache identity failure metric: {e}")

    @property
    def is_active(self) -> bool:
        """Return whether metrics are actively being recorded."""
        return self._is_active


def get_application_metrics(
    telemetry_manager: "TelemetryManager",
) -> ApplicationMetrics:
    """
    Get or create the ApplicationMetrics singleton.

    Args:
        telemetry_manager: TelemetryManager instance

    Returns:
        ApplicationMetrics instance
    """
    global _application_metrics

    if _application_metrics is None:
        _application_metrics = ApplicationMetrics(telemetry_manager)

    return _application_metrics


def reset_application_metrics() -> None:
    """Reset the ApplicationMetrics singleton (for testing)."""
    global _application_metrics
    _application_metrics = None
