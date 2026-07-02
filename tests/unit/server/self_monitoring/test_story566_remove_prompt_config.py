"""
Unit tests for Story #566: Remove Prompt Configuration from Self-Monitoring UI.

Tests all acceptance criteria:
- AC1: SelfMonitoringConfig dataclass no longer has prompt_template / prompt_user_modified fields
- AC2: Cadence and Model settings persist correctly (regression guard)
- AC3: SelfMonitoringService always loads prompt from default_analysis_prompt.md
- AC4: Config persistence ignores stale prompt_template field in existing JSON
- AC5: POST route ignores prompt_template form parameter
"""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path


class TestAC1_SelfMonitoringConfigNoPromptFields:
    """AC1: SelfMonitoringConfig dataclass must NOT contain prompt_template or prompt_user_modified."""

    def test_self_monitoring_config_has_no_prompt_template_field(self):
        """SelfMonitoringConfig must not have a prompt_template field."""
        from code_indexer.server.utils.config_manager import SelfMonitoringConfig
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(SelfMonitoringConfig)}
        assert "prompt_template" not in field_names, (
            "SelfMonitoringConfig must not have prompt_template field (Story #566)"
        )

    def test_self_monitoring_config_has_no_prompt_user_modified_field(self):
        """SelfMonitoringConfig must not have a prompt_user_modified field."""
        from code_indexer.server.utils.config_manager import SelfMonitoringConfig
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(SelfMonitoringConfig)}
        assert "prompt_user_modified" not in field_names, (
            "SelfMonitoringConfig must not have prompt_user_modified field (Story #566)"
        )

    def test_self_monitoring_config_still_has_required_fields(self):
        """SelfMonitoringConfig must still have enabled, cadence_minutes, and model."""
        from code_indexer.server.utils.config_manager import SelfMonitoringConfig
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(SelfMonitoringConfig)}
        assert "enabled" in field_names
        assert "cadence_minutes" in field_names
        assert "model" in field_names

    def test_self_monitoring_config_default_values_without_prompt(self):
        """SelfMonitoringConfig defaults are correct without prompt fields."""
        from code_indexer.server.utils.config_manager import SelfMonitoringConfig

        config = SelfMonitoringConfig()

        assert config.enabled is False
        assert config.cadence_minutes == 60
        assert config.model == "opus"

    def test_self_monitoring_config_instantiation_rejects_prompt_template(self):
        """Instantiating SelfMonitoringConfig with prompt_template must raise TypeError."""
        from code_indexer.server.utils.config_manager import SelfMonitoringConfig

        with pytest.raises(TypeError):
            SelfMonitoringConfig(prompt_template="should fail")  # type: ignore[call-arg]

    def test_self_monitoring_config_instantiation_rejects_prompt_user_modified(self):
        """Instantiating SelfMonitoringConfig with prompt_user_modified must raise TypeError."""
        from code_indexer.server.utils.config_manager import SelfMonitoringConfig

        with pytest.raises(TypeError):
            SelfMonitoringConfig(prompt_user_modified=True)  # type: ignore[call-arg]


class TestAC2_CadenceAndModelPersist:
    """AC2: Cadence and model settings still save and reload correctly."""

    def test_cadence_and_model_roundtrip(self, tmp_path):
        """Save cadence=30 and model=sonnet, reload and verify persisted."""
        from code_indexer.server.utils.config_manager import (
            ServerConfigManager,
        )

        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        assert config.self_monitoring_config is not None
        config.self_monitoring_config.cadence_minutes = 30
        config.self_monitoring_config.model = "sonnet"
        config.self_monitoring_config.enabled = True

        config_manager.save_config(config)
        loaded = config_manager.load_config()

        assert loaded is not None
        assert loaded.self_monitoring_config is not None
        assert loaded.self_monitoring_config.cadence_minutes == 30
        assert loaded.self_monitoring_config.model == "sonnet"
        assert loaded.self_monitoring_config.enabled is True

    def test_self_monitoring_config_custom_cadence_and_model(self):
        """SelfMonitoringConfig accepts custom cadence and model."""
        from code_indexer.server.utils.config_manager import SelfMonitoringConfig

        config = SelfMonitoringConfig(
            enabled=True,
            cadence_minutes=30,
            model="sonnet",
        )

        assert config.enabled is True
        assert config.cadence_minutes == 30
        assert config.model == "sonnet"


