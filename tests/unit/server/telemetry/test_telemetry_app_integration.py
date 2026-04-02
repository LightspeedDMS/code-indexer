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
