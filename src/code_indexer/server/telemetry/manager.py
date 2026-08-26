"""
TelemetryManager - Singleton manager for OpenTelemetry SDK lifecycle (Story #695).

This module implements the TelemetryManager class which:
- Manages OTEL SDK initialization and shutdown
- Provides tracer and meter instances for instrumentation
- Supports both gRPC and HTTP protocols for OTLP export
- Handles graceful degradation when telemetry is disabled or collector is unreachable
- Uses lazy loading to ensure zero overhead when disabled

CRITICAL: Lazy loading pattern used throughout to avoid importing OTEL SDK
at module level, ensuring server startup time is unaffected when telemetry is disabled.
"""

from __future__ import annotations

import logging
from threading import Lock
from typing import TYPE_CHECKING, Optional
from code_indexer import __version__ as _CIDX_VERSION
from code_indexer.server.logging_utils import format_error_log

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter, MeterProvider
    from opentelemetry.trace import Tracer, TracerProvider

    from src.code_indexer.server.utils.config_manager import TelemetryConfig

logger = logging.getLogger(__name__)

# Story #1676 AC6: custom resource attribute for this codebase's cluster
# node-identity concept. Deliberately NOT the semantic-convention
# ResourceAttributes.HOST_ID -- that field has different semantics (a
# stable machine/VM identifier) than the node identity resolved by
# code_indexer.server.utils.cluster_node_id.resolve_cluster_node_id()
# (which is what NodeHeartbeatService/get_active_nodes() treat as
# authoritative for this cluster). Kept as a module constant so every
# reader/writer of this attribute name agrees on the exact string.
CLUSTER_NODE_ID_RESOURCE_ATTRIBUTE = "cidx.cluster.node_id"

# Singleton instance and lock
_telemetry_manager: Optional["TelemetryManager"] = None
_manager_lock = Lock()


