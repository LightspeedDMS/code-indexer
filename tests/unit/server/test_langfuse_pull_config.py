"""
Tests for Langfuse Pull Configuration (Story #164).

Tests the extension of LangfuseConfig with pull-specific settings:
- LangfusePullProject dataclass
- LangfuseConfig with pull fields
- Configuration persistence
- Settings service integration
"""

import json
import tempfile
from pathlib import Path
from dataclasses import asdict

import pytest

from code_indexer.server.utils.config_manager import (
    LangfuseConfig,
    LangfusePullProject,
    ServerConfig,
    ServerConfigManager,
)
from code_indexer.server.services.config_service import ConfigService


class TestLangfusePullProject:
    """Test LangfusePullProject dataclass."""

    def test_create_with_defaults(self):
        """Test creating LangfusePullProject with defaults."""
        project = LangfusePullProject()
        assert project.public_key == ""
        assert project.secret_key == ""

    def test_create_with_all_fields(self):
        """Test creating LangfusePullProject with all fields."""
        project = LangfusePullProject(
            public_key="pk-abc",
            secret_key="sk-xyz",
        )
        assert project.public_key == "pk-abc"
        assert project.secret_key == "sk-xyz"

    def test_serialization(self):
        """Test LangfusePullProject serialization with asdict."""
        project = LangfusePullProject(
            public_key="pk-abc",
            secret_key="sk-xyz",
        )
        data = asdict(project)
        assert data == {
            "public_key": "pk-abc",
            "secret_key": "sk-xyz",
        }


class TestLangfuseConfigPullFields:
    """Test LangfuseConfig with new pull fields."""

    def test_default_pull_fields(self):
        """Test LangfuseConfig has correct default values for pull fields."""
        config = LangfuseConfig()
        assert config.pull_enabled is False
        assert config.pull_host == "https://cloud.langfuse.com"
        assert config.pull_projects == []
        assert config.pull_sync_interval_seconds == 300
        assert config.pull_trace_age_days == 30

    def test_create_with_pull_fields(self):
        """Test creating LangfuseConfig with pull fields."""
        project = LangfusePullProject(public_key="pk-test")
        config = LangfuseConfig(
            pull_enabled=True,
            pull_projects=[project],
            pull_sync_interval_seconds=600,
            pull_trace_age_days=60,
        )
        assert config.pull_enabled is True
        assert len(config.pull_projects) == 1
        assert config.pull_projects[0].public_key == "pk-test"
        assert config.pull_sync_interval_seconds == 600
        assert config.pull_trace_age_days == 60

    def test_serialization_with_pull_fields(self):
        """Test LangfuseConfig serialization includes pull fields."""
        project1 = LangfusePullProject(public_key="pk1", secret_key="sk1")
        project2 = LangfusePullProject(public_key="pk2", secret_key="sk2")
        config = LangfuseConfig(
            enabled=True,
            public_key="main-pk",
            pull_enabled=True,
            pull_projects=[project1, project2],
            pull_sync_interval_seconds=450,
            pull_trace_age_days=45,
        )
        data = asdict(config)

        assert data["pull_enabled"] is True
        assert data["pull_sync_interval_seconds"] == 450
        assert data["pull_trace_age_days"] == 45
        assert len(data["pull_projects"]) == 2
        assert data["pull_projects"][0]["public_key"] == "pk1"
        assert data["pull_projects"][1]["public_key"] == "pk2"

    def test_deserialization_from_dict(self):
        """Test LangfuseConfig can be reconstructed from dict (JSON load scenario)."""
        # Simulate loading from JSON where pull_projects are dicts
        data = {
            "enabled": True,
            "public_key": "pk-main",
            "secret_key": "sk-main",
            "host": "https://cloud.langfuse.com",
            "auto_trace_enabled": False,
            "pull_enabled": True,
            "pull_projects": [
                {"public_key": "pk1", "secret_key": "sk1"},
                {"public_key": "pk2", "secret_key": "sk2"},
            ],
            "pull_sync_interval_seconds": 600,
            "pull_trace_age_days": 60,
        }

        config = LangfuseConfig(**data)

        # After __post_init__, pull_projects should be LangfusePullProject instances
        assert config.pull_enabled is True
        assert len(config.pull_projects) == 2
        assert isinstance(config.pull_projects[0], LangfusePullProject)
        assert config.pull_projects[0].public_key == "pk1"
        assert config.pull_projects[0].secret_key == "sk1"
        assert isinstance(config.pull_projects[1], LangfusePullProject)
        assert config.pull_projects[1].public_key == "pk2"
        assert config.pull_projects[1].secret_key == "sk2"


class TestLangfuseConfigBackwardCompatibility:
    """Test backward compatibility when loading configs without pull fields."""

    def test_load_config_without_pull_fields(self):
        """Test that configs without pull fields load correctly with defaults."""
        # Old config format (before Story #164)
        data = {
            "enabled": True,
            "public_key": "pk-old",
            "secret_key": "sk-old",
            "host": "https://cloud.langfuse.com",
            "auto_trace_enabled": True,
        }

        config = LangfuseConfig(**data)

        # Should have default values for pull fields
        assert config.enabled is True
        assert config.public_key == "pk-old"
        assert config.pull_enabled is False
        assert config.pull_host == "https://cloud.langfuse.com"
        assert config.pull_projects == []
        assert config.pull_sync_interval_seconds == 300
        assert config.pull_trace_age_days == 30


