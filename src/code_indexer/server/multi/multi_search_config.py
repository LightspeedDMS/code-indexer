"""
Multi-search configuration for cross-repository search.

Provides configuration management for multi-repository search operations
with sensible defaults. Configuration comes from Web UI/ServerConfig only.

Story #32: Environment variable configuration has been removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from code_indexer.server.services.config_service import ConfigService


@dataclass
class MultiSearchConfig:
    """
    Configuration for multi-repository search operations.

    Attributes:
        max_workers: Maximum number of concurrent search threads (default: 2 per resource audit)
        query_timeout_seconds: Timeout for each repository search in seconds (default: 30)
        max_repos_per_query: Maximum number of repositories allowed in a single query (default: 50)
        max_results_per_repo: Maximum number of results to return per repository (default: 100)

    Story #32: Environment variable configuration has been removed.
    All configuration MUST come from the Web UI configuration system (ServerConfig).
    """

    max_workers: int = 2
    query_timeout_seconds: int = 30
    max_repos_per_query: int = 50
    max_results_per_repo: int = 100

    def __post_init__(self):
        """Validate configuration values."""
        if self.max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if self.query_timeout_seconds <= 0:
            raise ValueError("query_timeout_seconds must be positive")
        if self.max_repos_per_query <= 0:
            raise ValueError("max_repos_per_query must be positive")
        if self.max_results_per_repo <= 0:
            raise ValueError("max_results_per_repo must be positive")

    @classmethod
    def from_config(cls, config_service: "ConfigService") -> "MultiSearchConfig":
        """
        Create configuration from ConfigService (Story #25).

        This is the preferred method for creating MultiSearchConfig in server mode.
        Settings are read from the Web UI Configuration system.

        Args:
            config_service: ConfigService instance with loaded configuration

        Returns:
            MultiSearchConfig with values from ConfigService
        """
        server_config = config_service.get_config()
        multi_search_limits = server_config.multi_search_limits_config
        assert multi_search_limits is not None  # Guaranteed by ServerConfig.__post_init__

        return cls(
            max_workers=multi_search_limits.multi_search_max_workers,
            query_timeout_seconds=multi_search_limits.multi_search_timeout_seconds,
            # These settings are not yet in MultiSearchLimitsConfig, use defaults
            max_repos_per_query=50,
            max_results_per_repo=100,
        )
