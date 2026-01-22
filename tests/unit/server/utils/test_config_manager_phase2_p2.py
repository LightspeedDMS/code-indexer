"""
Unit tests for Phase 2 P2 Priority Configuration (AC12-AC26).

Story #3: Server Configuration Consolidation
Tests for GitTimeoutsConfig, ErrorHandlingConfig, ApiLimitsConfig, WebSecurityConfig.
"""

import tempfile
import pytest
from dataclasses import asdict

from code_indexer.server.utils.config_manager import (
    ServerConfigManager,
    ServerConfig,
    GitTimeoutsConfig,
    ErrorHandlingConfig,
    ApiLimitsConfig,
    WebSecurityConfig,
)


class TestGitTimeoutsConfig:
    """Tests for AC12-AC15: Git Operation Timeouts."""

    def test_default_values(self):
        """AC12: GitTimeoutsConfig has correct default values."""
        config = GitTimeoutsConfig()
        assert config.git_local_timeout == 30
        assert config.git_remote_timeout == 300
        assert config.git_command_timeout == 30
        assert config.git_fetch_timeout == 60

    def test_custom_values(self):
        """GitTimeoutsConfig accepts custom values."""
        config = GitTimeoutsConfig(
            git_local_timeout=60,
            git_remote_timeout=600,
            git_command_timeout=45,
            git_fetch_timeout=120,
        )
        assert config.git_local_timeout == 60
        assert config.git_remote_timeout == 600
        assert config.git_command_timeout == 45
        assert config.git_fetch_timeout == 120

    def test_serialization(self):
        """GitTimeoutsConfig serializes to dict correctly."""
        config = GitTimeoutsConfig()
        data = asdict(config)
        assert data == {
            "git_local_timeout": 30,
            "git_remote_timeout": 300,
            "git_command_timeout": 30,
            "git_fetch_timeout": 60,
            # P3 API Provider Timeouts (AC27-AC30)
            "github_api_timeout": 30,
            "gitlab_api_timeout": 30,
            "github_provider_timeout": 30,
            "gitlab_provider_timeout": 30,
        }

    def test_validation_git_local_timeout_minimum(self):
        """AC13: git_local_timeout minimum is 5 seconds."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                git_timeouts_config=GitTimeoutsConfig(git_local_timeout=4),
            )
            with pytest.raises(ValueError, match="git_local_timeout"):
                manager.validate_config(server_config)

    def test_validation_git_remote_timeout_minimum(self):
        """AC14: git_remote_timeout minimum is 30 seconds."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                git_timeouts_config=GitTimeoutsConfig(git_remote_timeout=29),
            )
            with pytest.raises(ValueError, match="git_remote_timeout"):
                manager.validate_config(server_config)

    def test_validation_git_command_timeout_minimum(self):
        """AC15: git_command_timeout minimum is 5 seconds."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                git_timeouts_config=GitTimeoutsConfig(git_command_timeout=4),
            )
            with pytest.raises(ValueError, match="git_command_timeout"):
                manager.validate_config(server_config)

    def test_validation_git_fetch_timeout_minimum(self):
        """AC15: git_fetch_timeout minimum is 10 seconds."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                git_timeouts_config=GitTimeoutsConfig(git_fetch_timeout=9),
            )
            with pytest.raises(ValueError, match="git_fetch_timeout"):
                manager.validate_config(server_config)

    def test_valid_boundary_values(self):
        """All timeout values at minimum boundaries pass validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                git_timeouts_config=GitTimeoutsConfig(
                    git_local_timeout=5,
                    git_remote_timeout=30,
                    git_command_timeout=5,
                    git_fetch_timeout=10,
                ),
            )
            # Should not raise
            manager.validate_config(server_config)


class TestErrorHandlingConfig:
    """Tests for AC16-AC18: Error Handling and Retry Configuration."""

    def test_default_values(self):
        """AC16: ErrorHandlingConfig has correct default values."""
        config = ErrorHandlingConfig()
        assert config.max_retry_attempts == 3
        assert config.base_retry_delay_seconds == 0.1
        assert config.max_retry_delay_seconds == 60.0

    def test_custom_values(self):
        """ErrorHandlingConfig accepts custom values."""
        config = ErrorHandlingConfig(
            max_retry_attempts=5,
            base_retry_delay_seconds=0.5,
            max_retry_delay_seconds=120.0,
        )
        assert config.max_retry_attempts == 5
        assert config.base_retry_delay_seconds == 0.5
        assert config.max_retry_delay_seconds == 120.0

    def test_serialization(self):
        """ErrorHandlingConfig serializes to dict correctly."""
        config = ErrorHandlingConfig()
        data = asdict(config)
        assert data == {
            "max_retry_attempts": 3,
            "base_retry_delay_seconds": 0.1,
            "max_retry_delay_seconds": 60.0,
        }

    def test_validation_max_retry_attempts_range(self):
        """AC17: max_retry_attempts must be in range 1-10."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                error_handling_config=ErrorHandlingConfig(max_retry_attempts=0),
            )
            with pytest.raises(ValueError, match="max_retry_attempts"):
                manager.validate_config(server_config)

    def test_validation_base_retry_delay_range(self):
        """AC18: base_retry_delay_seconds must be in range 0.01-5.0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                error_handling_config=ErrorHandlingConfig(base_retry_delay_seconds=0.005),
            )
            with pytest.raises(ValueError, match="base_retry_delay_seconds"):
                manager.validate_config(server_config)

    def test_validation_max_retry_delay_range(self):
        """AC18: max_retry_delay_seconds must be in range 1-300."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                error_handling_config=ErrorHandlingConfig(max_retry_delay_seconds=0.5),
            )
            with pytest.raises(ValueError, match="max_retry_delay_seconds"):
                manager.validate_config(server_config)

    def test_valid_boundary_values(self):
        """All error handling values at boundaries pass validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                error_handling_config=ErrorHandlingConfig(
                    max_retry_attempts=1,
                    base_retry_delay_seconds=0.01,
                    max_retry_delay_seconds=1.0,
                ),
            )
            manager.validate_config(server_config)


class TestApiLimitsConfig:
    """Tests for AC19-AC24: API Response Limits Configuration."""

    def test_default_values(self):
        """AC19: ApiLimitsConfig has correct default values."""
        config = ApiLimitsConfig()
        assert config.default_file_read_lines == 500
        assert config.max_file_read_lines == 5000
        assert config.default_diff_lines == 500
        assert config.max_diff_lines == 5000
        assert config.default_log_commits == 50
        assert config.max_log_commits == 500

    def test_custom_values(self):
        """ApiLimitsConfig accepts custom values."""
        config = ApiLimitsConfig(
            default_file_read_lines=200,
            max_file_read_lines=10000,
            default_diff_lines=300,
            max_diff_lines=8000,
            default_log_commits=100,
            max_log_commits=1000,
        )
        assert config.default_file_read_lines == 200
        assert config.max_file_read_lines == 10000

    def test_serialization(self):
        """ApiLimitsConfig serializes to dict correctly."""
        config = ApiLimitsConfig()
        data = asdict(config)
        assert data == {
            "default_file_read_lines": 500,
            "max_file_read_lines": 5000,
            "default_diff_lines": 500,
            "max_diff_lines": 5000,
            "default_log_commits": 50,
            "max_log_commits": 500,
            # P3 Miscellaneous Limits (AC35, AC38-AC39)
            "audit_log_default_limit": 100,
            "log_page_size_default": 50,
            "log_page_size_max": 500,
        }

    def test_validation_default_file_read_lines_range(self):
        """AC20: default_file_read_lines must be in range 100-5000."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                api_limits_config=ApiLimitsConfig(default_file_read_lines=99),
            )
            with pytest.raises(ValueError, match="default_file_read_lines"):
                manager.validate_config(server_config)

    def test_validation_max_file_read_lines_range(self):
        """AC20: max_file_read_lines must be in range 500-50000."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                api_limits_config=ApiLimitsConfig(max_file_read_lines=499),
            )
            with pytest.raises(ValueError, match="max_file_read_lines"):
                manager.validate_config(server_config)

    def test_validation_default_diff_lines_range(self):
        """AC21-22: default_diff_lines must be in range 100-5000."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                api_limits_config=ApiLimitsConfig(default_diff_lines=99),
            )
            with pytest.raises(ValueError, match="default_diff_lines"):
                manager.validate_config(server_config)

    def test_validation_max_diff_lines_range(self):
        """AC21-22: max_diff_lines must be in range 500-50000."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                api_limits_config=ApiLimitsConfig(max_diff_lines=499),
            )
            with pytest.raises(ValueError, match="max_diff_lines"):
                manager.validate_config(server_config)

    def test_validation_default_log_commits_range(self):
        """AC23-24: default_log_commits must be in range 10-500."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                api_limits_config=ApiLimitsConfig(default_log_commits=9),
            )
            with pytest.raises(ValueError, match="default_log_commits"):
                manager.validate_config(server_config)

    def test_validation_max_log_commits_range(self):
        """AC23-24: max_log_commits must be in range 50-5000."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                api_limits_config=ApiLimitsConfig(max_log_commits=49),
            )
            with pytest.raises(ValueError, match="max_log_commits"):
                manager.validate_config(server_config)

    def test_valid_boundary_values(self):
        """All API limit values at boundaries pass validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                api_limits_config=ApiLimitsConfig(
                    default_file_read_lines=100,
                    max_file_read_lines=500,
                    default_diff_lines=100,
                    max_diff_lines=500,
                    default_log_commits=10,
                    max_log_commits=50,
                ),
            )
            manager.validate_config(server_config)


