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

import logging

import pytest

from code_indexer.server.services.async_logging import (
    install_queue_logging,
    register_additional_listener_handler,
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
        """Round 2 code review SHOULD-FIX 3: the manager is constructed with
        ALL THREE exports False -- this avoids ever constructing a real
        BatchSpanProcessor/PeriodicExportingMetricReader/
        BatchLogRecordProcessor (each spawns a background thread + an
        atexit handler), which the original version of this test leaked for
        the rest of the pytest process by overwriting the real providers
        with fakes before ever shutting them down. The wrapper handler
        (which real construction only registers when export_logs=True) is
        built and registered manually here instead, so the test's actual
        assertions -- independent per-provider shutdown, wrapper
        unregistration -- are unchanged.
        """
        from code_indexer.server.telemetry.manager import TelemetryManager

        config = TelemetryConfig(
            enabled=True,
            export_traces=False,
            export_metrics=False,
            export_logs=False,
        )
        manager = TelemetryManager(config)

        wrapper = ContextAwareLogBridgeHandler(logging.NullHandler())
        assert register_additional_listener_handler(wrapper) is True
        manager._log_bridge_handler = wrapper  # type: ignore[assignment]
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


class _OrderTrackingProvider:
    """Fake LoggerProvider that records whether the wrapper handler was
    already unregistered from the listener BY THE TIME its own shutdown()
    was invoked -- used to prove SHOULD-FIX 5's ordering requirement.
    """

    def __init__(self, listener, wrapper, order: list) -> None:
        self._listener = listener
        self._wrapper = wrapper
        self._order = order

    def shutdown(self) -> None:
        wrapper_still_registered = self._wrapper in self._listener.handlers
        self._order.append(("logger_provider_shutdown", wrapper_still_registered))


class TestLogBridgeHandlerRegistrationFailureLogsWarning:
    """Round 2 code review SHOULD-FIX 4: register_additional_listener_handler()'s
    return value must not be silently discarded. If registration ever fails
    (e.g. no active async-logging listener), export_logs=true would
    otherwise silently export nothing while log_bridge_handler still
    reports non-None -- a Messi Rule #13 anti-silent-failure violation.
    """

    def test_warning_logged_when_no_active_listener(self, caplog) -> None:
        from code_indexer.server.telemetry.manager import TelemetryManager

        # No active listener at all -- register_additional_listener_handler()
        # is guaranteed to return False.
        shutdown_queue_logging()

        config = TelemetryConfig(
            enabled=True,
            export_traces=False,
            export_metrics=False,
            export_logs=True,
        )
        with caplog.at_level(
            logging.WARNING, logger="code_indexer.server.telemetry.manager"
        ):
            manager = TelemetryManager(config)
        try:
            assert any(
                record.levelno == logging.WARNING
                and "log bridge handler" in record.getMessage().lower()
                for record in caplog.records
            ), "expected a WARNING when OTEL log bridge handler registration fails"
        finally:
            manager.shutdown()


class TestLogBridgeHandlerUnregisteredBeforeLoggerProviderShutdown:
    """Round 2 code review SHOULD-FIX 5: the wrapper handler must be
    unregistered from the async-logging listener BEFORE the LoggerProvider
    itself is shut down, so no in-flight record in the narrow window
    between the two steps is emitted into an already-shut-down provider
    (which itself logs "Shutdown called, ignoring..." -- feeding back into
    the Required Fix 1 amplification hazard if these steps run in the
    wrong order).
    """

    def test_wrapper_unregistered_before_logger_provider_shutdown(
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

        wrapper = ContextAwareLogBridgeHandler(logging.NullHandler())
        assert register_additional_listener_handler(wrapper) is True
        manager._log_bridge_handler = wrapper  # type: ignore[assignment]
        assert wrapper in listener.handlers

        order: list = []
        manager._logger_provider = _OrderTrackingProvider(  # type: ignore[assignment]
            listener, wrapper, order
        )

        manager.shutdown()

        assert order == [("logger_provider_shutdown", False)], (
            "the wrapper handler must be unregistered from the listener "
            "BEFORE the LoggerProvider is shut down"
        )