class TestConfigPersistence:
    """Test configuration persistence with pull fields."""

    def test_save_and_load_with_pull_config(self):
        """Test round-trip save/load preserves pull configuration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create manager and config with pull settings
            manager = ServerConfigManager(tmpdir)
            config = manager.create_default_config()

            # Set pull configuration
            project1 = LangfusePullProject(
                public_key="pk-analytics",
                secret_key="sk-analytics",
            )
            project2 = LangfusePullProject(
                public_key="pk-monitoring",
                secret_key="sk-monitoring",
            )

            config.langfuse_config.pull_enabled = True
            config.langfuse_config.pull_projects = [project1, project2]
            config.langfuse_config.pull_sync_interval_seconds = 450
            config.langfuse_config.pull_trace_age_days = 45

            manager.save_config(config)

            # Load fresh instance
            manager2 = ServerConfigManager(tmpdir)
            loaded_config = manager2.load_config()

            # Verify pull settings persisted
            assert loaded_config.langfuse_config.pull_enabled is True
            assert len(loaded_config.langfuse_config.pull_projects) == 2
            assert isinstance(loaded_config.langfuse_config.pull_projects[0], LangfusePullProject)
            assert loaded_config.langfuse_config.pull_projects[0].public_key == "pk-analytics"
            assert loaded_config.langfuse_config.pull_projects[0].secret_key == "sk-analytics"
            assert loaded_config.langfuse_config.pull_projects[1].public_key == "pk-monitoring"
            assert loaded_config.langfuse_config.pull_projects[1].secret_key == "sk-monitoring"
            assert loaded_config.langfuse_config.pull_sync_interval_seconds == 450
            assert loaded_config.langfuse_config.pull_trace_age_days == 45


class TestConfigServiceIntegration:
    """Test ConfigurationService integration with pull fields."""

    def test_get_all_settings_includes_pull_fields(self):
        """Test get_all_settings includes pull configuration fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = ConfigService(tmpdir)
            config = service.get_config()

            # Set pull configuration
            project = LangfusePullProject(
                public_key="pk-test",
                secret_key="sk-test",
            )
            config.langfuse_config.pull_enabled = True
            config.langfuse_config.pull_projects = [project]
            config.langfuse_config.pull_sync_interval_seconds = 600
            config.langfuse_config.pull_trace_age_days = 60
            service.config_manager.save_config(config)

            # Re-load to verify
            service._config = None  # Force reload
            settings = service.get_all_settings()

            # Verify pull fields in settings
            assert "langfuse" in settings
            langfuse_settings = settings["langfuse"]
            assert langfuse_settings["pull_enabled"] is True
            assert len(langfuse_settings["pull_projects"]) == 1
            assert langfuse_settings["pull_projects"][0]["public_key"] == "pk-test"
            assert langfuse_settings["pull_projects"][0]["secret_key"] == "sk-test"
            assert langfuse_settings["pull_sync_interval_seconds"] == 600
            assert langfuse_settings["pull_trace_age_days"] == 60

    def test_update_langfuse_pull_enabled(self):
        """Test updating pull_enabled setting."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = ConfigService(tmpdir)

            # Update pull_enabled
            service.update_setting("langfuse", "pull_enabled", "true")

            config = service.get_config()
            assert config.langfuse_config.pull_enabled is True

    def test_update_langfuse_pull_host(self):
        """Test updating pull_host setting."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = ConfigService(tmpdir)

            # Update pull_host
            service.update_setting("langfuse", "pull_host", "http://my-langfuse:3000")

            config = service.get_config()
            assert config.langfuse_config.pull_host == "http://my-langfuse:3000"

            # Empty value falls back to default
            service.update_setting("langfuse", "pull_host", "")
            config = service.get_config()
            assert config.langfuse_config.pull_host == "https://cloud.langfuse.com"

    def test_update_langfuse_pull_sync_interval(self):
        """Test updating pull_sync_interval_seconds with bounds checking."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = ConfigService(tmpdir)

            # Valid value
            service.update_setting("langfuse", "pull_sync_interval_seconds", "450")
            config = service.get_config()
            assert config.langfuse_config.pull_sync_interval_seconds == 450

            # Min bound (60)
            service.update_setting("langfuse", "pull_sync_interval_seconds", "30")
            config = service.get_config()
            assert config.langfuse_config.pull_sync_interval_seconds == 60

            # Max bound (3600)
            service.update_setting("langfuse", "pull_sync_interval_seconds", "5000")
            config = service.get_config()
            assert config.langfuse_config.pull_sync_interval_seconds == 3600

    def test_update_langfuse_pull_trace_age(self):
        """Test updating pull_trace_age_days with bounds checking."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = ConfigService(tmpdir)

            # Valid value
            service.update_setting("langfuse", "pull_trace_age_days", "45")
            config = service.get_config()
            assert config.langfuse_config.pull_trace_age_days == 45

            # Min bound (1)
            service.update_setting("langfuse", "pull_trace_age_days", "0")
            config = service.get_config()
            assert config.langfuse_config.pull_trace_age_days == 1

            # Max bound (365)
            service.update_setting("langfuse", "pull_trace_age_days", "500")
            config = service.get_config()
            assert config.langfuse_config.pull_trace_age_days == 365

    def test_update_langfuse_pull_projects(self):
        """Test updating pull_projects from JSON string."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = ConfigService(tmpdir)

            # Update projects as JSON string
            projects_json = json.dumps([
                {"public_key": "pk1", "secret_key": "sk1"},
                {"public_key": "pk2", "secret_key": "sk2"},
            ])

            service.update_setting("langfuse", "pull_projects", projects_json)

            config = service.get_config()
            assert len(config.langfuse_config.pull_projects) == 2
            assert isinstance(config.langfuse_config.pull_projects[0], LangfusePullProject)
            assert config.langfuse_config.pull_projects[0].public_key == "pk1"
            assert config.langfuse_config.pull_projects[0].secret_key == "sk1"
            assert config.langfuse_config.pull_projects[1].public_key == "pk2"
            assert config.langfuse_config.pull_projects[1].secret_key == "sk2"