class TelemetryManager:
    """
    Manages OpenTelemetry SDK lifecycle for CIDX Server.

    This class is responsible for:
    - Initializing OTEL TracerProvider and MeterProvider based on config
    - Providing tracer and meter instances for instrumentation
    - Graceful shutdown with telemetry flush
    - No-op behavior when telemetry is disabled

    Thread-safe singleton pattern is implemented via get_telemetry_manager().
    """

    def __init__(
        self,
        config: "TelemetryConfig",
        cluster_node_id: Optional[str] = None,
    ) -> None:
        """
        Initialize TelemetryManager with configuration.

        Args:
            config: TelemetryConfig instance with telemetry settings
            cluster_node_id: Story #1676 AC6 -- this process's cluster node
                identity, PRE-RESOLVED by the caller (lifespan.py) via
                code_indexer.server.utils.cluster_node_id.resolve_cluster_node_id().
                Deliberately a plain constructor argument rather than
                resolved internally here: manager.py must not import
                cluster-bootstrap internals, and must never invent a
                second/competing identity scheme. When None (e.g. tests
                constructing TelemetryManager directly, or any caller that
                has not resolved an identity), the resource simply omits
                the cidx.cluster.node_id attribute.

        Note:
            OTEL SDK is only loaded if config.enabled is True.
            This ensures zero overhead when telemetry is disabled.
        """
        self._config = config
        self._cluster_node_id = cluster_node_id
        self._tracer_provider: Optional["TracerProvider"] = None
        self._meter_provider: Optional["MeterProvider"] = None
        self._is_initialized = False

        if config.enabled:
            self._initialize_otel()

    def _initialize_otel(self) -> None:
        """
        Initialize OpenTelemetry SDK components.

        Lazy imports OTEL libraries to avoid loading them when disabled.
        Sets up TracerProvider and MeterProvider with OTLP exporters.
        """
        try:
            # Lazy import OTEL SDK components
            from opentelemetry import metrics, trace
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
            from opentelemetry.semconv.resource import ResourceAttributes

            # Create resource with service information. Story #1676 AC6
            # adds service.version (from the installed code_indexer package
            # version) and, when a cluster node identity was resolved by
            # the caller, a custom cidx.cluster.node_id attribute -- both
            # shared by the tracer AND meter providers below since they are
            # built from this ONE resource instance.
            resource_attributes = {
                ResourceAttributes.SERVICE_NAME: self._config.service_name,
                ResourceAttributes.DEPLOYMENT_ENVIRONMENT: self._config.deployment_environment,
                ResourceAttributes.SERVICE_VERSION: _CIDX_VERSION,
            }
            if self._cluster_node_id:
                resource_attributes[CLUSTER_NODE_ID_RESOURCE_ATTRIBUTE] = (
                    self._cluster_node_id
                )
            resource = Resource.create(resource_attributes)

            # Story #1676 AC4: explicit, config-driven sampler for every
            # TracerProvider this manager constructs, replacing the SDK's
            # own default sampler resolution (normally
            # ParentBased(AlwaysOn) absent OTEL_TRACES_SAMPLER env
            # configuration). ParentBased ensures an already-sampled (or
            # already-dropped) parent context is always honored regardless
            # of trace_sample_rate. Built once and shared by both branches
            # below for symmetry.
            sampler = ParentBased(
                root=TraceIdRatioBased(self._config.trace_sample_rate)
            )

            # Initialize TracerProvider if traces are enabled
            if self._config.export_traces:
                self._tracer_provider = TracerProvider(
                    resource=resource, sampler=sampler
                )
                self._setup_trace_exporter()
                trace.set_tracer_provider(self._tracer_provider)
            else:
                # Use no-op tracer provider
                self._tracer_provider = TracerProvider(
                    resource=resource, sampler=sampler
                )
                trace.set_tracer_provider(self._tracer_provider)

            # Initialize MeterProvider if metrics are enabled
            if self._config.export_metrics:
                self._meter_provider = self._create_meter_provider(resource)
                metrics.set_meter_provider(self._meter_provider)
            else:
                # Use no-op meter provider
                self._meter_provider = MeterProvider(resource=resource)
                metrics.set_meter_provider(self._meter_provider)

            self._is_initialized = True
            logger.info(
                f"OpenTelemetry initialized: service={self._config.service_name}, "
                f"endpoint={self._config.collector_endpoint}, "
                f"protocol={self._config.collector_protocol}"
            )

        except Exception as e:
            logger.error(
                format_error_log(
                    "QUERY-GENERAL-026", f"Failed to initialize OpenTelemetry: {e}"
                )
            )
            # Set initialized to True anyway - we tried, and we don't want to fail startup
            self._is_initialized = True

    def _setup_trace_exporter(self) -> None:
        """Configure trace exporter based on protocol."""
        if self._tracer_provider is None:
            return

        try:
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            if self._config.collector_protocol.lower() == "grpc":
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )

                exporter = OTLPSpanExporter(
                    endpoint=self._config.collector_endpoint,
                    insecure=True,
                )
            else:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[assignment]
                    OTLPSpanExporter,
                )

                # HTTP endpoint typically includes /v1/traces path
                endpoint = self._config.collector_endpoint
                if not endpoint.endswith("/v1/traces"):
                    endpoint = f"{endpoint.rstrip('/')}/v1/traces"
                exporter = OTLPSpanExporter(endpoint=endpoint)

            self._tracer_provider.add_span_processor(BatchSpanProcessor(exporter))  # type: ignore[attr-defined]

        except Exception as e:
            logger.warning(
                format_error_log(
                    "QUERY-GENERAL-027", f"Failed to setup trace exporter: {e}"
                )
            )

    def _create_meter_provider(self, resource) -> "MeterProvider":
        """Create MeterProvider with OTLP exporter."""
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        try:
            if self._config.collector_protocol.lower() == "grpc":
                from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                    OTLPMetricExporter,
                )

                exporter = OTLPMetricExporter(
                    endpoint=self._config.collector_endpoint,
                    insecure=True,
                )
            else:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (  # type: ignore[assignment]
                    OTLPMetricExporter,
                )

                endpoint = self._config.collector_endpoint
                if not endpoint.endswith("/v1/metrics"):
                    endpoint = f"{endpoint.rstrip('/')}/v1/metrics"
                exporter = OTLPMetricExporter(endpoint=endpoint)

            reader = PeriodicExportingMetricReader(
                exporter,
                export_interval_millis=self._config.machine_metrics_interval_seconds
                * 1000,
            )
            return MeterProvider(resource=resource, metric_readers=[reader])

        except Exception as e:
            logger.warning(
                format_error_log(
                    "REPO-GENERAL-042", f"Failed to setup metric exporter: {e}"
                )
            )
            return MeterProvider(resource=resource)

    @property
    def is_initialized(self) -> bool:
        """Return whether OTEL SDK is initialized."""
        return self._is_initialized

    @property
    def tracer_provider(self) -> Optional["TracerProvider"]:
        """Return the TracerProvider instance, or None if not initialized."""
        return self._tracer_provider

    @property
    def meter_provider(self) -> Optional["MeterProvider"]:
        """Return the MeterProvider instance, or None if not initialized."""
        return self._meter_provider

    @property
    def service_name(self) -> str:
        """Return the configured service name."""
        return self._config.service_name

    @property
    def cluster_node_id(self) -> Optional[str]:
        """Return the resolved cluster node identity (Story #1676 AC6), or
        None if the caller never supplied one."""
        return self._cluster_node_id

    @property
    def deployment_environment(self) -> str:
        """Return the configured deployment environment."""
        return self._config.deployment_environment

    @property
    def collector_protocol(self) -> str:
        """Return the configured collector protocol."""
        return self._config.collector_protocol

    def get_tracer(self, name: str, version: Optional[str] = None) -> "Tracer":
        """
        Get a tracer instance for instrumentation.

        Args:
            name: Name of the tracer (typically component/module name)
            version: Optional version string

        Returns:
            Tracer instance (real or no-op depending on config)
        """
        from opentelemetry import trace

        return trace.get_tracer(name, version)

    def get_meter(self, name: str, version: Optional[str] = None) -> "Meter":
        """
        Get a meter instance for metrics.

        Args:
            name: Name of the meter (typically component/module name)
            version: Optional version string

        Returns:
            Meter instance (real or no-op depending on config)
        """
        from opentelemetry import metrics

        return metrics.get_meter(name, version)  # type: ignore[arg-type]

    def shutdown(self) -> None:
        """Shutdown telemetry, flushing any pending data."""
        if not self._is_initialized:
            return

        try:
            if self._tracer_provider is not None:
                self._tracer_provider.shutdown()  # type: ignore[attr-defined]
                logger.debug("TracerProvider shutdown complete")

            if self._meter_provider is not None:
                self._meter_provider.shutdown()  # type: ignore[attr-defined]
                logger.debug("MeterProvider shutdown complete")

            logger.info("OpenTelemetry shutdown complete")

        except Exception as e:
            logger.warning(
                format_error_log(
                    "REPO-GENERAL-043", f"Error during OpenTelemetry shutdown: {e}"
                )
            )

        finally:
            # Story #1676 AC5: httpx instrumentation is process-global
            # (unlike per-app FastAPI instrumentation), so it has no
            # per-instance teardown hook elsewhere -- unwind it here,
            # unconditionally, so repeated lifespan start/stop cycles
            # (e.g. the test suite, via reset_telemetry_manager()) never
            # leave a dangling patch bound to this now-torn-down
            # TracerProvider even if the shutdown steps above raised.
            from code_indexer.server.telemetry.instrumentation import (
                uninstrument_httpx,
            )

            uninstrument_httpx()
            self._is_initialized = False


