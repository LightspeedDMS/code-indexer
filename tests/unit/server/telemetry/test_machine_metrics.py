"""
TDD Tests for MachineMetricsExporter (Story #696).

Tests the OTEL observable gauges that export machine metrics from
SystemMetricsCollector to the OTEL collector.

All tests use real components following MESSI Rule #1: No mocks.
"""

import socket

import pytest

from src.code_indexer.server.utils.config_manager import TelemetryConfig


def reset_all_singletons():
    """Reset all singletons to ensure clean test state."""
    from src.code_indexer.server.telemetry import (
        reset_telemetry_manager,
        reset_machine_metrics_exporter,
    )
    from src.code_indexer.server.services.system_metrics_collector import (
        reset_system_metrics_collector,
    )

    reset_machine_metrics_exporter()
    reset_telemetry_manager()
    reset_system_metrics_collector()


# =============================================================================
# MachineMetricsExporter Import Tests
# =============================================================================


class TestMachineMetricsExporterImport:
    """Tests for MachineMetricsExporter import behavior."""

    def test_machine_metrics_exporter_can_be_imported(self):
        """MachineMetricsExporter can be imported."""
        from src.code_indexer.server.telemetry.machine_metrics import (
            MachineMetricsExporter,
        )

        assert MachineMetricsExporter is not None

    def test_get_machine_metrics_exporter_function_exists(self):
        """get_machine_metrics_exporter() function is exported."""
        from src.code_indexer.server.telemetry.machine_metrics import (
            get_machine_metrics_exporter,
        )

        assert callable(get_machine_metrics_exporter)


# =============================================================================
# MachineMetricsExporter Creation Tests
# =============================================================================


@pytest.mark.slow
class TestMachineMetricsExporterCreation:
    """Tests for MachineMetricsExporter instantiation."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_all_singletons()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_all_singletons()

    def test_exporter_created_when_telemetry_and_machine_metrics_enabled(self):
        """
        MachineMetricsExporter is created when both telemetry and machine_metrics are enabled.
        """
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.machine_metrics import (
            MachineMetricsExporter,
        )

        config = TelemetryConfig(
            enabled=True,
            machine_metrics_enabled=True,
            collector_endpoint="http://localhost:4317",
        )
        telemetry_manager = get_telemetry_manager(config)

        exporter = MachineMetricsExporter(telemetry_manager)

        assert exporter is not None
        assert exporter.is_active

    def test_exporter_not_active_when_machine_metrics_disabled(self):
        """
        MachineMetricsExporter is not active when machine_metrics is disabled.
        """
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.machine_metrics import (
            MachineMetricsExporter,
        )

        config = TelemetryConfig(
            enabled=True,
            machine_metrics_enabled=False,
            collector_endpoint="http://localhost:4317",
        )
        telemetry_manager = get_telemetry_manager(config)

        exporter = MachineMetricsExporter(
            telemetry_manager, machine_metrics_enabled=False
        )

        assert exporter is not None
        assert not exporter.is_active


# =============================================================================
# Observable Gauge Registration Tests
# =============================================================================


@pytest.mark.slow
class TestMachineMetricsGaugeRegistration:
    """Tests for observable gauge registration."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_all_singletons()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_all_singletons()

    def test_cpu_usage_gauge_registered(self):
        """system.cpu.usage gauge is registered."""
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.machine_metrics import (
            MachineMetricsExporter,
        )

        config = TelemetryConfig(
            enabled=True,
            machine_metrics_enabled=True,
            collector_endpoint="http://localhost:4317",
        )
        telemetry_manager = get_telemetry_manager(config)
        exporter = MachineMetricsExporter(telemetry_manager)

        # The gauge should be registered
        assert "system.cpu.usage" in exporter.registered_gauges

    def test_memory_usage_gauge_registered(self):
        """system.memory.usage gauge is registered."""
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.machine_metrics import (
            MachineMetricsExporter,
        )

        config = TelemetryConfig(
            enabled=True,
            machine_metrics_enabled=True,
            collector_endpoint="http://localhost:4317",
        )
        telemetry_manager = get_telemetry_manager(config)
        exporter = MachineMetricsExporter(telemetry_manager)

        assert "system.memory.usage" in exporter.registered_gauges

    def test_disk_free_gauge_registered(self):
        """system.disk.free gauge is registered."""
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.machine_metrics import (
            MachineMetricsExporter,
        )

        config = TelemetryConfig(
            enabled=True,
            machine_metrics_enabled=True,
            collector_endpoint="http://localhost:4317",
        )
        telemetry_manager = get_telemetry_manager(config)
        exporter = MachineMetricsExporter(telemetry_manager)

        assert "system.disk.free" in exporter.registered_gauges

    def test_network_io_gauges_registered(self):
        """Network I/O gauges are registered."""
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.machine_metrics import (
            MachineMetricsExporter,
        )

        config = TelemetryConfig(
            enabled=True,
            machine_metrics_enabled=True,
            collector_endpoint="http://localhost:4317",
        )
        telemetry_manager = get_telemetry_manager(config)
        exporter = MachineMetricsExporter(telemetry_manager)

        assert "system.network.io.receive" in exporter.registered_gauges
        assert "system.network.io.transmit" in exporter.registered_gauges


