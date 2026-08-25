"""
TDD Tests for TelemetryConfig dataclass (Story #695).

These tests define the expected behavior for the TelemetryConfig dataclass
and its integration with ServerConfig. Following TDD methodology - tests
written FIRST before implementation.

All tests use real components following MESSI Rule #1: No mocks.
"""

import json
import tempfile

import pytest

from src.code_indexer.server.utils.config_manager import (
    ServerConfig,
    ServerConfigManager,
    TelemetryConfig,
)


# =============================================================================
# AC1: Default telemetry configuration
# =============================================================================


class TestTelemetryConfigDefaults:
    """Tests for TelemetryConfig default values."""

    def test_telemetry_config_disabled_by_default(self):
        """
        AC1: Telemetry disabled by default.

        Given a fresh TelemetryConfig instance
        When created with no arguments
        Then enabled should be False
        """
        config = TelemetryConfig()
        assert config.enabled is False, "Telemetry should be disabled by default"

    def test_telemetry_config_default_collector_endpoint(self):
        """
        AC1: Default collector endpoint is localhost:4317.

        Given a fresh TelemetryConfig instance
        When created with no arguments
        Then collector_endpoint should be http://localhost:4317
        """
        config = TelemetryConfig()
        assert config.collector_endpoint == "http://localhost:4317", (
            "Default endpoint should be http://localhost:4317"
        )

    def test_telemetry_config_default_collector_protocol(self):
        """
        AC1: Default collector protocol is grpc.

        Given a fresh TelemetryConfig instance
        When created with no arguments
        Then collector_protocol should be grpc
        """
        config = TelemetryConfig()
        assert config.collector_protocol == "grpc", "Default protocol should be grpc"

    def test_telemetry_config_default_service_name(self):
        """
        AC1: Default service name is cidx-server.

        Given a fresh TelemetryConfig instance
        When created with no arguments
        Then service_name should be cidx-server
        """
        config = TelemetryConfig()
        assert config.service_name == "cidx-server", (
            "Default service_name should be cidx-server"
        )

    def test_telemetry_config_default_export_flags(self):
        """
        AC1: Default export flags - traces/metrics True.

        Given a fresh TelemetryConfig instance
        When created with no arguments
        Then export_traces and export_metrics should be True
        """
        config = TelemetryConfig()
        assert config.export_traces is True, "export_traces should be True by default"
        assert config.export_metrics is True, "export_metrics should be True by default"

    def test_telemetry_config_default_machine_metrics(self):
        """
        AC1: Default machine metrics settings.

        Given a fresh TelemetryConfig instance
        When created with no arguments
        Then machine_metrics_enabled should be True
        And machine_metrics_interval_seconds should be 60
        """
        config = TelemetryConfig()
        assert config.machine_metrics_enabled is True, (
            "machine_metrics_enabled should be True by default"
        )
        assert config.machine_metrics_interval_seconds == 60, (
            "machine_metrics_interval_seconds should be 60 by default"
        )

    def test_telemetry_config_default_deployment_environment(self):
        """
        AC1: Default deployment environment is development.

        Given a fresh TelemetryConfig instance
        When created with no arguments
        Then deployment_environment should be development
        """
        config = TelemetryConfig()
        assert config.deployment_environment == "development", (
            "deployment_environment should be development by default"
        )


# =============================================================================
# AC1: ServerConfig includes telemetry_config
# =============================================================================


class TestServerConfigTelemetryIntegration:
    """Tests for TelemetryConfig integration with ServerConfig."""

    def test_serverconfig_has_telemetry_config_field(self):
        """
        AC1: ServerConfig includes telemetry_config field.

        Given a new ServerConfig instance
        When created with defaults
        Then it should have a telemetry_config field of type TelemetryConfig
        """
        config = ServerConfig(server_dir="/tmp/test")
        assert hasattr(config, "telemetry_config"), (
            "ServerConfig should have telemetry_config field"
        )
        assert isinstance(config.telemetry_config, TelemetryConfig), (
            "telemetry_config should be TelemetryConfig instance"
        )

    def test_serverconfig_telemetry_disabled_by_default(self):
        """
        AC1: Fresh ServerConfig has telemetry disabled.

        Given a new ServerConfig instance
        When created with defaults
        Then telemetry_config.enabled should be False
        """
        config = ServerConfig(server_dir="/tmp/test")
        assert (
            config.telemetry_config.enabled is False  # type: ignore[union-attr]
        ), "Telemetry should be disabled by default in ServerConfig"