class TestWebSecurityConfig:
    """Tests for AC25-AC26: Web Security Configuration."""

    def test_default_values(self):
        """AC25: WebSecurityConfig has correct default values."""
        config = WebSecurityConfig()
        assert config.csrf_max_age_seconds == 600
        assert config.web_session_timeout_seconds == 28800

    def test_custom_values(self):
        """WebSecurityConfig accepts custom values."""
        config = WebSecurityConfig(
            csrf_max_age_seconds=1200,
            web_session_timeout_seconds=43200,
        )
        assert config.csrf_max_age_seconds == 1200
        assert config.web_session_timeout_seconds == 43200

    def test_serialization(self):
        """WebSecurityConfig serializes to dict correctly."""
        config = WebSecurityConfig()
        data = asdict(config)
        assert data == {
            "csrf_max_age_seconds": 600,
            "web_session_timeout_seconds": 28800,
        }

    def test_validation_csrf_max_age_range(self):
        """AC26: csrf_max_age_seconds must be in range 60-3600."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                web_security_config=WebSecurityConfig(csrf_max_age_seconds=59),
            )
            with pytest.raises(ValueError, match="csrf_max_age_seconds"):
                manager.validate_config(server_config)

    def test_validation_web_session_timeout_range(self):
        """AC26: web_session_timeout_seconds must be in range 1800-86400."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                web_security_config=WebSecurityConfig(web_session_timeout_seconds=1799),
            )
            with pytest.raises(ValueError, match="web_session_timeout_seconds"):
                manager.validate_config(server_config)

    def test_valid_boundary_values(self):
        """All web security values at boundaries pass validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            server_config = ServerConfig(
                server_dir=tmpdir,
                web_security_config=WebSecurityConfig(
                    csrf_max_age_seconds=60,
                    web_session_timeout_seconds=1800,
                ),
            )
            manager.validate_config(server_config)


class TestServerConfigP2Integration:
    """Test suite for ServerConfig integration with P2 config objects."""

    def test_server_config_has_git_timeouts_config(self):
        """Test ServerConfig has git_timeouts_config attribute initialized."""
        config = ServerConfig(server_dir="/tmp/test")

        assert hasattr(config, "git_timeouts_config")
        assert config.git_timeouts_config is not None
        assert isinstance(config.git_timeouts_config, GitTimeoutsConfig)

    def test_server_config_has_error_handling_config(self):
        """Test ServerConfig has error_handling_config attribute initialized."""
        config = ServerConfig(server_dir="/tmp/test")

        assert hasattr(config, "error_handling_config")
        assert config.error_handling_config is not None
        assert isinstance(config.error_handling_config, ErrorHandlingConfig)

    def test_server_config_has_api_limits_config(self):
        """Test ServerConfig has api_limits_config attribute initialized."""
        config = ServerConfig(server_dir="/tmp/test")

        assert hasattr(config, "api_limits_config")
        assert config.api_limits_config is not None
        assert isinstance(config.api_limits_config, ApiLimitsConfig)

    def test_server_config_has_web_security_config(self):
        """Test ServerConfig has web_security_config attribute initialized."""
        config = ServerConfig(server_dir="/tmp/test")

        assert hasattr(config, "web_security_config")
        assert config.web_security_config is not None
        assert isinstance(config.web_security_config, WebSecurityConfig)

    def test_save_load_preserves_p2_configs(self):
        """Test P2 configs are properly serialized/deserialized via JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a config with custom P2 values
            manager = ServerConfigManager(tmpdir)
            original_config = ServerConfig(
                server_dir=tmpdir,
                git_timeouts_config=GitTimeoutsConfig(
                    git_local_timeout=60,
                    git_remote_timeout=600,
                    git_command_timeout=45,
                    git_fetch_timeout=120,
                ),
                error_handling_config=ErrorHandlingConfig(
                    max_retry_attempts=5,
                    base_retry_delay_seconds=0.5,
                    max_retry_delay_seconds=120.0,
                ),
                api_limits_config=ApiLimitsConfig(
                    default_file_read_lines=200,
                    max_file_read_lines=10000,
                    default_diff_lines=300,
                    max_diff_lines=8000,
                    default_log_commits=100,
                    max_log_commits=1000,
                ),
                web_security_config=WebSecurityConfig(
                    csrf_max_age_seconds=1200,
                    web_session_timeout_seconds=43200,
                ),
            )

            # Save and reload
            manager.save_config(original_config)
            loaded_config = manager.load_config()

            # Verify P2 configs are dataclass instances, not dicts
            assert loaded_config is not None

            # Verify git_timeouts_config
            assert isinstance(loaded_config.git_timeouts_config, GitTimeoutsConfig)
            assert loaded_config.git_timeouts_config.git_local_timeout == 60
            assert loaded_config.git_timeouts_config.git_remote_timeout == 600
            assert loaded_config.git_timeouts_config.git_command_timeout == 45
            assert loaded_config.git_timeouts_config.git_fetch_timeout == 120

            # Verify error_handling_config
            assert isinstance(loaded_config.error_handling_config, ErrorHandlingConfig)
            assert loaded_config.error_handling_config.max_retry_attempts == 5
            assert loaded_config.error_handling_config.base_retry_delay_seconds == 0.5
            assert loaded_config.error_handling_config.max_retry_delay_seconds == 120.0

            # Verify api_limits_config
            assert isinstance(loaded_config.api_limits_config, ApiLimitsConfig)
            assert loaded_config.api_limits_config.default_file_read_lines == 200
            assert loaded_config.api_limits_config.max_file_read_lines == 10000
            assert loaded_config.api_limits_config.default_diff_lines == 300
            assert loaded_config.api_limits_config.max_diff_lines == 8000
            assert loaded_config.api_limits_config.default_log_commits == 100
            assert loaded_config.api_limits_config.max_log_commits == 1000

            # Verify web_security_config
            assert isinstance(loaded_config.web_security_config, WebSecurityConfig)
            assert loaded_config.web_security_config.csrf_max_age_seconds == 1200
            assert loaded_config.web_security_config.web_session_timeout_seconds == 43200