# =============================================================================
# Host Identification Tests
# =============================================================================


class TestMachineMetricsHostIdentification:
    """Tests for host identification attributes."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_all_singletons()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_all_singletons()

    def test_host_name_attribute_set(self):
        """Metrics include host.name attribute."""
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.machine_metrics import (
            MachineMetricsExporter,
        )

        config = TelemetryConfig(
            enabled=True,
            machine_metrics_enabled=True,
            collector_endpoint="http://localhost:4317",
            service_name="test-service",
        )
        telemetry_manager = get_telemetry_manager(config)
        exporter = MachineMetricsExporter(telemetry_manager)

        # Host name should be set
        assert exporter.host_name is not None
        assert len(exporter.host_name) > 0
        # Should match actual hostname
        assert exporter.host_name == socket.gethostname()

    def test_service_name_attribute_set(self):
        """Metrics include service.name attribute."""
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.machine_metrics import (
            MachineMetricsExporter,
        )

        config = TelemetryConfig(
            enabled=True,
            machine_metrics_enabled=True,
            collector_endpoint="http://localhost:4317",
            service_name="test-cidx-server",
        )
        telemetry_manager = get_telemetry_manager(config)
        exporter = MachineMetricsExporter(telemetry_manager)

        assert exporter.service_name == "test-cidx-server"


# =============================================================================
# Metric Collection Callback Tests
# =============================================================================


class TestMachineMetricsCallbacks:
    """Tests for metric collection callbacks."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_all_singletons()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_all_singletons()

    def test_cpu_callback_returns_valid_value(self):
        """CPU callback returns value between 0 and 100."""
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.machine_metrics import (
            MachineMetricsExporter,
        )

        config = TelemetryConfig(
            enabled=True,
            machine_metrics_enabled=True,
            collector_endpoint="http://localhost:4317",
        )
        telemetry_manager = get_telemetry_manager(config)
        exporter = MachineMetricsExporter(telemetry_manager)

        # Call the CPU callback directly
        observations = list(exporter._cpu_callback(None))
        assert len(observations) == 1
        assert 0.0 <= observations[0].value <= 100.0

    def test_memory_callback_returns_valid_value(self):
        """Memory callback returns value between 0 and 100."""
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.machine_metrics import (
            MachineMetricsExporter,
        )

        config = TelemetryConfig(
            enabled=True,
            machine_metrics_enabled=True,
            collector_endpoint="http://localhost:4317",
        )
        telemetry_manager = get_telemetry_manager(config)
        exporter = MachineMetricsExporter(telemetry_manager)

        observations = list(exporter._memory_callback(None))
        assert len(observations) == 1
        assert 0.0 <= observations[0].value <= 100.0

    def test_callbacks_include_attributes(self):
        """Callbacks include host.name and service.name attributes."""
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.machine_metrics import (
            MachineMetricsExporter,
        )

        config = TelemetryConfig(
            enabled=True,
            machine_metrics_enabled=True,
            collector_endpoint="http://localhost:4317",
            service_name="test-service",
        )
        telemetry_manager = get_telemetry_manager(config)
        exporter = MachineMetricsExporter(telemetry_manager)

        observations = list(exporter._cpu_callback(None))
        attributes = observations[0].attributes

        assert "host.name" in attributes
        assert "service.name" in attributes
        assert attributes["service.name"] == "test-service"


