"""Shared test-support helpers for OTEL wiring tests (Story #1586).

Real OTEL SDK objects only (MeterProvider, InMemoryMetricReader, Counter,
Histogram) -- no mocking of the metric-recording code under test. The one
thing this module works around is a genuine constraint of the OTEL API
itself: ``opentelemetry.metrics.set_meter_provider()`` can only succeed
ONCE per process (subsequent calls are silently ignored with a warning --
see ``opentelemetry.metrics._internal.set_meter_provider``'s docstring).
Since ``TelemetryManager.get_meter()`` reads the GLOBAL provider via that
API, two different test files in the same pytest session cannot each
install their own ``InMemoryMetricReader`` through the normal
``TelemetryManager(config)`` construction path -- whichever test runs
first would "win" the global registry for the rest of the process.

``active_application_metrics()`` sidesteps this with a small local subclass
of ``TelemetryManager`` (``_InMemoryTelemetryManager``) that overrides
``get_meter`` via ordinary method override to delegate to a locally-owned,
real ``MeterProvider(metric_readers=[InMemoryMetricReader()])`` -- bypassing
only the global-singleton indirection, never the OTEL SDK classes
themselves, and never touching production ``TelemetryManager`` code. Every
object involved (Counter, Histogram, MeterProvider, InMemoryMetricReader) is
the genuine OTEL SDK implementation, so counters/histograms created through
it are byte-identical to what production code would create. The context
manager shuts the locally-owned MeterProvider down on exit.

``active_application_metrics_singleton()`` is the sibling helper for WIRING
tests: production code (e.g. the MCP search handlers) resolves
``ApplicationMetrics`` itself via
``get_application_metrics(get_telemetry_manager())`` rather than receiving
an instance directly, so the test must install the real, inspectable
instance as the process-wide singleton for that call to observe it.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional, Tuple

from opentelemetry.metrics import Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from code_indexer.server.telemetry.job_metrics import (
    JobMetrics,
    get_job_metrics,
    reset_job_metrics,
)
from code_indexer.server.telemetry.manager import TelemetryManager
from code_indexer.server.telemetry.metrics_instrumentation import (
    ApplicationMetrics,
    get_application_metrics,
    reset_application_metrics,
)
from code_indexer.server.utils.config_manager import TelemetryConfig


@contextmanager
def active_job_metrics() -> Iterator[Tuple[JobMetrics, InMemoryMetricReader]]:
    """Yield (metrics, reader): a real, active JobMetrics plus the real
    InMemoryMetricReader backing it. Shuts the local MeterProvider down on
    exit. Sibling of active_application_metrics() for job/repo metrics
    (Story #1586 AC3/AC4).
    """
    manager, provider, reader = _build_local_manager()
    try:
        metrics = JobMetrics(manager)
        assert metrics.is_active, "JobMetrics failed to activate for test"
        yield metrics, reader
    finally:
        provider.shutdown()


def _install_telemetry_manager_singleton(
    manager: Optional[TelemetryManager],
) -> Optional[TelemetryManager]:
    """Atomically swap the code_indexer.server.telemetry.manager singleton
    to `manager` (or clear it, if None), all under one _manager_lock
    acquisition so a concurrent real get_telemetry_manager() caller can
    never observe a torn-down-but-not-yet-replaced window. Test-only;
    reaches into the module's private global the same way
    reset_telemetry_manager() does, but inline (that function acquires the
    same lock itself and is not reentrant, so it cannot be called from
    inside an already-held lock).

    Returns whatever manager was installed immediately beforehand, WITHOUT
    shutting it down -- that manager may still be owned by an outer,
    still-active context (Story #1586 code-review round 2 fix: this
    function used to unconditionally call .shutdown() on whatever it
    replaced, which killed an outer context's manager the moment a nested
    context installed its own). Callers are responsible for both restoring
    the returned previous manager on their own exit (LIFO) and for shutting
    down only the manager they themselves installed -- never one this
    function merely handed back to them.
    """
    import code_indexer.server.telemetry.manager as _manager_module

    with _manager_module._manager_lock:
        previous = _manager_module._telemetry_manager
        _manager_module._telemetry_manager = manager
        return previous


@contextmanager
def active_job_metrics_singleton() -> Iterator[Tuple[JobMetrics, InMemoryMetricReader]]:
    """Like active_job_metrics(), but installs the real JobMetrics as the
    process-wide singleton via get_job_metrics()/reset_job_metrics().

    For wiring tests that exercise production code which itself resolves
    get_job_metrics(get_telemetry_manager()) rather than receiving an
    instance directly (JobTracker.complete_job/fail_job, RefreshScheduler
    completion, startup/lifespan.py's observable-gauge callbacks). Resets
    the singleton back to None on exit so later tests get a clean slate.

    Also installs the local fake manager as the REAL
    code_indexer.server.telemetry.manager process-wide TelemetryManager
    singleton (Story #1586 AC3 fix): job_tracker.py's _record_job_metric
    calls peek_telemetry_manager() -- which reads that global directly,
    never creating one -- so the wiring under test would otherwise see
    "telemetry not yet initialized" and no-op.

    LIFO-safe for nesting (Story #1586 code-review round 2 fix): saves
    whatever manager was installed before this context started and
    restores exactly that manager on exit, rather than unconditionally
    clearing the global to None. Only the manager THIS context installs
    is ever shut down -- never a manager an outer, still-active context
    owns (e.g. active_application_metrics_singleton() wrapping this one,
    as tests/e2e/server/test_20_telemetry_metrics_wiring_1586.py does).
    """
    manager, provider, reader = _build_local_manager()
    reset_job_metrics()
    previous_manager = _install_telemetry_manager_singleton(manager)
    try:
        metrics = get_job_metrics(manager)
        assert metrics.is_active, "JobMetrics failed to activate for test"
        yield metrics, reader
    finally:
        _install_telemetry_manager_singleton(previous_manager)
        manager.shutdown()
        reset_job_metrics()
        provider.shutdown()


class _InMemoryTelemetryManager(TelemetryManager):
    """TelemetryManager whose get_meter() reads a locally-owned MeterProvider
    instead of the process-wide OTEL global registry.

    Test-only: lets each test file observe its own metric data points via a
    real InMemoryMetricReader, independent of whichever other test in the
    pytest session already claimed the global singleton.
    """

    def __init__(self, meter_provider: MeterProvider) -> None:
        # enabled=False: skip the real constructor's OTLP/global-registry
        # setup entirely -- this subclass supplies its own meter provider.
        super().__init__(TelemetryConfig(enabled=False))
        self._local_meter_provider = meter_provider
        self._is_initialized = True
        self._config.export_metrics = True

    def get_meter(self, name: str, version: Optional[str] = None) -> Meter:
        return self._local_meter_provider.get_meter(name, version)


def _build_local_manager() -> Tuple[
    _InMemoryTelemetryManager, MeterProvider, InMemoryMetricReader
]:
    """Construct a real, locally-owned (reader, provider, manager) triple.

    Shared by both context managers below -- the only difference between
    them is how the resulting ApplicationMetrics is obtained and torn down.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    manager = _InMemoryTelemetryManager(provider)
    return manager, provider, reader


@contextmanager
def active_application_metrics() -> Iterator[
    Tuple[ApplicationMetrics, InMemoryMetricReader]
]:
    """Yield (metrics, reader): a real, active ApplicationMetrics plus the
    real InMemoryMetricReader backing it. Shuts the local MeterProvider down
    on exit.
    """
    manager, provider, reader = _build_local_manager()
    try:
        metrics = ApplicationMetrics(manager)
        assert metrics.is_active, "ApplicationMetrics failed to activate for test"
        yield metrics, reader
    finally:
        provider.shutdown()


_span_tracer_install_lock = threading.Lock()


@contextmanager
def active_span_exporter() -> Iterator[InMemorySpanExporter]:
    """Yield a real InMemorySpanExporter wired to a locally-owned
    TracerProvider, installed directly into
    code_indexer.server.telemetry.spans's module-level tracer cache
    (Story #1586 AC5).

    Sidesteps the same OTEL SDK "global provider settable only once per
    process" constraint documented at the top of this module -- this time
    for trace.set_tracer_provider()/TracerProvider instead of
    metrics.set_meter_provider()/MeterProvider. spans.get_tracer() caches
    whatever is in the module's _tracer global and never re-resolves the
    global registry once set, so directly assigning a real, locally-owned
    tracer there (rather than calling trace.set_tracer_provider()) lets
    every test file observe its own spans independent of whichever other
    test already claimed the process-wide trace provider.

    _span_tracer_install_lock is held for the ENTIRE context lifetime
    (across the yield, not just the save/restore steps) so two overlapping
    uses of this helper serialize completely instead of interleaving their
    installed tracer -- otherwise a second caller's install could be
    clobbered by the first caller's restore-on-exit.
    """
    import code_indexer.server.telemetry.spans as spans_module

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.otel_test_support")

    with _span_tracer_install_lock:
        saved_tracer = spans_module._tracer
        saved_enabled = spans_module._tracing_enabled
        spans_module._tracer = tracer
        spans_module._tracing_enabled = True
        try:
            yield exporter
        finally:
            spans_module._tracer = saved_tracer
            spans_module._tracing_enabled = saved_enabled
            provider.shutdown()


def find_metric(reader: InMemoryMetricReader, name: str):
    """Return the OTEL SDK Metric object with the given name, or None.

    Iterates ``reader.get_metrics_data()`` -- the real OTEL SDK data model
    (ResourceMetrics -> ScopeMetrics -> Metric -> data_points).
    """
    data = reader.get_metrics_data()
    if data is None:
        return None
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == name:
                    return metric
    return None


@contextmanager
def active_application_metrics_singleton() -> Iterator[
    Tuple[ApplicationMetrics, InMemoryMetricReader]
]:
    """Like active_application_metrics(), but installs the real
    ApplicationMetrics as the process-wide singleton via
    get_application_metrics()/reset_application_metrics().

    For wiring tests that exercise production code which itself resolves
    get_application_metrics(get_telemetry_manager()) -- the metrics instance
    is never handed to the code under test directly. Resets the singleton
    back to None on exit so later tests get a clean slate.

    Also installs the local fake manager as the REAL
    code_indexer.server.telemetry.manager process-wide TelemetryManager
    singleton (Story #1586 Finding 3 fix), mirroring
    active_job_metrics_singleton() above: several production call sites
    (record_embedding_provider_call, _record_search_metric,
    _record_fts_metric) correctly use peek_telemetry_manager() -- which
    reads that global directly, never creating one -- so without this the
    wiring under test would see "telemetry not yet initialized" and no-op.

    LIFO-safe for nesting (Story #1586 code-review round 2 fix): saves
    whatever manager was installed before this context started and
    restores exactly that manager on exit, rather than unconditionally
    clearing the global to None. Only the manager THIS context installs
    is ever shut down -- never a manager an outer, still-active context
    owns.
    """
    manager, provider, reader = _build_local_manager()
    reset_application_metrics()
    previous_manager = _install_telemetry_manager_singleton(manager)
    try:
        metrics = get_application_metrics(manager)
        assert metrics.is_active, "ApplicationMetrics failed to activate for test"
        yield metrics, reader
    finally:
        _install_telemetry_manager_singleton(previous_manager)
        manager.shutdown()
        reset_application_metrics()
        provider.shutdown()
