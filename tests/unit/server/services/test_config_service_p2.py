"""
Unit tests for ConfigService P2 settings integration (Story #3 - Phase 2, AC12-AC26).

Tests verify that:
1. ConfigService.get_all_settings() includes P2 config sections
2. ConfigService.update_setting() works for P2 config categories
3. Validation is enforced when updating P2 settings
"""

import pytest

from code_indexer.server.services.config_service import ConfigService


class TestConfigServiceGetAllSettingsP2:
    """Test ConfigService.get_all_settings() includes P2 config sections."""

    def test_get_all_settings_contains_git_timeouts(self, tmp_path):
        """AC12-AC15: get_all_settings includes git_timeouts section."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()

        assert "git_timeouts" in settings
        assert "git_local_timeout" in settings["git_timeouts"]
        assert "git_remote_timeout" in settings["git_timeouts"]

    def test_get_all_settings_git_timeouts_default_values(self, tmp_path):
        """AC12-AC15: git_timeouts has correct default values."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()

        assert settings["git_timeouts"]["git_local_timeout"] == 30
        assert settings["git_timeouts"]["git_remote_timeout"] == 300

    def test_get_all_settings_contains_error_handling(self, tmp_path):
        """AC16-AC18: get_all_settings includes error_handling section."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()

        assert "error_handling" in settings
        assert "max_retry_attempts" in settings["error_handling"]
        assert "base_retry_delay_seconds" in settings["error_handling"]
        assert "max_retry_delay_seconds" in settings["error_handling"]

    def test_get_all_settings_error_handling_default_values(self, tmp_path):
        """AC16-AC18: error_handling has correct default values."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()

        assert settings["error_handling"]["max_retry_attempts"] == 3
        assert settings["error_handling"]["base_retry_delay_seconds"] == 0.1
        assert settings["error_handling"]["max_retry_delay_seconds"] == 60.0

    def test_get_all_settings_contains_api_limits(self, tmp_path):
        """AC19-AC24: get_all_settings includes api_limits section."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()

        assert "api_limits" in settings
        assert "default_file_read_lines" in settings["api_limits"]
        assert "max_file_read_lines" in settings["api_limits"]
        assert "default_diff_lines" in settings["api_limits"]
        assert "max_diff_lines" in settings["api_limits"]
        assert "default_log_commits" in settings["api_limits"]
        assert "max_log_commits" in settings["api_limits"]

    def test_get_all_settings_api_limits_default_values(self, tmp_path):
        """AC19-AC24: api_limits has correct default values."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()

        assert settings["api_limits"]["default_file_read_lines"] == 500
        assert settings["api_limits"]["max_file_read_lines"] == 5000
        assert settings["api_limits"]["default_diff_lines"] == 500
        assert settings["api_limits"]["max_diff_lines"] == 5000
        assert settings["api_limits"]["default_log_commits"] == 50
        assert settings["api_limits"]["max_log_commits"] == 500

    def test_get_all_settings_contains_web_security(self, tmp_path):
        """AC25-AC26: get_all_settings includes web_security section."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()

        assert "web_security" in settings
        assert "csrf_max_age_seconds" in settings["web_security"]
        assert "web_session_timeout_seconds" in settings["web_security"]

    def test_get_all_settings_web_security_default_values(self, tmp_path):
        """AC25-AC26: web_security has correct default values."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()

        assert settings["web_security"]["csrf_max_age_seconds"] == 600
        assert settings["web_security"]["web_session_timeout_seconds"] == 28800