# =============================================================================
# Bug #1606: observable-gauge callbacks must yield real Observation objects
# =============================================================================


class _LocalMachineMetricsManager:
    """Minimal real TelemetryManager whose get_meter() reads a locally-owned
    MeterProvider instead of the process-wide OTEL global registry, mirroring
    tests/unit/server/telemetry/otel_test_support.py's _InMemoryTelemetryManager
    pattern (kept local here since this test file's scope is machine_metrics.py
    only). Every object involved (MeterProvider, InMemoryMetricReader) is the
    genuine OTEL SDK -- no mocking of the code under test.
    """

    def __init__(self):
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader
        from src.code_indexer.server.telemetry.manager import TelemetryManager
        from src.code_indexer.server.utils.config_manager import (
            TelemetryConfig as _LocalTelemetryConfig,
        )

        self.reader = InMemoryMetricReader()
        self.provider = MeterProvider(metric_readers=[self.reader])
        outer = self

        class _Manager(TelemetryManager):
            def __init__(self):
                super().__init__(_LocalTelemetryConfig(enabled=False, service_name="m"))
                self._is_initialized = True

            def get_meter(self, name, version=None):
                return outer.provider.get_meter(name, version)

        self.manager = _Manager()

    def shutdown(self):
        self.provider.shutdown()


class TestMachineMetricsObservationWiringBug1606:
    """Bug #1606: observable-gauge callbacks yielded plain ``(value, attrs)``
    tuples instead of real ``opentelemetry.metrics.Observation`` objects.
    The OTEL SDK's real callback-invocation code (not a mock) requires
    ``Observation.value``/``.attributes`` and logs an ERROR + drops the data
    point for anything else -- confirmed live: every one of the 7 gauges
    below silently produced zero data points on every export cycle.
    """

    def setup_method(self):
        reset_all_singletons()

    def teardown_method(self):
        reset_all_singletons()

    def test_forced_export_cycle_produces_no_errors_and_real_data_points(self, caplog):
        import logging

        from src.code_indexer.server.telemetry.machine_metrics import (
            MachineMetricsExporter,
        )

        local = _LocalMachineMetricsManager()
        try:
            exporter = MachineMetricsExporter(local.manager)
            assert exporter.is_active, "exporter failed to activate"

            with caplog.at_level(logging.WARNING):
                data = local.reader.get_metrics_data()

            error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
            assert not error_records, (
                "observable-gauge callback(s) raised/logged an error during "
                f"a real export cycle: {[r.getMessage() for r in error_records]}"
            )

            assert data is not None, "forced export cycle produced no metrics at all"

            expected_gauge_names = [
                "system.cpu.usage",
                "system.memory.usage",
                "system.disk.free",
                "system.disk.io.read",
                "system.disk.io.write",
                "system.network.io.receive",
                "system.network.io.transmit",
            ]
            found_names = {
                metric.name
                for rm in data.resource_metrics
                for sm in rm.scope_metrics
                for metric in sm.metrics
            }
            for name in expected_gauge_names:
                assert name in found_names, f"{name} produced no metric at all"

            for rm in data.resource_metrics:
                for sm in rm.scope_metrics:
                    for metric in sm.metrics:
                        if metric.name not in expected_gauge_names:
                            continue
                        points = list(metric.data.data_points)
                        assert len(points) >= 1, f"{metric.name} has zero data points"
        finally:
            local.shutdown()
