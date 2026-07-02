"""
Unit tests for DataRetentionConfig validation in config_manager (Story #400 - AC4).

Tests that validate_config enforces constraints on DataRetentionConfig:
- retention hours: min 1, max 8760
- cleanup interval: min 1, max 24

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import pytest

from code_indexer.server.utils.config_manager import ServerConfigManager


class TestDataRetentionConfigValidation:
    """AC4: validate_config enforces DataRetentionConfig constraints."""

    def test_validation_accepts_valid_defaults(self, tmp_path):
        """Default DataRetentionConfig values should pass validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config_manager.validate_config(config)  # Should not raise

    def test_validation_accepts_valid_retention_hours(self, tmp_path):
        """Valid retention hours (1-8760) should pass validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        for hours in [1, 168, 720, 2160, 8760]:
            config.data_retention_config.operational_logs_retention_hours = hours
            config_manager.validate_config(config)  # Should not raise

    def test_validation_rejects_operational_logs_retention_zero(self, tmp_path):
        """operational_logs_retention_hours of 0 should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config.data_retention_config.operational_logs_retention_hours = 0

        with pytest.raises(ValueError, match="operational_logs_retention_hours"):
            config_manager.validate_config(config)

    def test_validation_rejects_operational_logs_retention_too_high(self, tmp_path):
        """operational_logs_retention_hours above 8760 should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config.data_retention_config.operational_logs_retention_hours = 9000

        with pytest.raises(ValueError, match="operational_logs_retention_hours"):
            config_manager.validate_config(config)

    def test_validation_rejects_audit_logs_retention_zero(self, tmp_path):
        """audit_logs_retention_hours of 0 should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config.data_retention_config.audit_logs_retention_hours = 0

        with pytest.raises(ValueError, match="audit_logs_retention_hours"):
            config_manager.validate_config(config)

    def test_validation_rejects_audit_logs_retention_too_high(self, tmp_path):
        """audit_logs_retention_hours above 8760 should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config.data_retention_config.audit_logs_retention_hours = 9000

        with pytest.raises(ValueError, match="audit_logs_retention_hours"):
            config_manager.validate_config(config)

    def test_validation_rejects_sync_jobs_retention_zero(self, tmp_path):
        """sync_jobs_retention_hours of 0 should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config.data_retention_config.sync_jobs_retention_hours = 0

        with pytest.raises(ValueError, match="sync_jobs_retention_hours"):
            config_manager.validate_config(config)

    def test_validation_rejects_dep_map_history_retention_zero(self, tmp_path):
        """dep_map_history_retention_hours of 0 should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config.data_retention_config.dep_map_history_retention_hours = 0

        with pytest.raises(ValueError, match="dep_map_history_retention_hours"):
            config_manager.validate_config(config)

    def test_validation_rejects_background_jobs_retention_zero(self, tmp_path):
        """background_jobs_retention_hours of 0 should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config.data_retention_config.background_jobs_retention_hours = 0

        with pytest.raises(ValueError, match="background_jobs_retention_hours"):
            config_manager.validate_config(config)

    def test_validation_rejects_cleanup_interval_zero(self, tmp_path):
        """cleanup_interval_hours of 0 should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config.data_retention_config.cleanup_interval_hours = 0

        with pytest.raises(ValueError, match="cleanup_interval_hours"):
            config_manager.validate_config(config)

    def test_validation_rejects_cleanup_interval_too_high(self, tmp_path):
        """cleanup_interval_hours above 24 should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config.data_retention_config.cleanup_interval_hours = 25

        with pytest.raises(ValueError, match="cleanup_interval_hours"):
            config_manager.validate_config(config)

    def test_validation_accepts_valid_cleanup_interval(self, tmp_path):
        """Valid cleanup intervals (1-24) should pass validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        for hours in [1, 6, 12, 24]:
            config.data_retention_config.cleanup_interval_hours = hours
            config_manager.validate_config(config)  # Should not raise