def get_telemetry_manager(
    config: Optional["TelemetryConfig"] = None,
    cluster_node_id: Optional[str] = None,
) -> TelemetryManager:
    """
    Get the TelemetryManager singleton instance.

    Args:
        config: TelemetryConfig to use for initialization.
                Required on first call, optional on subsequent calls.
                If None on first call, creates a disabled TelemetryConfig as fallback.
        cluster_node_id: Story #1676 AC6 -- this process's cluster node
                identity, pre-resolved by the caller (see
                TelemetryManager.__init__ for the full rationale). Only
                meaningful on the call that actually constructs the
                singleton (first call wins, same as config).

    Returns:
        TelemetryManager singleton instance

    Thread-safe implementation using double-checked locking.
    """
    global _telemetry_manager

    if _telemetry_manager is not None:
        return _telemetry_manager

    with _manager_lock:
        # Double-check after acquiring lock
        if _telemetry_manager is not None:
            return _telemetry_manager

        if config is None:
            # Create disabled config as fallback
            from code_indexer.server.utils.config_manager import TelemetryConfig

            config = TelemetryConfig(enabled=False)

        _telemetry_manager = TelemetryManager(config, cluster_node_id=cluster_node_id)
        return _telemetry_manager


def peek_telemetry_manager() -> Optional[TelemetryManager]:
    """
    Return the current TelemetryManager singleton WITHOUT creating one
    (Story #1586 AC3).

    Unlike get_telemetry_manager(), this never instantiates a disabled
    fallback on a cache miss -- it simply reports "not yet initialized" as
    None. Needed by call sites that can legitimately run BEFORE the real
    startup config is loaded (e.g. a background thread's job-completion
    callback racing the main lifespan coroutine's own telemetry-init
    block): such a caller must never win the get_telemetry_manager()
    "first call wins" race and permanently lock in a disabled config for
    the rest of the process.

    Returns:
        The TelemetryManager singleton if get_telemetry_manager() has
        already been called at least once this process, else None.
    """
    return _telemetry_manager


def reset_telemetry_manager() -> None:
    """
    Reset the TelemetryManager singleton.

    This is primarily for testing purposes. It shuts down the current
    manager (if any) and clears the singleton reference.
    """
    global _telemetry_manager

    with _manager_lock:
        if _telemetry_manager is not None:
            _telemetry_manager.shutdown()
            _telemetry_manager = None