class TestConfigServiceUpdateSettingGitTimeouts:
    """Test ConfigService.update_setting() for git_timeouts category."""

    def test_update_git_local_timeout(self, tmp_path):
        """AC12: Can update git_local_timeout via ConfigService."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("git_timeouts", "git_local_timeout", 60)

        config = service.get_config()
        assert config.git_timeouts_config.git_local_timeout == 60

    def test_update_git_remote_timeout(self, tmp_path):
        """AC14: Can update git_remote_timeout via ConfigService."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("git_timeouts", "git_remote_timeout", 600)

        config = service.get_config()
        assert config.git_timeouts_config.git_remote_timeout == 600

        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        config = service.get_config()

        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        config = service.get_config()

    def test_update_invalid_git_timeout_key_raises_error(self, tmp_path):
        """Test that invalid git_timeouts key raises ValueError."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        with pytest.raises(ValueError, match="Unknown git timeouts setting"):
            service.update_setting("git_timeouts", "invalid_key", 100)

    def test_validation_rejects_git_local_timeout_below_minimum(self, tmp_path):
        """AC13: git_local_timeout below 5 seconds is rejected."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        with pytest.raises(ValueError, match="git_local_timeout"):
            service.update_setting("git_timeouts", "git_local_timeout", 4)

    def test_validation_rejects_git_remote_timeout_below_minimum(self, tmp_path):
        """AC14: git_remote_timeout below 30 seconds is rejected."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        with pytest.raises(ValueError, match="git_remote_timeout"):
            service.update_setting("git_timeouts", "git_remote_timeout", 29)


class TestConfigServiceUpdateSettingErrorHandling:
    """Test ConfigService.update_setting() for error_handling category."""

    def test_update_max_retry_attempts(self, tmp_path):
        """AC16: Can update max_retry_attempts via ConfigService."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("error_handling", "max_retry_attempts", 5)

        config = service.get_config()
        assert config.error_handling_config.max_retry_attempts == 5

    def test_update_base_retry_delay_seconds(self, tmp_path):
        """AC17: Can update base_retry_delay_seconds via ConfigService."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("error_handling", "base_retry_delay_seconds", 0.5)

        config = service.get_config()
        assert config.error_handling_config.base_retry_delay_seconds == 0.5

    def test_update_max_retry_delay_seconds(self, tmp_path):
        """AC18: Can update max_retry_delay_seconds via ConfigService."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("error_handling", "max_retry_delay_seconds", 120.0)

        config = service.get_config()
        assert config.error_handling_config.max_retry_delay_seconds == 120.0

    def test_update_invalid_error_handling_key_raises_error(self, tmp_path):
        """Test that invalid error_handling key raises ValueError."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        with pytest.raises(ValueError, match="Unknown error handling setting"):
            service.update_setting("error_handling", "invalid_key", 100)

    def test_validation_rejects_max_retry_attempts_below_minimum(self, tmp_path):
        """AC16: max_retry_attempts below 1 is rejected."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        with pytest.raises(ValueError, match="max_retry_attempts"):
            service.update_setting("error_handling", "max_retry_attempts", 0)

    def test_validation_rejects_max_retry_attempts_above_maximum(self, tmp_path):
        """AC16: max_retry_attempts above 10 is rejected."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        with pytest.raises(ValueError, match="max_retry_attempts"):
            service.update_setting("error_handling", "max_retry_attempts", 11)


class TestConfigServiceUpdateSettingApiLimits:
    """Test ConfigService.update_setting() for api_limits category."""

    def test_update_default_file_read_lines(self, tmp_path):
        """AC19: Can update default_file_read_lines via ConfigService."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("api_limits", "default_file_read_lines", 1000)

        config = service.get_config()
        assert config.api_limits_config.default_file_read_lines == 1000

    def test_update_max_file_read_lines(self, tmp_path):
        """AC20: Can update max_file_read_lines via ConfigService."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("api_limits", "max_file_read_lines", 10000)

        config = service.get_config()
        assert config.api_limits_config.max_file_read_lines == 10000

    def test_update_default_diff_lines(self, tmp_path):
        """AC21: Can update default_diff_lines via ConfigService."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("api_limits", "default_diff_lines", 1000)

        config = service.get_config()
        assert config.api_limits_config.default_diff_lines == 1000

    def test_update_max_diff_lines(self, tmp_path):
        """AC22: Can update max_diff_lines via ConfigService."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("api_limits", "max_diff_lines", 10000)

        config = service.get_config()
        assert config.api_limits_config.max_diff_lines == 10000

    def test_update_default_log_commits(self, tmp_path):
        """AC23: Can update default_log_commits via ConfigService."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("api_limits", "default_log_commits", 100)

        config = service.get_config()
        assert config.api_limits_config.default_log_commits == 100

    def test_update_max_log_commits(self, tmp_path):
        """AC24: Can update max_log_commits via ConfigService."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("api_limits", "max_log_commits", 1000)

        config = service.get_config()
        assert config.api_limits_config.max_log_commits == 1000

    def test_update_invalid_api_limits_key_raises_error(self, tmp_path):
        """Test that invalid api_limits key raises ValueError."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        with pytest.raises(ValueError, match="Unknown API limits setting"):
            service.update_setting("api_limits", "invalid_key", 100)

    def test_validation_rejects_default_file_read_lines_below_minimum(self, tmp_path):
        """AC19: default_file_read_lines below 1 is rejected."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        with pytest.raises(ValueError, match="default_file_read_lines"):
            service.update_setting("api_limits", "default_file_read_lines", 0)


class TestConfigServiceUpdateSettingWebSecurity:
    """Test ConfigService.update_setting() for web_security category."""

    def test_update_csrf_max_age_seconds(self, tmp_path):
        """AC25: Can update csrf_max_age_seconds via ConfigService."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("web_security", "csrf_max_age_seconds", 1200)

        config = service.get_config()
        assert config.web_security_config.csrf_max_age_seconds == 1200

    def test_update_web_session_timeout_seconds(self, tmp_path):
        """AC26: Can update web_session_timeout_seconds via ConfigService."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("web_security", "web_session_timeout_seconds", 43200)

        config = service.get_config()
        assert config.web_security_config.web_session_timeout_seconds == 43200

    def test_update_invalid_web_security_key_raises_error(self, tmp_path):
        """Test that invalid web_security key raises ValueError."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        with pytest.raises(ValueError, match="Unknown web security setting"):
            service.update_setting("web_security", "invalid_key", 100)

    def test_validation_rejects_csrf_max_age_below_minimum(self, tmp_path):
        """AC25: csrf_max_age_seconds below 60 is rejected."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        with pytest.raises(ValueError, match="csrf_max_age_seconds"):
            service.update_setting("web_security", "csrf_max_age_seconds", 59)

    def test_validation_rejects_csrf_max_age_above_maximum(self, tmp_path):
        """AC25: csrf_max_age_seconds above 3600 is rejected."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        with pytest.raises(ValueError, match="csrf_max_age_seconds"):
            service.update_setting("web_security", "csrf_max_age_seconds", 3601)

    def test_validation_rejects_web_session_timeout_below_minimum(self, tmp_path):
        """AC26: web_session_timeout_seconds below 1800 is rejected."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        with pytest.raises(ValueError, match="web_session_timeout_seconds"):
            service.update_setting("web_security", "web_session_timeout_seconds", 1799)

    def test_validation_rejects_web_session_timeout_above_maximum(self, tmp_path):
        """AC26: web_session_timeout_seconds above 86400 is rejected."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        with pytest.raises(ValueError, match="web_session_timeout_seconds"):
            service.update_setting("web_security", "web_session_timeout_seconds", 86401)


