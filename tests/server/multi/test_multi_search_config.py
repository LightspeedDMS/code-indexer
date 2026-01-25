"""
TDD tests for MultiSearchConfig (AC8: Configuration Management).

Tests written FIRST before implementation.

Story #32: Removed environment variable configuration. All configuration
must come from Web UI configuration system (ServerConfig).

Verifies:
- Sensible default values
- Configuration validation
"""

import pytest

from code_indexer.server.multi.multi_search_config import MultiSearchConfig


class TestMultiSearchConfigDefaults:
    """Test default configuration values."""

    def test_max_workers_default(self):
        """Story #25: max_workers defaults to 2 (per resource audit recommendation)."""
        config = MultiSearchConfig()
        assert config.max_workers == 2

    def test_query_timeout_default(self):
        """query_timeout_seconds defaults to 30."""
        config = MultiSearchConfig()
        assert config.query_timeout_seconds == 30

    def test_max_repos_per_query_default(self):
        """max_repos_per_query defaults to 50."""
        config = MultiSearchConfig()
        assert config.max_repos_per_query == 50

    def test_max_results_per_repo_default(self):
        """max_results_per_repo defaults to 100."""
        config = MultiSearchConfig()
        assert config.max_results_per_repo == 100


class TestMultiSearchConfigValidation:
    """Test configuration validation at startup."""

    def test_invalid_max_workers_raises_error(self):
        """max_workers <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="max_workers must be positive"):
            MultiSearchConfig(max_workers=0)

        with pytest.raises(ValueError, match="max_workers must be positive"):
            MultiSearchConfig(max_workers=-1)

    def test_invalid_query_timeout_raises_error(self):
        """query_timeout_seconds <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="query_timeout_seconds must be positive"):
            MultiSearchConfig(query_timeout_seconds=0)

        with pytest.raises(ValueError, match="query_timeout_seconds must be positive"):
            MultiSearchConfig(query_timeout_seconds=-10)

    def test_invalid_max_repos_raises_error(self):
        """max_repos_per_query <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="max_repos_per_query must be positive"):
            MultiSearchConfig(max_repos_per_query=0)

    def test_invalid_max_results_raises_error(self):
        """max_results_per_repo <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="max_results_per_repo must be positive"):
            MultiSearchConfig(max_results_per_repo=0)

    def test_valid_configuration_passes(self):
        """Valid configuration does not raise errors."""
        config = MultiSearchConfig(
            max_workers=5,
            query_timeout_seconds=20,
            max_repos_per_query=25,
            max_results_per_repo=50,
        )
        assert config.max_workers == 5
        assert config.query_timeout_seconds == 20
        assert config.max_repos_per_query == 25
        assert config.max_results_per_repo == 50
