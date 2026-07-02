"""
Tests for RepositoryProviderBase ABC.

Following TDD methodology - these tests are written FIRST before implementation.
Tests define the expected behavior for the abstract base class that all
repository providers (GitLab, GitHub) must implement.
"""

import pytest
from abc import ABC
from typing import Optional


def _make_concrete_provider():
    """Return a minimal concrete RepositoryProviderBase instance for testing."""
    from code_indexer.server.services.repository_providers.base import (
        RepositoryProviderBase,
    )
    from code_indexer.server.models.auto_discovery import RepositoryDiscoveryResult

    class ConcreteProvider(RepositoryProviderBase):
        @property
        def platform(self) -> str:
            return "test"

        def is_configured(self) -> bool:
            return True

        def discover_repositories(
            self,
            cursor: Optional[str] = None,
            page_size: int = 50,
            search: Optional[str] = None,
        ) -> RepositoryDiscoveryResult:
            return RepositoryDiscoveryResult(
                repositories=[],
                page_size=page_size,
                platform="gitlab",
                has_next_page=False,
                next_cursor=None,
                partial_due_to_cap=False,
            )

    return ConcreteProvider()


class TestRepositoryProviderBase:
    """Tests for RepositoryProviderBase abstract base class."""

    def test_provider_base_is_abstract(self):
        """Test that RepositoryProviderBase is an abstract class."""
        from code_indexer.server.services.repository_providers.base import (
            RepositoryProviderBase,
        )

        assert issubclass(RepositoryProviderBase, ABC)

    def test_provider_base_cannot_be_instantiated(self):
        """Test that RepositoryProviderBase cannot be instantiated directly."""
        from code_indexer.server.services.repository_providers.base import (
            RepositoryProviderBase,
        )

        with pytest.raises(TypeError):
            RepositoryProviderBase()

    def test_provider_base_has_discover_repositories_method(self):
        """Test that RepositoryProviderBase defines discover_repositories method."""
        from code_indexer.server.services.repository_providers.base import (
            RepositoryProviderBase,
        )

        # Verify the method exists as an abstract method
        assert hasattr(RepositoryProviderBase, "discover_repositories")
        assert getattr(
            RepositoryProviderBase.discover_repositories, "__isabstractmethod__", False
        )

    def test_provider_base_has_platform_property(self):
        """Test that RepositoryProviderBase defines platform property."""
        from code_indexer.server.services.repository_providers.base import (
            RepositoryProviderBase,
        )

        # Verify the property exists as an abstract property
        assert hasattr(RepositoryProviderBase, "platform")

    def test_provider_base_has_is_configured_method(self):
        """Test that RepositoryProviderBase defines is_configured method."""
        from code_indexer.server.services.repository_providers.base import (
            RepositoryProviderBase,
        )

        assert hasattr(RepositoryProviderBase, "is_configured")
        assert getattr(
            RepositoryProviderBase.is_configured, "__isabstractmethod__", False
        )

    def test_concrete_implementation_can_be_created(self):
        """Test that a concrete implementation can be created."""
        provider = _make_concrete_provider()
        assert provider.platform == "test"

    @pytest.mark.asyncio
    async def test_concrete_implementation_methods_can_be_called(self):
        """Test that concrete implementation methods can be called."""
        from code_indexer.server.models.auto_discovery import RepositoryDiscoveryResult

        provider = _make_concrete_provider()
        assert provider.is_configured() is True
        result = provider.discover_repositories(cursor=None, page_size=50)
        assert isinstance(result, RepositoryDiscoveryResult)
        assert result.has_next_page is False
        assert result.next_cursor is None
        assert result.partial_due_to_cap is False
