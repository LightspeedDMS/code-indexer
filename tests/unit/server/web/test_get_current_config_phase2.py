"""
Unit tests for _get_current_config() function (Story #3 - Phase 2).

Tests that the function returns all Phase 2 config sections:
- P0/P1: mcp_session, health, scip
- P2: git_timeouts, error_handling, api_limits, web_security

These tests verify the bug fix for missing config sections that caused
Web UI rendering failures when templates referenced undefined variables.
"""

from unittest.mock import patch, MagicMock


class TestGetCurrentConfigPhase2Sections:
    """Test suite for Phase 2 config sections in _get_current_config()."""

    def test_returns_mcp_session_section(self):
        """Test _get_current_config includes mcp_session section with defaults."""
        from src.code_indexer.server.web.routes import _get_current_config

        # Mock the config service to return empty settings (backward compatibility)
        mock_config_service = MagicMock()
        mock_config_service.get_all_settings.return_value = {
            "server": {"host": "127.0.0.1", "port": 8000},
            "cache": {},
            "reindexing": {},
            "timeouts": {},
            "password_security": {},
        }

        with patch(
            "src.code_indexer.server.services.config_service.get_config_service",
            return_value=mock_config_service,
        ):
            config = _get_current_config()

        assert "mcp_session" in config, "mcp_session section should exist in config"
        assert (
            "session_ttl_seconds" in config["mcp_session"]
        ), "session_ttl_seconds should exist"
        assert (
            "cleanup_interval_seconds" in config["mcp_session"]
        ), "cleanup_interval_seconds should exist"
        # Check default values
        assert config["mcp_session"]["session_ttl_seconds"] == 3600
        assert config["mcp_session"]["cleanup_interval_seconds"] == 900

    def test_returns_health_section(self):
        """Test _get_current_config includes health section with defaults."""
        from src.code_indexer.server.web.routes import _get_current_config

        mock_config_service = MagicMock()
        mock_config_service.get_all_settings.return_value = {
            "server": {"host": "127.0.0.1", "port": 8000},
            "cache": {},
            "reindexing": {},
            "timeouts": {},
            "password_security": {},
        }

        with patch(
            "src.code_indexer.server.services.config_service.get_config_service",
            return_value=mock_config_service,
        ):
            config = _get_current_config()

        assert "health" in config, "health section should exist in config"
        assert "memory_warning_threshold_percent" in config["health"]
        assert "memory_critical_threshold_percent" in config["health"]
        assert "disk_warning_threshold_percent" in config["health"]
        assert "disk_critical_threshold_percent" in config["health"]
        assert "cpu_sustained_threshold_percent" in config["health"]
        # Check default values
        assert config["health"]["memory_warning_threshold_percent"] == 80.0
        assert config["health"]["memory_critical_threshold_percent"] == 90.0

    def test_returns_scip_section(self):
        """Test _get_current_config includes scip section with defaults."""
        from src.code_indexer.server.web.routes import _get_current_config

        mock_config_service = MagicMock()
        mock_config_service.get_all_settings.return_value = {
            "server": {"host": "127.0.0.1", "port": 8000},
            "cache": {},
            "reindexing": {},
            "timeouts": {},
            "password_security": {},
        }

        with patch(
            "src.code_indexer.server.services.config_service.get_config_service",
            return_value=mock_config_service,
        ):
            config = _get_current_config()

        assert "scip" in config, "scip section should exist in config"
        assert "indexing_timeout_seconds" in config["scip"]
        assert "scip_generation_timeout_seconds" in config["scip"]
        assert "temporal_stale_threshold_days" in config["scip"]
        # Check default values
        assert config["scip"]["indexing_timeout_seconds"] == 3600
        assert config["scip"]["scip_generation_timeout_seconds"] == 600
        assert config["scip"]["temporal_stale_threshold_days"] == 7

    def test_returns_git_timeouts_section(self):
        """Test _get_current_config includes git_timeouts section with defaults."""
        from src.code_indexer.server.web.routes import _get_current_config

        mock_config_service = MagicMock()
        mock_config_service.get_all_settings.return_value = {
            "server": {"host": "127.0.0.1", "port": 8000},
            "cache": {},
            "reindexing": {},
            "timeouts": {},
            "password_security": {},
        }

        with patch(
            "src.code_indexer.server.services.config_service.get_config_service",
            return_value=mock_config_service,
        ):
            config = _get_current_config()

        assert "git_timeouts" in config, "git_timeouts section should exist in config"
        assert "git_local_timeout" in config["git_timeouts"]
        assert "git_remote_timeout" in config["git_timeouts"]
        # Check default values (AC12-AC15)
        assert config["git_timeouts"]["git_local_timeout"] == 30
        assert config["git_timeouts"]["git_remote_timeout"] == 300

    def test_returns_error_handling_section(self):
        """Test _get_current_config includes error_handling section with defaults."""
        from src.code_indexer.server.web.routes import _get_current_config

        mock_config_service = MagicMock()
        mock_config_service.get_all_settings.return_value = {
            "server": {"host": "127.0.0.1", "port": 8000},
            "cache": {},
            "reindexing": {},
            "timeouts": {},
            "password_security": {},
        }

        with patch(
            "src.code_indexer.server.services.config_service.get_config_service",
            return_value=mock_config_service,
        ):
            config = _get_current_config()

        assert (
            "error_handling" in config
        ), "error_handling section should exist in config"
        assert "max_retry_attempts" in config["error_handling"]
        assert "base_retry_delay_seconds" in config["error_handling"]
        assert "max_retry_delay_seconds" in config["error_handling"]
        # Check default values (AC16-AC18)
        assert config["error_handling"]["max_retry_attempts"] == 3
        assert config["error_handling"]["base_retry_delay_seconds"] == 0.1
        assert config["error_handling"]["max_retry_delay_seconds"] == 60.0

    def test_returns_api_limits_section(self):
        """Test _get_current_config includes api_limits section with defaults."""
        from src.code_indexer.server.web.routes import _get_current_config

        mock_config_service = MagicMock()
        mock_config_service.get_all_settings.return_value = {
            "server": {"host": "127.0.0.1", "port": 8000},
            "cache": {},
            "reindexing": {},
            "timeouts": {},
            "password_security": {},
        }

        with patch(
            "src.code_indexer.server.services.config_service.get_config_service",
            return_value=mock_config_service,
        ):
            config = _get_current_config()

        assert "api_limits" in config, "api_limits section should exist in config"
        assert "default_file_read_lines" in config["api_limits"]
        assert "max_file_read_lines" in config["api_limits"]
        assert "default_diff_lines" in config["api_limits"]
        assert "max_diff_lines" in config["api_limits"]
        assert "default_log_commits" in config["api_limits"]
        assert "max_log_commits" in config["api_limits"]
        # Check default values (AC19-AC24)
        assert config["api_limits"]["default_file_read_lines"] == 500
        assert config["api_limits"]["max_file_read_lines"] == 5000
        assert config["api_limits"]["default_diff_lines"] == 500
        assert config["api_limits"]["max_diff_lines"] == 5000
        assert config["api_limits"]["default_log_commits"] == 50
        assert config["api_limits"]["max_log_commits"] == 500

    def test_returns_web_security_section(self):
        """Test _get_current_config includes web_security section with defaults."""
        from src.code_indexer.server.web.routes import _get_current_config

        mock_config_service = MagicMock()
        mock_config_service.get_all_settings.return_value = {
            "server": {"host": "127.0.0.1", "port": 8000},
            "cache": {},
            "reindexing": {},
            "timeouts": {},
            "password_security": {},
        }

        with patch(
            "src.code_indexer.server.services.config_service.get_config_service",
            return_value=mock_config_service,
        ):
            config = _get_current_config()

        assert "web_security" in config, "web_security section should exist in config"
        assert "csrf_max_age_seconds" in config["web_security"]
        assert "web_session_timeout_seconds" in config["web_security"]
        # Check default values (AC25-AC26)
        assert config["web_security"]["csrf_max_age_seconds"] == 600
        assert config["web_security"]["web_session_timeout_seconds"] == 28800

    def test_uses_persisted_values_when_available(self):
        """Test _get_current_config uses persisted values over defaults when available."""
        from src.code_indexer.server.web.routes import _get_current_config

        mock_config_service = MagicMock()
        mock_config_service.get_all_settings.return_value = {
            "server": {"host": "127.0.0.1", "port": 8000},
            "cache": {},
            "reindexing": {},
            "timeouts": {},
            "password_security": {},
            # Persisted Phase 2 values
            "mcp_session": {
                "session_ttl_seconds": 7200,
                "cleanup_interval_seconds": 600,
            },
            "health": {
                "memory_warning_threshold_percent": 75.0,
                "memory_critical_threshold_percent": 85.0,
                "disk_warning_threshold_percent": 75.0,
                "disk_critical_threshold_percent": 85.0,
                "cpu_sustained_threshold_percent": 90.0,
            },
            "scip": {
                "indexing_timeout_seconds": 7200,
                "scip_generation_timeout_seconds": 1200,
                "temporal_stale_threshold_days": 14,
            },
            "error_handling": {
                "max_retry_attempts": 5,
                "base_retry_delay_seconds": 0.2,
                "max_retry_delay_seconds": 120.0,
            },
            "api_limits": {
                "default_file_read_lines": 1000,
                "max_file_read_lines": 10000,
                "default_diff_lines": 1000,
                "max_diff_lines": 10000,
                "default_log_commits": 100,
                "max_log_commits": 1000,
            },
            "web_security": {
                "csrf_max_age_seconds": 1200,
                "web_session_timeout_seconds": 43200,
            },
        }

        with patch(
            "src.code_indexer.server.services.config_service.get_config_service",
            return_value=mock_config_service,
        ):
            config = _get_current_config()

        # Verify persisted values are used
        assert config["mcp_session"]["session_ttl_seconds"] == 7200
        assert config["health"]["memory_warning_threshold_percent"] == 75.0
        assert config["scip"]["indexing_timeout_seconds"] == 7200
        assert config["git_timeouts"]["git_local_timeout"] == 60
        assert config["error_handling"]["max_retry_attempts"] == 5
        assert config["api_limits"]["default_file_read_lines"] == 1000
        assert config["web_security"]["csrf_max_age_seconds"] == 1200

    def test_all_phase2_sections_present(self):
        """Test _get_current_config includes all Phase 2 sections in return dict."""
        from src.code_indexer.server.web.routes import _get_current_config

        mock_config_service = MagicMock()
        mock_config_service.get_all_settings.return_value = {
            "server": {"host": "127.0.0.1", "port": 8000},
            "cache": {},
            "reindexing": {},
            "timeouts": {},
            "password_security": {},
        }

        with patch(
            "src.code_indexer.server.services.config_service.get_config_service",
            return_value=mock_config_service,
        ):
            config = _get_current_config()

        # All P0/P1 Phase 2 sections
        assert "mcp_session" in config
        assert "health" in config
        assert "scip" in config

        # All P2 Phase 2 sections (AC12-AC26)
        assert "git_timeouts" in config
        assert "error_handling" in config
        assert "api_limits" in config
        assert "web_security" in config

        # P3 Phase 2 section (AC36)
        assert "auth" in config

    def test_returns_auth_section(self):
        """Test _get_current_config includes auth section with defaults (AC36)."""
        from src.code_indexer.server.web.routes import _get_current_config

        mock_config_service = MagicMock()
        mock_config_service.get_all_settings.return_value = {
            "server": {"host": "127.0.0.1", "port": 8000},
            "cache": {},
            "reindexing": {},
            "timeouts": {},
            "password_security": {},
        }

        with patch(
            "src.code_indexer.server.services.config_service.get_config_service",
            return_value=mock_config_service,
        ):
            config = _get_current_config()

        assert "auth" in config, "auth section should exist in config"
        assert "oauth_extension_threshold_hours" in config["auth"]
        # Check default value (AC36: 4 hours)
        assert config["auth"]["oauth_extension_threshold_hours"] == 4