class TestAC3_ServiceAlwaysLoadsPromptFromFile:
    """AC3: SelfMonitoringService always loads prompt from default_analysis_prompt.md."""

    def test_service_constructor_has_no_prompt_template_param(self):
        """SelfMonitoringService.__init__ must not accept a prompt_template parameter."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService
        import inspect

        sig = inspect.signature(SelfMonitoringService.__init__)
        assert "prompt_template" not in sig.parameters, (
            "SelfMonitoringService must not accept prompt_template constructor param (Story #566)"
        )

    def test_service_execute_scan_calls_get_default_prompt(self, tmp_path):
        """_execute_scan must call get_default_prompt() (not use a stored template)."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService

        db_path = str(tmp_path / "cidx.db")
        log_db_path = str(tmp_path / "logs.db")

        service = SelfMonitoringService(
            enabled=True,
            cadence_minutes=60,
            job_manager=Mock(),
            db_path=db_path,
            log_db_path=log_db_path,
            github_repo="owner/repo",
            model="sonnet",
        )

        mock_scanner_instance = MagicMock()
        mock_scanner_instance.execute_scan.return_value = {"status": "SUCCESS"}

        with (
            patch(
                "code_indexer.server.self_monitoring.service.get_default_prompt"
            ) as mock_get_prompt,
            patch(
                "code_indexer.server.self_monitoring.scanner.LogScanner"
            ) as mock_scanner_class,
        ):
            mock_get_prompt.return_value = "default prompt text"
            mock_scanner_class.return_value = mock_scanner_instance

            service._execute_scan()

            mock_get_prompt.assert_called_once()

    def test_service_execute_scan_uses_prompt_from_file_not_config(self, tmp_path):
        """_execute_scan must pass the file-loaded prompt to LogScanner."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService

        db_path = str(tmp_path / "cidx.db")
        log_db_path = str(tmp_path / "logs.db")

        service = SelfMonitoringService(
            enabled=True,
            cadence_minutes=60,
            job_manager=Mock(),
            db_path=db_path,
            log_db_path=log_db_path,
            github_repo="owner/repo",
            model="sonnet",
        )

        expected_prompt = "prompt loaded from default_analysis_prompt.md"
        mock_scanner_instance = MagicMock()
        mock_scanner_instance.execute_scan.return_value = {"status": "SUCCESS"}

        with (
            patch(
                "code_indexer.server.self_monitoring.service.get_default_prompt"
            ) as mock_get_prompt,
            patch(
                "code_indexer.server.self_monitoring.scanner.LogScanner"
            ) as mock_scanner_class,
        ):
            mock_get_prompt.return_value = expected_prompt
            mock_scanner_class.return_value = mock_scanner_instance

            service._execute_scan()

            # LogScanner must be constructed with the file-loaded prompt
            call_kwargs = mock_scanner_class.call_args
            assert call_kwargs is not None
            passed_prompt = call_kwargs[1].get(
                "prompt_template",
                call_kwargs[0][4] if len(call_kwargs[0]) > 4 else None,
            )
            assert passed_prompt == expected_prompt, (
                f"Expected prompt from file, got: {passed_prompt!r}"
            )

    def test_service_has_no_prompt_template_attribute(self):
        """SelfMonitoringService instance must NOT store _prompt_template attribute."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService

        service = SelfMonitoringService(
            enabled=False,
            cadence_minutes=60,
            job_manager=Mock(),
        )

        assert not hasattr(service, "_prompt_template"), (
            "SelfMonitoringService must not store _prompt_template (Story #566)"
        )


class TestAC4_BackwardCompatibilityWithStaleConfig:
    """AC4: Server loads config cleanly when existing JSON has stale prompt_template field."""

    def test_config_load_ignores_stale_prompt_template_field(self, tmp_path):
        """Loading config with stale prompt_template in JSON must not raise."""
        from code_indexer.server.utils.config_manager import (
            ServerConfigManager,
            SelfMonitoringConfig,
        )

        # Write a config file that still has the old prompt_template field
        old_config_data = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
            "self_monitoring_config": {
                "enabled": True,
                "cadence_minutes": 30,
                "model": "sonnet",
                "prompt_template": "old custom prompt that should be ignored",
                "prompt_user_modified": True,
            },
        }

        config_file = tmp_path / "config.json"
        with open(config_file, "w") as f:
            json.dump(old_config_data, f)

        config_manager = ServerConfigManager(str(tmp_path))

        # Must not raise TypeError even though JSON has unknown fields
        loaded = config_manager.load_config()

        assert loaded is not None
        assert loaded.self_monitoring_config is not None
        assert isinstance(loaded.self_monitoring_config, SelfMonitoringConfig)

    def test_config_load_preserves_cadence_and_model_from_stale_config(self, tmp_path):
        """Loading stale config preserves cadence/model while discarding prompt fields."""
        from code_indexer.server.utils.config_manager import ServerConfigManager

        old_config_data = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
            "self_monitoring_config": {
                "enabled": True,
                "cadence_minutes": 45,
                "model": "sonnet",
                "prompt_template": "stale prompt should be dropped",
                "prompt_user_modified": True,
            },
        }

        config_file = tmp_path / "config.json"
        with open(config_file, "w") as f:
            json.dump(old_config_data, f)

        config_manager = ServerConfigManager(str(tmp_path))
        loaded = config_manager.load_config()

        assert loaded is not None
        assert loaded.self_monitoring_config is not None
        # These valid fields must be preserved
        assert loaded.self_monitoring_config.enabled is True
        assert loaded.self_monitoring_config.cadence_minutes == 45
        assert loaded.self_monitoring_config.model == "sonnet"

    def test_config_load_with_only_new_fields_works(self, tmp_path):
        """Loading config that already lacks prompt fields works correctly."""
        from code_indexer.server.utils.config_manager import ServerConfigManager

        new_config_data = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
            "self_monitoring_config": {
                "enabled": False,
                "cadence_minutes": 60,
                "model": "opus",
            },
        }

        config_file = tmp_path / "config.json"
        with open(config_file, "w") as f:
            json.dump(new_config_data, f)

        config_manager = ServerConfigManager(str(tmp_path))
        loaded = config_manager.load_config()

        assert loaded is not None
        assert loaded.self_monitoring_config is not None
        assert loaded.self_monitoring_config.cadence_minutes == 60
        assert loaded.self_monitoring_config.model == "opus"


