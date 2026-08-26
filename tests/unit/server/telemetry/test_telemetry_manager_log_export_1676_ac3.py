"""
Story #1676 AC3: TelemetryManager wiring for real OTLP Logs export.

Covers:
  - export_logs=True constructs a LoggerProvider and registers EXACTLY ONE
    ContextAwareLogBridgeHandler with the async-logging listener -- no
    duplicate across repeated TelemetryManager construct/reset cycles (a
    known leak pattern in this codebase's own test suite, per the story's
    "Critical process notes").
  - export_logs=False constructs no logger provider at all and registers
    no wrapper handler.
  - TelemetryManager.shutdown() attempts each provider's shutdown
    INDEPENDENTLY: a tracer-provider shutdown failure must not skip the
    meter provider's or logger provider's shutdown (or the log bridge
    handler's unregistration).
  - reset_telemetry_manager() also unregisters the wrapper handler from the
    async-logging listener (not just manager.shutdown()).

All tests use real OTEL/async_logging primitives (Messi Rule #1: no mocks
of the thing under test); only a couple of tiny local fakes stand in for
provider objects in the independent-shutdown test.
"""

from __future__ import annotations

import pytest

from code_indexer.server.services.async_logging import (
    install_queue_logging,
    shutdown_queue_logging,
    unregister_additional_listener_handler,
)
from code_indexer.server.telemetry.log_handler import ContextAwareLogBridgeHandler
from code_indexer.server.utils.config_manager import TelemetryConfig


@pytest.fixture
def listener():
    lst = install_queue_logging([])
    try:
        yield lst
    finally:
        shutdown_queue_logging()


def _wrapper_handlers(listener):
    return [h for h in listener.handlers if isinstance(h, ContextAwareLogBridgeHandler)]


class TestExportLogsEnabledRegistersExactlyOneWrapper:
    def test_registers_exactly_one_wrapper_handler(self, listener) -> None:
        from code_indexer.server.telemetry import (
            get_telemetry_manager,
            reset_telemetry_manager,
        )

        config = TelemetryConfig(
            enabled=True,
            export_traces=False,
            export_metrics=False,
            export_logs=True,
        )
        get_telemetry_manager(config)
        try:
            assert len(_wrapper_handlers(listener)) == 1
        finally:
            reset_telemetry_manager()

    def test_no_duplicate_wrapper_across_reset_and_reconstruct(self, listener) -> None:
        from code_indexer.server.telemetry import (
            get_telemetry_manager,
            reset_telemetry_manager,
        )

        config = TelemetryConfig(
            enabled=True,
            export_traces=False,
            export_metrics=False,
            export_logs=True,
        )
        get_telemetry_manager(config)
        reset_telemetry_manager()

        get_telemetry_manager(config)
        try:
            assert len(_wrapper_handlers(listener)) == 1
        finally:
            reset_telemetry_manager()


class TestExportLogsDisabledConstructsNothing:
    def test_no_logger_provider_and_no_wrapper_when_export_logs_false(
        self, listener
    ) -> None:
        from code_indexer.server.telemetry.manager import TelemetryManager

        config = TelemetryConfig(
            enabled=True,
            export_traces=False,
            export_metrics=False,
            export_logs=False,
        )
        manager = TelemetryManager(config)
        try:
            assert manager.logger_provider is None
            assert manager.log_bridge_handler is None
            assert _wrapper_handlers(listener) == []
        finally:
            manager.shutdown()


class _RaisingProvider:
    def shutdown(self) -> None:
        raise RuntimeError("simulated tracer provider shutdown failure")


class _TrackingProvider:
    def __init__(self) -> None:
        self.shutdown_called = False

    def shutdown(self) -> None:
        self.shutdown_called = True


class TestTelemetryManagerIndependentShutdown:
    def test_tracer_shutdown_failure_does_not_skip_meter_or_logger_shutdown(
        self, listener
    ) -> None:
        from code_indexer.server.telemetry.manager import TelemetryManager

        config = TelemetryConfig(
            enabled=True,
            export_traces=True,
            export_metrics=True,
            export_logs=True,
        )
        manager = TelemetryManager(config)

        wrapper = manager.log_bridge_handler
        assert wrapper is not None
        assert wrapper in listener.handlers

        raising_tracer = _RaisingProvider()
        tracking_meter = _TrackingProvider()
        tracking_logger = _TrackingProvider()
        manager._tracer_provider = raising_tracer  # type: ignore[assignment]
        manager._meter_provider = tracking_meter  # type: ignore[assignment]
        manager._logger_provider = tracking_logger  # type: ignore[assignment]

        manager.shutdown()  # must not raise despite the tracer failure

        assert tracking_meter.shutdown_called is True
        assert tracking_logger.shutdown_called is True
        # The wrapper handler was unregistered as part of shutdown() too --
        # a second unregister attempt is now a no-op (returns False).
        assert unregister_additional_listener_handler(wrapper) is False


class TestResetTelemetryManagerUnregistersWrapper:
    def test_reset_unregisters_wrapper_from_listener(self, listener) -> None:
        from code_indexer.server.telemetry import (
            get_telemetry_manager,
            reset_telemetry_manager,
        )

        config = TelemetryConfig(
            enabled=True,
            export_traces=False,
            export_metrics=False,
            export_logs=True,
        )
        manager = get_telemetry_manager(config)
        wrapper = manager.log_bridge_handler
        assert wrapper is not None
        assert wrapper in listener.handlers

        reset_telemetry_manager()

        assert wrapper not in listener.handlers