class TestValidateConfigSectionPhase2:
    """Test suite for _validate_config_section() Phase 2 validations."""

    def test_auth_section_validates_oauth_threshold_in_range(self):
        """Test auth section validation accepts valid oauth_extension_threshold_hours."""
        from src.code_indexer.server.web.routes import _validate_config_section

        # Valid value within range (1-24 hours)
        result = _validate_config_section(
            "auth", {"oauth_extension_threshold_hours": 4}
        )
        assert result is None, "Valid oauth threshold should pass validation"

        result = _validate_config_section(
            "auth", {"oauth_extension_threshold_hours": 1}
        )
        assert result is None, "Min oauth threshold (1) should pass validation"

        result = _validate_config_section(
            "auth", {"oauth_extension_threshold_hours": 24}
        )
        assert result is None, "Max oauth threshold (24) should pass validation"

    def test_auth_section_rejects_oauth_threshold_below_min(self):
        """Test auth section validation rejects oauth_extension_threshold_hours below minimum."""
        from src.code_indexer.server.web.routes import _validate_config_section

        result = _validate_config_section(
            "auth", {"oauth_extension_threshold_hours": 0}
        )
        assert result is not None, "OAuth threshold below min should fail validation"
        assert "1" in result and "24" in result, "Error should mention valid range"

    def test_auth_section_rejects_oauth_threshold_above_max(self):
        """Test auth section validation rejects oauth_extension_threshold_hours above maximum."""
        from src.code_indexer.server.web.routes import _validate_config_section

        result = _validate_config_section(
            "auth", {"oauth_extension_threshold_hours": 25}
        )
        assert result is not None, "OAuth threshold above max should fail validation"
        assert "1" in result and "24" in result, "Error should mention valid range"

    def test_auth_section_rejects_invalid_oauth_threshold(self):
        """Test auth section validation rejects non-numeric oauth_extension_threshold_hours."""
        from src.code_indexer.server.web.routes import _validate_config_section

        result = _validate_config_section(
            "auth", {"oauth_extension_threshold_hours": "invalid"}
        )
        assert result is not None, "Non-numeric oauth threshold should fail validation"
        assert "valid number" in result.lower(), "Error should mention valid number"

    def test_health_section_validates_metrics_cache_ttl_in_range(self):
        """Test health section validation accepts valid metrics_cache_ttl_seconds (AC37)."""
        from src.code_indexer.server.web.routes import _validate_config_section

        # Valid value within range (1-60 seconds per constants)
        result = _validate_config_section("health", {"metrics_cache_ttl_seconds": 30})
        assert result is None, "Valid metrics cache TTL should pass validation"

        result = _validate_config_section("health", {"metrics_cache_ttl_seconds": 1})
        assert result is None, "Min metrics cache TTL (1) should pass validation"

        result = _validate_config_section("health", {"metrics_cache_ttl_seconds": 60})
        assert result is None, "Max metrics cache TTL (60) should pass validation"

    def test_health_section_rejects_metrics_cache_ttl_below_min(self):
        """Test health section validation rejects metrics_cache_ttl_seconds below minimum."""
        from src.code_indexer.server.web.routes import _validate_config_section

        result = _validate_config_section("health", {"metrics_cache_ttl_seconds": 0})
        assert result is not None, "Metrics cache TTL below min should fail validation"
        assert "1" in result and "60" in result, "Error should mention valid range"

    def test_health_section_rejects_metrics_cache_ttl_above_max(self):
        """Test health section validation rejects metrics_cache_ttl_seconds above maximum."""
        from src.code_indexer.server.web.routes import _validate_config_section

        result = _validate_config_section("health", {"metrics_cache_ttl_seconds": 61})
        assert result is not None, "Metrics cache TTL above max should fail validation"
        assert "1" in result and "60" in result, "Error should mention valid range"

    def test_health_section_rejects_invalid_metrics_cache_ttl(self):
        """Test health section validation rejects non-numeric metrics_cache_ttl_seconds."""
        from src.code_indexer.server.web.routes import _validate_config_section

        result = _validate_config_section(
            "health", {"metrics_cache_ttl_seconds": "invalid"}
        )
        assert (
            result is not None
        ), "Non-numeric metrics cache TTL should fail validation"
        assert "valid number" in result.lower(), "Error should mention valid number"