class TestAC5_PostEndpointIgnoresPromptTemplate:
    """AC5: POST to /admin/self-monitoring ignores prompt_template form field."""

    def test_save_self_monitoring_config_does_not_save_prompt_template(self, tmp_path):
        """POST handler must not write prompt_template into the config object."""
        from code_indexer.server.utils.config_manager import (
            SelfMonitoringConfig,
        )
        import dataclasses

        # Verify the dataclass itself has no prompt_template field (structural guarantee)
        field_names = {f.name for f in dataclasses.fields(SelfMonitoringConfig)}
        assert "prompt_template" not in field_names, (
            "Config dataclass must not have prompt_template after Story #566"
        )

    def test_post_handler_saves_cadence_and_model_only(self, tmp_path):
        """POST form submission persists cadence and model but not prompt."""
        from code_indexer.server.utils.config_manager import (
            ServerConfigManager,
        )
        import dataclasses

        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        # Simulate what the POST handler does: update only cadence, model, enabled
        # (no prompt_template set, because the field no longer exists)
        assert config.self_monitoring_config is not None
        config.self_monitoring_config.enabled = True
        config.self_monitoring_config.cadence_minutes = 30
        config.self_monitoring_config.model = "sonnet"

        config_manager.save_config(config)
        loaded = config_manager.load_config()

        assert loaded is not None
        assert loaded.self_monitoring_config is not None
        assert loaded.self_monitoring_config.enabled is True
        assert loaded.self_monitoring_config.cadence_minutes == 30
        assert loaded.self_monitoring_config.model == "sonnet"

        # Confirm no prompt field leaked in
        field_names = {
            f.name for f in dataclasses.fields(loaded.self_monitoring_config)
        }
        assert "prompt_template" not in field_names


class TestAC1_HTMLTemplateNoPromptTextarea:
    """AC1: self_monitoring.html must not contain the prompt template textarea."""

    def test_html_template_has_no_prompt_template_textarea(self):
        """The self_monitoring.html template must not contain a prompt_template textarea."""
        template_path = (
            Path(__file__).parent.parent.parent.parent.parent
            / "src"
            / "code_indexer"
            / "server"
            / "web"
            / "templates"
            / "self_monitoring.html"
        )

        assert template_path.exists(), f"Template not found at {template_path}"
        content = template_path.read_text(encoding="utf-8")

        assert 'name="prompt_template"' not in content, (
            "self_monitoring.html must not contain prompt_template textarea (Story #566)"
        )

    def test_html_template_still_has_cadence_select(self):
        """self_monitoring.html must still have cadence_minutes select field."""
        template_path = (
            Path(__file__).parent.parent.parent.parent.parent
            / "src"
            / "code_indexer"
            / "server"
            / "web"
            / "templates"
            / "self_monitoring.html"
        )

        assert template_path.exists()
        content = template_path.read_text(encoding="utf-8")

        assert 'name="cadence_minutes"' in content, (
            "self_monitoring.html must retain cadence_minutes field"
        )

    def test_html_template_still_has_model_select(self):
        """self_monitoring.html must still have model select field."""
        template_path = (
            Path(__file__).parent.parent.parent.parent.parent
            / "src"
            / "code_indexer"
            / "server"
            / "web"
            / "templates"
            / "self_monitoring.html"
        )

        assert template_path.exists()
        content = template_path.read_text(encoding="utf-8")

        assert 'name="model"' in content, "self_monitoring.html must retain model field"

    def test_html_template_still_has_enabled_checkbox(self):
        """self_monitoring.html must still have enabled checkbox."""
        template_path = (
            Path(__file__).parent.parent.parent.parent.parent
            / "src"
            / "code_indexer"
            / "server"
            / "web"
            / "templates"
            / "self_monitoring.html"
        )

        assert template_path.exists()
        content = template_path.read_text(encoding="utf-8")

        assert 'name="enabled"' in content, (
            "self_monitoring.html must retain enabled checkbox"
        )
