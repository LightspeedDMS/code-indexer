"""
Tests for instrument_httpx()/uninstrument_httpx() (Story #1676 AC5).

These tests exercise the process-global HTTPXClientInstrumentor wiring
in-process -- they never touch OpenTelemetry's global TracerProvider/
MeterProvider (only-settable-once-per-process) singletons, so they are
safe to run alongside every other test in the shared pytest worker.
Tests that need to observe REAL spans produced through the global trace
API run in a FRESH subprocess instead (see
test_httpx_span_creation_1676_ac5.py) for exactly that reason.

No mocks of the code under test: HTTPXClientInstrumentor is the genuine
opentelemetry-instrumentation-httpx package, and TelemetryManager is the
genuine production class.
"""

from __future__ import annotations

import builtins
import logging

import pytest

from code_indexer.server.telemetry.instrumentation import (
    instrument_httpx,
    uninstrument_httpx,
)
from code_indexer.server.telemetry.manager import TelemetryManager
from code_indexer.server.utils.config_manager import TelemetryConfig


@pytest.fixture(autouse=True)
def _clean_httpx_instrumentation_state():
    """Guarantee httpx starts and ends each test un-instrumented.

    Prevents this test file from leaking instrumentation state into
    other test files sharing the pytest worker process.
    """
    uninstrument_httpx()
    yield
    uninstrument_httpx()


def _is_httpx_instrumented() -> bool:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    return bool(HTTPXClientInstrumentor().is_instrumented_by_opentelemetry)


class TestInstrumentHttpx:
    def test_instrument_httpx_returns_true_and_sets_flag(self):
        assert _is_httpx_instrumented() is False

        result = instrument_httpx()

        assert result is True
        assert _is_httpx_instrumented() is True

    def test_instrument_httpx_is_idempotent(self):
        first = instrument_httpx()
        second = instrument_httpx()

        assert first is True
        assert second is True
        assert _is_httpx_instrumented() is True

    def test_instrument_httpx_import_error_degrades_gracefully(self, caplog):
        real_import = builtins.__import__

        def _raise_for_httpx_instrumentation(name, *args, **kwargs):
            if name == "opentelemetry.instrumentation.httpx":
                raise ImportError("simulated missing package")
            return real_import(name, *args, **kwargs)

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(builtins, "__import__", _raise_for_httpx_instrumentation)
            with caplog.at_level(logging.WARNING):
                result = instrument_httpx()

        assert result is False
        assert _is_httpx_instrumented() is False
        assert any(
            "httpx instrumentation unavailable" in record.message
            for record in caplog.records
        )


class TestUninstrumentHttpx:
    def test_uninstrument_httpx_when_not_instrumented_returns_false(self):
        assert _is_httpx_instrumented() is False

        result = uninstrument_httpx()

        assert result is False

    def test_uninstrument_httpx_removes_instrumentation(self):
        instrument_httpx()
        assert _is_httpx_instrumented() is True

        result = uninstrument_httpx()

        assert result is True
        assert _is_httpx_instrumented() is False

    def test_uninstrument_httpx_import_error_degrades_gracefully(self, caplog):
        instrument_httpx()
        real_import = builtins.__import__

        def _raise_for_httpx_instrumentation(name, *args, **kwargs):
            if name == "opentelemetry.instrumentation.httpx":
                raise ImportError("simulated missing package")
            return real_import(name, *args, **kwargs)

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(builtins, "__import__", _raise_for_httpx_instrumentation)
            with caplog.at_level(logging.WARNING):
                result = uninstrument_httpx()

        assert result is False


class TestTelemetryManagerShutdownUninstallsHttpx:
    """Story #1676 AC5, requirement 5: shutdown/reset must undo the
    process-global httpx patch so repeated lifespan start/stop cycles
    (as happen throughout the test suite) never leave it dangling.
    """

    def test_shutdown_uninstalls_httpx_when_it_was_instrumented(self):
        instrument_httpx()
        assert _is_httpx_instrumented() is True

        manager = TelemetryManager(
            TelemetryConfig(
                enabled=True,
                export_traces=True,
                export_metrics=False,
                collector_endpoint="http://127.0.0.1:1",
            )
        )
        try:
            assert manager.is_initialized is True
        finally:
            manager.shutdown()

        assert _is_httpx_instrumented() is False

    def test_shutdown_is_a_no_op_when_httpx_was_never_instrumented(self):
        assert _is_httpx_instrumented() is False

        manager = TelemetryManager(
            TelemetryConfig(
                enabled=True,
                export_traces=True,
                export_metrics=False,
                collector_endpoint="http://127.0.0.1:1",
            )
        )
        manager.shutdown()

        assert _is_httpx_instrumented() is False

    def test_shutdown_on_disabled_manager_does_not_touch_httpx(self):
        instrument_httpx()
        assert _is_httpx_instrumented() is True

        manager = TelemetryManager(TelemetryConfig(enabled=False))
        manager.shutdown()

        # A disabled manager's shutdown() is a `_is_initialized` early
        # return -- it must not reach (and therefore not touch) the
        # httpx-uninstrument step, since this manager never instrumented
        # anything.
        assert _is_httpx_instrumented() is True