# =============================================================================
# AC2: Enable telemetry via configuration file
# =============================================================================


class TestTelemetryConfigSerialization:
    """Tests for TelemetryConfig JSON serialization/deserialization."""

    def test_telemetry_config_serialization(self):
        """
        AC2: TelemetryConfig serializes to JSON.

        Given a ServerConfig with custom telemetry settings
        When serialized via ServerConfigManager
        Then the JSON includes all telemetry fields
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            config = ServerConfig(server_dir=tmpdir)
            config.telemetry_config = TelemetryConfig(
                enabled=True,
                collector_endpoint="http://collector:4317",
                service_name="test-service",
            )

            manager.save_config(config)

            # Read raw JSON to verify serialization
            with open(manager.config_file_path, "r") as f:
                config_dict = json.load(f)

            assert "telemetry_config" in config_dict, (
                "Serialized config should include telemetry_config"
            )
            telemetry = config_dict["telemetry_config"]
            assert telemetry["enabled"] is True
            assert telemetry["collector_endpoint"] == "http://collector:4317"
            assert telemetry["service_name"] == "test-service"

    def test_telemetry_config_backward_compatibility(self):
        """
        AC2: Old configs without telemetry_config load successfully.

        Given an old config.json without telemetry_config
        When loaded via ServerConfigManager
        Then it loads with default TelemetryConfig (disabled)
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)

            # Create old config WITHOUT telemetry_config
            old_config = {
                "server_dir": tmpdir,
                "host": "127.0.0.1",
                "port": 8000,
            }

            with open(manager.config_file_path, "w") as f:
                json.dump(old_config, f)

            config = manager.load_config()

            assert config is not None, "Old config should load successfully"
            assert hasattr(config, "telemetry_config"), (
                "Should have telemetry_config field"
            )
            assert (
                config.telemetry_config.enabled is False  # type: ignore[union-attr]
            ), "Telemetry should be disabled for old configs"

    def test_telemetry_config_roundtrip(self):
        """
        AC2: Save + Load roundtrip preserves all telemetry fields.

        Given a ServerConfig with custom telemetry settings
        When saved and reloaded
        Then all telemetry fields are preserved
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)

            original = ServerConfig(server_dir=tmpdir)
            original.telemetry_config = TelemetryConfig(
                enabled=True,
                collector_endpoint="http://custom:4317",
                collector_protocol="http",
                service_name="custom-service",
                export_traces=True,
                export_metrics=True,
                machine_metrics_enabled=True,
                machine_metrics_interval_seconds=120,
                deployment_environment="staging",
            )

            manager.save_config(original)
            loaded = manager.load_config()

            assert loaded is not None
            assert loaded.telemetry_config.enabled == original.telemetry_config.enabled  # type: ignore[union-attr]
            assert (
                loaded.telemetry_config.collector_endpoint  # type: ignore[union-attr]
                == original.telemetry_config.collector_endpoint
            )
            assert (
                loaded.telemetry_config.collector_protocol  # type: ignore[union-attr]
                == original.telemetry_config.collector_protocol
            )
            assert (
                loaded.telemetry_config.service_name  # type: ignore[union-attr]
                == original.telemetry_config.service_name
            )
            assert (
                loaded.telemetry_config.export_traces  # type: ignore[union-attr]
                == original.telemetry_config.export_traces
            )
            assert (
                loaded.telemetry_config.export_metrics  # type: ignore[union-attr]
                == original.telemetry_config.export_metrics
            )
            assert (
                loaded.telemetry_config.machine_metrics_enabled  # type: ignore[union-attr]
                == original.telemetry_config.machine_metrics_enabled
            )
            assert (
                loaded.telemetry_config.machine_metrics_interval_seconds  # type: ignore[union-attr]
                == original.telemetry_config.machine_metrics_interval_seconds
            )
            assert (
                loaded.telemetry_config.deployment_environment  # type: ignore[union-attr]
                == original.telemetry_config.deployment_environment
            )


# =============================================================================
# Story #1676 AC1: telemetry environment-variable overrides REMOVED.
# DB/file config is authoritative; any of the 5 legacy vars still present in
# the process environment is ignored and reported via ONE aggregated WARNING.
# =============================================================================

_ALL_TELEMETRY_ENV_VARS = (
    "CIDX_TELEMETRY_ENABLED",
    "CIDX_OTEL_COLLECTOR_ENDPOINT",
    "CIDX_OTEL_COLLECTOR_PROTOCOL",
    "CIDX_OTEL_SERVICE_NAME",
    "CIDX_DEPLOYMENT_ENVIRONMENT",
)

_TELEMETRY_ENV_VAR_VALUES = {
    "CIDX_TELEMETRY_ENABLED": "true",
    "CIDX_OTEL_COLLECTOR_ENDPOINT": "http://env-collector:4317",
    "CIDX_OTEL_COLLECTOR_PROTOCOL": "http",
    "CIDX_OTEL_SERVICE_NAME": "env-service-name",
    "CIDX_DEPLOYMENT_ENVIRONMENT": "production",
}


class TestTelemetryEnvVarsNoLongerOverride:
    """AC1: none of the 5 legacy telemetry env vars override the DB/file
    config any more -- the config value always wins. Uses monkeypatch so the
    process environment is restored automatically after each test."""

    @pytest.mark.parametrize(
        "env_var,env_value,field_name",
        [
            ("CIDX_TELEMETRY_ENABLED", "true", "enabled"),
            (
                "CIDX_OTEL_COLLECTOR_ENDPOINT",
                "http://env-collector:4317",
                "collector_endpoint",
            ),
            ("CIDX_OTEL_COLLECTOR_PROTOCOL", "http", "collector_protocol"),
            ("CIDX_OTEL_SERVICE_NAME", "env-service-name", "service_name"),
            (
                "CIDX_DEPLOYMENT_ENVIRONMENT",
                "production",
                "deployment_environment",
            ),
        ],
    )
    def test_env_var_no_longer_applied(
        self, monkeypatch, env_var, env_value, field_name
    ):
        """Given a config value, setting the env var must not change it."""
        monkeypatch.setenv(env_var, env_value)

        config = ServerConfig(server_dir="/tmp/test")
        original_value = getattr(config.telemetry_config, field_name)  # type: ignore[union-attr]

        manager = ServerConfigManager("/tmp/test")
        config = manager.apply_env_overrides(config)

        assert (
            getattr(config.telemetry_config, field_name) == original_value  # type: ignore[union-attr]
        ), f"{env_var} must be ignored -- DB config is authoritative"

    def test_telemetry_disabled_env_var_no_longer_applied(self, monkeypatch):
        """CIDX_TELEMETRY_ENABLED=false must NOT override an enabled DB config."""
        monkeypatch.setenv("CIDX_TELEMETRY_ENABLED", "false")

        config = ServerConfig(server_dir="/tmp/test")
        config.telemetry_config = TelemetryConfig(enabled=True)

        manager = ServerConfigManager("/tmp/test")
        config = manager.apply_env_overrides(config)

        assert (
            config.telemetry_config.enabled is True  # type: ignore[union-attr]
        ), "CIDX_TELEMETRY_ENABLED=false must be ignored -- DB config is authoritative"


class TestTelemetryEnvVarIgnoredWarning:
    """AC1: presence of legacy telemetry env vars is reported via exactly
    ONE aggregated WARNING naming every present variable -- never one
    WARNING per variable, and never silently."""

    def test_aggregated_warning_fires_once_naming_all_present_vars(
        self, monkeypatch, caplog
    ):
        for name, value in _TELEMETRY_ENV_VAR_VALUES.items():
            monkeypatch.setenv(name, value)

        config = ServerConfig(server_dir="/tmp/test")
        manager = ServerConfigManager("/tmp/test")

        with caplog.at_level("WARNING"):
            manager.apply_env_overrides(config)

        telemetry_warnings = [
            record
            for record in caplog.records
            if record.levelname == "WARNING"
            and any(name in record.message for name in _ALL_TELEMETRY_ENV_VARS)
        ]
        assert len(telemetry_warnings) == 1, (
            "Exactly one aggregated WARNING must be logged, not one per "
            f"variable (found {len(telemetry_warnings)}: "
            f"{[r.message for r in telemetry_warnings]})"
        )
        message = telemetry_warnings[0].message
        for name in _ALL_TELEMETRY_ENV_VARS:
            assert name in message, f"{name} must be named in the aggregated warning"
        assert "ignored" in message.lower()

    def test_no_warning_when_no_telemetry_env_vars_present(self, monkeypatch, caplog):
        for name in _ALL_TELEMETRY_ENV_VARS:
            monkeypatch.delenv(name, raising=False)

        config = ServerConfig(server_dir="/tmp/test")
        manager = ServerConfigManager("/tmp/test")

        with caplog.at_level("WARNING"):
            manager.apply_env_overrides(config)

        telemetry_warnings = [
            record
            for record in caplog.records
            if record.levelname == "WARNING" and "telemetry" in record.message.lower()
        ]
        assert len(telemetry_warnings) == 0, (
            "No telemetry-related warning should be logged when no telemetry "
            "env vars are present"
        )


# =============================================================================
# Validation Tests
# =============================================================================


class TestTelemetryConfigValidation:
    """Tests for TelemetryConfig validation."""

    def test_validation_accepts_valid_config(self):
        """
        Validation accepts valid TelemetryConfig.

        Given a ServerConfig with valid telemetry settings
        When validated
        Then it passes without error
        """
        config = ServerConfig(server_dir="/tmp/test")
        config.telemetry_config = TelemetryConfig(
            enabled=True,
            collector_endpoint="http://localhost:4317",
        )

        manager = ServerConfigManager("/tmp/test")
        # Should not raise
        manager.validate_config(config)

    def test_validation_rejects_invalid_collector_protocol(self):
        """
        Validation rejects invalid collector_protocol.

        Given a config with collector_protocol = 'invalid'
        When validated
        Then it raises ValueError
        """
        config = ServerConfig(server_dir="/tmp/test")
        config.telemetry_config = TelemetryConfig(collector_protocol="invalid")

        manager = ServerConfigManager("/tmp/test")

        with pytest.raises(ValueError) as exc_info:
            manager.validate_config(config)

        assert "collector_protocol" in str(exc_info.value).lower(), (
            "Error should mention collector_protocol"
        )

    def test_validation_rejects_negative_machine_metrics_interval(self):
        """
        Validation rejects machine_metrics_interval_seconds < 1.

        Given a config with machine_metrics_interval_seconds = 0
        When validated
        Then it raises ValueError
        """
        config = ServerConfig(server_dir="/tmp/test")
        config.telemetry_config = TelemetryConfig(machine_metrics_interval_seconds=0)

        manager = ServerConfigManager("/tmp/test")

        with pytest.raises(ValueError) as exc_info:
            manager.validate_config(config)

        assert "machine_metrics_interval" in str(exc_info.value).lower(), (
            "Error should mention machine_metrics_interval"
        )

    def test_validation_accepts_grpc_protocol(self):
        """
        Validation accepts collector_protocol = 'grpc'.

        Given a config with collector_protocol = 'grpc'
        When validated
        Then it passes without error
        """
        config = ServerConfig(server_dir="/tmp/test")
        config.telemetry_config = TelemetryConfig(collector_protocol="grpc")

        manager = ServerConfigManager("/tmp/test")
        manager.validate_config(config)

    def test_validation_accepts_http_protocol(self):
        """
        Validation accepts collector_protocol = 'http'.

        Given a config with collector_protocol = 'http'
        When validated
        Then it passes without error
        """
        config = ServerConfig(server_dir="/tmp/test")
        config.telemetry_config = TelemetryConfig(collector_protocol="http")

        manager = ServerConfigManager("/tmp/test")
        manager.validate_config(config)
