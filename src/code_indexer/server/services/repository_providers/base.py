"""
Repository Provider Base Class for CIDX Server.

Defines the abstract interface that all repository discovery providers
(GitLab, GitHub) must implement.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from code_indexer.server.models.auto_discovery import RepositoryDiscoveryResult


class RepositoryProviderBase(ABC):
    """Abstract base class for repository discovery providers."""

    @property
    @abstractmethod
    def platform(self) -> str:
        """Return the platform name (gitlab, github)."""
        ...

    @abstractmethod
    def is_configured(self) -> bool:
        """
        Check if the provider is properly configured.

        Returns:
            True if the provider has valid configuration (e.g., API token),
            False otherwise.
        """
        ...

    @abstractmethod
    def discover_repositories(
        self,
        cursor: Optional[str] = None,
        page_size: int = 50,
        search: Optional[str] = None,
    ) -> "RepositoryDiscoveryResult":
        """
        Discover repositories from the platform using cursor-based pagination.

        Args:
            cursor: Opaque cursor token from a previous call (None for first page)
            page_size: Target number of unindexed repositories to return
            search: Optional search string to filter repositories by name/description

        Returns:
            RepositoryDiscoveryResult with cursor-based pagination fields

        Raises:
            DiscoveryProviderError: If API call fails
        """
        ...