class TestConfigServiceP2Persistence:
    """Test ConfigService P2 settings persistence."""

    def test_git_timeouts_persist_to_file(self, tmp_path):
        """Test that git_timeouts settings persist to config file."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("git_timeouts", "git_local_timeout", 60)
        service.update_setting("git_timeouts", "git_remote_timeout", 600)

        # Load with new service instance
        new_service = ConfigService(server_dir_path=str(tmp_path))
        config = new_service.load_config()

        assert config.git_timeouts_config.git_local_timeout == 60
        assert config.git_timeouts_config.git_remote_timeout == 600

    def test_error_handling_persists_to_file(self, tmp_path):
        """Test that error_handling settings persist to config file."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("error_handling", "max_retry_attempts", 5)
        service.update_setting("error_handling", "base_retry_delay_seconds", 0.5)

        # Load with new service instance
        new_service = ConfigService(server_dir_path=str(tmp_path))
        config = new_service.load_config()

        assert config.error_handling_config.max_retry_attempts == 5
        assert config.error_handling_config.base_retry_delay_seconds == 0.5

    def test_api_limits_persists_to_file(self, tmp_path):
        """Test that api_limits settings persist to config file."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("api_limits", "default_file_read_lines", 1000)
        service.update_setting("api_limits", "max_log_commits", 1000)

        # Load with new service instance
        new_service = ConfigService(server_dir_path=str(tmp_path))
        config = new_service.load_config()

        assert config.api_limits_config.default_file_read_lines == 1000
        assert config.api_limits_config.max_log_commits == 1000

    def test_web_security_persists_to_file(self, tmp_path):
        """Test that web_security settings persist to config file."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("web_security", "csrf_max_age_seconds", 1200)
        service.update_setting("web_security", "web_session_timeout_seconds", 43200)

        # Load with new service instance
        new_service = ConfigService(server_dir_path=str(tmp_path))
        config = new_service.load_config()

        assert config.web_security_config.csrf_max_age_seconds == 1200
        assert config.web_security_config.web_session_timeout_seconds == 43200
