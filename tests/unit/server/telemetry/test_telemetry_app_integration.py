"""
TDD Tests for TelemetryManager integration with app.py (Story #695).

These tests define the expected behavior for TelemetryManager integration
into the FastAPI application lifecycle. Following TDD methodology - tests
written FIRST before implementation.

All tests use real components following MESSI Rule #1: No mocks.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


def reset_all_singletons():
    """Reset all singletons to ensure clean test state.

    Uses src.code_indexer... import paths to match module resolution in tests.
    """
    # Reset config service singleton
    from src.code_indexer.server.services.config_service import reset_config_service

    reset_config_service()

    # Reset telemetry manager singleton
    from src.code_indexer.server.telemetry import (
        reset_telemetry_manager,
        reset_machine_metrics_exporter,
    )

    reset_machine_metrics_exporter()
    reset_telemetry_manager()


# =============================================================================
# App.py Integration Tests
# =============================================================================


@pytest.mark.slow
class TestTelemetryAppIntegration:
    """Tests for TelemetryManager integration with app.py.

    NOTE: Test order matters! The "disabled" test MUST run first because
    OpenTelemetry global providers (TracerProvider, MeterProvider) can only
    be set once per process. Once set by an "enabled" test, they cannot be
    reset, which would cause subsequent "disabled" tests to fail.

    Tests are ordered in the file to ensure proper pytest execution order:
    - test_0_* appears first (disabled tests)
    - test_1_* appears second (enabled tests)
    - test_2_* appears third (shutdown tests, which also enable telemetry)
    """

    def test_0_telemetry_manager_not_initialized_when_disabled(self, tmp_path: Path):
        """
        TelemetryManager is not initialized when disabled.

        Given a server config with telemetry.enabled=False
        When the FastAPI app starts
        Then app.state.telemetry_manager should be None
        """
        from asgi_lifespan import LifespanManager
        import asyncio

        # Create minimal server config with telemetry disabled
        config_dir = tmp_path / ".cidx-server"
        config_dir.mkdir(parents=True)
        # Create data directory structure needed by lifespan
        (config_dir / "data" / "golden-repos").mkdir(parents=True)
        config_file = config_dir / "config.json"
        config_file.write_text(json.dumps({"telemetry_config": {"enabled": False}}))

        with patch.dict(
            os.environ,
            {
                "CIDX_SERVER_DATA_DIR": str(config_dir),
            },
        ):
            # Reset singletons INSIDE patch context so env var is set first
            reset_all_singletons()

            from src.code_indexer.server.app import create_app

            app = create_app()

            async def check_telemetry_state():
                # Use LifespanManager to properly trigger FastAPI lifespan events
                async with LifespanManager(app):
                    # When disabled, telemetry_manager should be None
                    assert hasattr(app.state, "telemetry_manager"), (
                        "telemetry_manager attribute should exist on app.state"
                    )
                    assert app.state.telemetry_manager is None, (
                        "telemetry_manager should be None when disabled"
                    )

            asyncio.run(check_telemetry_state())


@pytest.mark.slow
class TestTelemetryEnvironmentOverrides:
    """Tests for environment variable overrides in app context."""

    def test_env_var_overrides_config_file_for_telemetry(self, tmp_path: Path):
        """
        Environment variables override config file settings.

        Given a config file with telemetry disabled
        And CIDX_TELEMETRY_ENABLED=true environment variable
        When the app starts
        Then telemetry should be enabled
        """
        from asgi_lifespan import LifespanManager
        import asyncio

        # Create config with telemetry disabled
        config_dir = tmp_path / ".cidx-server"
        config_dir.mkdir(parents=True)
        # Create data directory structure needed by lifespan
        (config_dir / "data" / "golden-repos").mkdir(parents=True)
        config_file = config_dir / "config.json"
        config_file.write_text(json.dumps({"telemetry_config": {"enabled": False}}))

        # Override with environment variable
        with patch.dict(
            os.environ,
            {
                "CIDX_SERVER_DATA_DIR": str(config_dir),
                "CIDX_TELEMETRY_ENABLED": "true",
                "CIDX_OTEL_COLLECTOR_ENDPOINT": "http://localhost:4317",
            },
        ):
            # Reset singletons INSIDE patch context so env var is set first
            reset_all_singletons()

            from src.code_indexer.server.app import create_app

            app = create_app()

            async def check_env_override():
                # Use LifespanManager to properly trigger FastAPI lifespan events
                async with LifespanManager(app):
                    # With env override, telemetry should be enabled
                    assert hasattr(app.state, "telemetry_manager"), (
                        "telemetry_manager not set on app.state"
                    )
                    assert app.state.telemetry_manager is not None, (
                        "telemetry_manager should not be None when env var enables it"
                    )
                    assert app.state.telemetry_manager.is_initialized is True, (
                        "telemetry_manager should be initialized when env var enables it"
                    )

            asyncio.run(check_env_override())


@pytest.mark.slow
class TestApplicationMetricsStartupWiring:
    """Story #1586 AC6 (partial): ApplicationMetrics is constructed at
    startup alongside TelemetryManager/MachineMetricsExporter.

    Same ordering constraint as TestTelemetryAppIntegration above: the
    "disabled" test MUST run first (test_0_) since OTEL's global
    MeterProvider can only be set once per process.
    """

    @staticmethod
    def _app_with_env(tmp_path: Path, extra_env: dict):
        """Context manager: build a FastAPI app with a disabled-telemetry
        base config, patched with extra_env, singletons reset. The
        patch.dict scope stays open around the yielded app so lifespan
        (triggered later by the caller's own LifespanManager) still sees
        the env override."""
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            config_dir = tmp_path / ".cidx-server"
            config_dir.mkdir(parents=True)
            (config_dir / "data" / "golden-repos").mkdir(parents=True)
            config_file = config_dir / "config.json"
            config_file.write_text(json.dumps({"telemetry_config": {"enabled": False}}))

            env = {"CIDX_SERVER_DATA_DIR": str(config_dir), **extra_env}
            with patch.dict(os.environ, env):
                reset_all_singletons()
                from src.code_indexer.server.app import create_app

                yield create_app()

        return _ctx()

    def test_0_application_metrics_not_initialized_when_disabled(self, tmp_path: Path):
        from asgi_lifespan import LifespanManager
        import asyncio

        with self._app_with_env(tmp_path, {}) as app:

            async def check_application_metrics_state():
                async with LifespanManager(app):
                    assert hasattr(app.state, "application_metrics"), (
                        "application_metrics attribute should exist on app.state"
                    )
                    assert app.state.application_metrics is None, (
                        "application_metrics should be None when telemetry disabled"
                    )

            asyncio.run(check_application_metrics_state())

    def test_1_application_metrics_initialized_when_enabled(self, tmp_path: Path):
        from asgi_lifespan import LifespanManager
        import asyncio

        extra_env = {
            "CIDX_TELEMETRY_ENABLED": "true",
            "CIDX_OTEL_COLLECTOR_ENDPOINT": "http://localhost:4317",
        }
        with self._app_with_env(tmp_path, extra_env) as app:

            async def check_application_metrics_active():
                async with LifespanManager(app):
                    assert hasattr(app.state, "application_metrics"), (
                        "application_metrics attribute should exist on app.state"
                    )
                    assert app.state.application_metrics is not None, (
                        "application_metrics should not be None when telemetry enabled"
                    )
                    assert app.state.application_metrics.is_active is True, (
                        "application_metrics should be active when export_metrics is on"
                    )

            asyncio.run(check_application_metrics_active())


@pytest.mark.slow
class TestJobMetricsStartupWiring:
    """Story #1586 AC6 (remainder): JobMetrics is constructed at startup
    alongside ApplicationMetrics, with its observable-gauge callbacks
    registered.

    Same ordering constraint as the classes above: the "disabled" test MUST
    run first (test_0_) since OTEL's global MeterProvider can only be set
    once per process.
    """

    def test_0_job_metrics_not_initialized_when_disabled(self, tmp_path: Path):
        from asgi_lifespan import LifespanManager
        import asyncio

        with TestApplicationMetricsStartupWiring._app_with_env(tmp_path, {}) as app:

            async def check_job_metrics_state():
                async with LifespanManager(app):
                    assert hasattr(app.state, "job_metrics"), (
                        "job_metrics attribute should exist on app.state"
                    )
                    assert app.state.job_metrics is None, (
                        "job_metrics should be None when telemetry disabled"
                    )

            asyncio.run(check_job_metrics_state())

    def test_1_job_metrics_initialized_when_enabled(self, tmp_path: Path):
        from asgi_lifespan import LifespanManager
        import asyncio

        # Bare (non-"src."-prefixed) import: lifespan.py constructs JobMetrics
        # via this same module identity, so isinstance() below must compare
        # against it -- the "src." alias is a DISTINCT class object under
        # dual PYTHONPATH import identity (same gotcha noted in conftest.py).
        from code_indexer.server.telemetry.job_metrics import JobMetrics

        extra_env = {
            "CIDX_TELEMETRY_ENABLED": "true",
            "CIDX_OTEL_COLLECTOR_ENDPOINT": "http://localhost:4317",
        }
        with TestApplicationMetricsStartupWiring._app_with_env(
            tmp_path, extra_env
        ) as app:

            async def check_job_metrics_active():
                async with LifespanManager(app):
                    assert hasattr(app.state, "job_metrics"), (
                        "job_metrics attribute should exist on app.state"
                    )
                    assert app.state.job_metrics is not None, (
                        "job_metrics should not be None when telemetry enabled"
                    )
                    assert isinstance(app.state.job_metrics, JobMetrics), (
                        "job_metrics should be a real JobMetrics instance"
                    )
                    assert app.state.job_metrics.is_active is True, (
                        "job_metrics should be active when export_metrics is on"
                    )

            asyncio.run(check_job_metrics_active())
