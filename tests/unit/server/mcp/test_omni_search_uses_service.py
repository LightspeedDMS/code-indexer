"""
Tests for Story #36: MCP Multi-Search Uses MultiSearchService.

Verifies that _omni_search_code delegates to MultiSearchService.search()
instead of having its own parallel execution implementation.

Written following TDD methodology - tests first, implementation second.

Story #51: Converted to sync tests since _omni_search_code and
MultiSearchService.search() are now synchronous for FastAPI thread pool execution.
"""

import json
import pytest
from datetime import datetime
from unittest.mock import Mock, patch
from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def mock_user():
    """Create mock user for testing."""
    return User(
        username="testuser",
        password_hash="hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


@pytest.fixture
def mock_config_service():
    """Mock ConfigService with multi_search_limits_config."""
    with patch("code_indexer.server.mcp.handlers.get_config_service") as mock:
        mock_service = Mock()
        mock_config = Mock()
        mock_limits = Mock()
        mock_limits.omni_max_workers = 4
        mock_limits.omni_per_repo_timeout_seconds = 30
        mock_limits.multi_search_max_workers = 4
        mock_limits.multi_search_timeout_seconds = 30
        mock_config.multi_search_limits_config = mock_limits
        mock_service.get_config.return_value = mock_config
        mock.return_value = mock_service
        yield mock


@pytest.fixture
def mock_wildcard_expansion():
    """Mock wildcard expansion to return patterns unchanged."""
    with patch("code_indexer.server.mcp.handlers._expand_wildcard_patterns") as mock:
        mock.side_effect = lambda patterns: patterns
        yield mock


class TestOmniSearchDelegatesToService:
    """Test that _omni_search_code delegates to MultiSearchService."""

    def test_omni_search_creates_multi_search_service(
        self, mock_user, mock_config_service, mock_wildcard_expansion
    ):
        """_omni_search_code creates MultiSearchService with correct config."""
        from code_indexer.server.mcp.handlers import _omni_search_code
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        params = {
            "query_text": "authentication",
            "repository_alias": ["repo1-global", "repo2-global"],
            "limit": 10,
        }

        # Patch at source module since import is inside function
        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_service_class:
            mock_service = Mock()
            mock_response = MultiSearchResponse(
                results={"repo1-global": [], "repo2-global": []},
                metadata=MultiSearchMetadata(
                    total_results=0,
                    total_repos_searched=2,
                    execution_time_ms=100,
                ),
                errors=None,
            )
            mock_service.search = Mock(return_value=mock_response)
            mock_service_class.return_value = mock_service

            _omni_search_code(params, mock_user)

            # Verify MultiSearchService was created with correct config
            mock_service_class.assert_called_once()
            call_args = mock_service_class.call_args
            config = call_args[0][0]
            assert config.max_workers == 4
            assert config.query_timeout_seconds == 30

    def test_omni_search_calls_service_search(
        self, mock_user, mock_config_service, mock_wildcard_expansion
    ):
        """_omni_search_code calls MultiSearchService.search() with request."""
        from code_indexer.server.mcp.handlers import _omni_search_code
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        params = {
            "query_text": "authentication",
            "repository_alias": ["repo1-global", "repo2-global"],
            "limit": 10,
        }

        # Patch at source module since import is inside function
        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_service_class:
            mock_service = Mock()
            mock_response = MultiSearchResponse(
                results={"repo1-global": [], "repo2-global": []},
                metadata=MultiSearchMetadata(
                    total_results=0,
                    total_repos_searched=2,
                    execution_time_ms=100,
                ),
                errors=None,
            )
            mock_service.search = Mock(return_value=mock_response)
            mock_service_class.return_value = mock_service

            _omni_search_code(params, mock_user)

            # Verify search was called
            mock_service.search.assert_called_once()

    def test_omni_search_no_asyncio_gather(
        self, mock_user, mock_config_service, mock_wildcard_expansion
    ):
        """_omni_search_code should NOT use asyncio.gather (delegates to service)."""
        from code_indexer.server.mcp.handlers import _omni_search_code
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        params = {
            "query_text": "authentication",
            "repository_alias": ["repo1-global", "repo2-global"],
            "limit": 10,
        }

        # Patch at source module since import is inside function
        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_service_class:
            mock_service = Mock()
            mock_response = MultiSearchResponse(
                results={"repo1-global": [], "repo2-global": []},
                metadata=MultiSearchMetadata(
                    total_results=0,
                    total_repos_searched=2,
                    execution_time_ms=100,
                ),
                errors=None,
            )
            mock_service.search = Mock(return_value=mock_response)
            mock_service_class.return_value = mock_service

            # Patch asyncio.gather to detect if it's used
            with patch("asyncio.gather") as mock_gather:
                _omni_search_code(params, mock_user)

                # asyncio.gather should NOT be called (delegation eliminates it)
                mock_gather.assert_not_called()


class TestOmniSearchParameterMapping:
    """Test parameter mapping from MCP params to MultiSearchRequest."""

    def test_maps_query_text_to_query(
        self, mock_user, mock_config_service, mock_wildcard_expansion
    ):
        """query_text param maps to MultiSearchRequest.query."""
        from code_indexer.server.mcp.handlers import _omni_search_code
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        params = {
            "query_text": "find authentication logic",
            "repository_alias": ["repo1-global"],
            "limit": 5,
        }

        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_service_class:
            mock_service = Mock()
            mock_response = MultiSearchResponse(
                results={"repo1-global": []},
                metadata=MultiSearchMetadata(
                    total_results=0, total_repos_searched=1, execution_time_ms=50
                ),
                errors=None,
            )
            mock_service.search = Mock(return_value=mock_response)
            mock_service_class.return_value = mock_service

            _omni_search_code(params, mock_user)

            # Check the request passed to search()
            call_args = mock_service.search.call_args
            request = call_args[0][0]
            assert request.query == "find authentication logic"

    def test_maps_repository_alias_to_repositories(
        self, mock_user, mock_config_service, mock_wildcard_expansion
    ):
        """repository_alias array maps to MultiSearchRequest.repositories."""
        from code_indexer.server.mcp.handlers import _omni_search_code
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        params = {
            "query_text": "test",
            "repository_alias": ["repo1-global", "repo2-global", "repo3-global"],
            "limit": 10,
        }

        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_service_class:
            mock_service = Mock()
            mock_response = MultiSearchResponse(
                results={
                    "repo1-global": [],
                    "repo2-global": [],
                    "repo3-global": [],
                },
                metadata=MultiSearchMetadata(
                    total_results=0, total_repos_searched=3, execution_time_ms=100
                ),
                errors=None,
            )
            mock_service.search = Mock(return_value=mock_response)
            mock_service_class.return_value = mock_service

            _omni_search_code(params, mock_user)

            call_args = mock_service.search.call_args
            request = call_args[0][0]
            assert request.repositories == [
                "repo1-global",
                "repo2-global",
                "repo3-global",
            ]

    def test_maps_search_mode_to_search_type(
        self, mock_user, mock_config_service, mock_wildcard_expansion
    ):
        """search_mode param maps to MultiSearchRequest.search_type."""
        from code_indexer.server.mcp.handlers import _omni_search_code
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        params = {
            "query_text": "test",
            "repository_alias": ["repo1-global"],
            "search_mode": "fts",
            "limit": 10,
        }

        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_service_class:
            mock_service = Mock()
            mock_response = MultiSearchResponse(
                results={"repo1-global": []},
                metadata=MultiSearchMetadata(
                    total_results=0, total_repos_searched=1, execution_time_ms=50
                ),
                errors=None,
            )
            mock_service.search = Mock(return_value=mock_response)
            mock_service_class.return_value = mock_service

            _omni_search_code(params, mock_user)

            call_args = mock_service.search.call_args
            request = call_args[0][0]
            assert request.search_type == "fts"

    def test_default_search_mode_is_semantic(
        self, mock_user, mock_config_service, mock_wildcard_expansion
    ):
        """Default search_mode (semantic) maps correctly."""
        from code_indexer.server.mcp.handlers import _omni_search_code
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        params = {
            "query_text": "test",
            "repository_alias": ["repo1-global"],
            "limit": 10,
            # No search_mode - should default to semantic
        }

        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_service_class:
            mock_service = Mock()
            mock_response = MultiSearchResponse(
                results={"repo1-global": []},
                metadata=MultiSearchMetadata(
                    total_results=0, total_repos_searched=1, execution_time_ms=50
                ),
                errors=None,
            )
            mock_service.search = Mock(return_value=mock_response)
            mock_service_class.return_value = mock_service

            _omni_search_code(params, mock_user)

            call_args = mock_service.search.call_args
            request = call_args[0][0]
            assert request.search_type == "semantic"


class TestOmniSearchResponseConversion:
    """Test conversion from MultiSearchResponse to MCP format."""

    def test_converts_grouped_results_to_flat_list(
        self, mock_user, mock_config_service, mock_wildcard_expansion
    ):
        """Service results grouped by repo are converted to flat list with source_repo."""
        from code_indexer.server.mcp.handlers import _omni_search_code
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        params = {
            "query_text": "test",
            "repository_alias": ["repo1-global", "repo2-global"],
            "limit": 10,
            "response_format": "flat",  # Explicit flat for testing source_repo tagging
        }

        # Service returns results grouped by repo
        service_results = {
            "repo1-global": [
                {"file_path": "src/auth.py", "score": 0.95, "content": "auth code"},
                {"file_path": "src/user.py", "score": 0.85, "content": "user code"},
            ],
            "repo2-global": [
                {"file_path": "src/login.js", "score": 0.90, "content": "login code"},
            ],
        }

        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_service_class:
            mock_service = Mock()
            mock_response = MultiSearchResponse(
                results=service_results,
                metadata=MultiSearchMetadata(
                    total_results=3, total_repos_searched=2, execution_time_ms=100
                ),
                errors=None,
            )
            mock_service.search = Mock(return_value=mock_response)
            mock_service_class.return_value = mock_service

            # Mock truncation to pass through
            with patch(
                "code_indexer.server.mcp.handlers._apply_payload_truncation",
                side_effect=lambda results: results,
            ):
                result = _omni_search_code(params, mock_user)

            # Parse response
            response_data = json.loads(result["content"][0]["text"])
            assert response_data["success"] is True

            results = response_data["results"]["results"]
            # All results should have source_repo tag
            for r in results:
                assert "source_repo" in r

            # Verify source_repo is set correctly
            repo1_results = [r for r in results if r["source_repo"] == "repo1-global"]
            repo2_results = [r for r in results if r["source_repo"] == "repo2-global"]
            assert len(repo1_results) == 2
            assert len(repo2_results) == 1

    def test_errors_passed_through_from_service(
        self, mock_user, mock_config_service, mock_wildcard_expansion
    ):
        """Errors from service are passed through to MCP response."""
        from code_indexer.server.mcp.handlers import _omni_search_code
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        params = {
            "query_text": "test",
            "repository_alias": ["repo1-global", "bad-repo"],
            "limit": 10,
        }

        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_service_class:
            mock_service = Mock()
            mock_response = MultiSearchResponse(
                results={"repo1-global": []},
                metadata=MultiSearchMetadata(
                    total_results=0, total_repos_searched=1, execution_time_ms=100
                ),
                errors={"bad-repo": "Repository not found"},
            )
            mock_service.search = Mock(return_value=mock_response)
            mock_service_class.return_value = mock_service

            result = _omni_search_code(params, mock_user)

            response_data = json.loads(result["content"][0]["text"])
            assert "bad-repo" in response_data["results"]["errors"]
            assert "not found" in response_data["results"]["errors"]["bad-repo"].lower()

    def test_total_repos_searched_from_metadata(
        self, mock_user, mock_config_service, mock_wildcard_expansion
    ):
        """total_repos_searched comes from service metadata."""
        from code_indexer.server.mcp.handlers import _omni_search_code
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        params = {
            "query_text": "test",
            "repository_alias": ["repo1-global", "repo2-global", "repo3-global"],
            "limit": 10,
        }

        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_service_class:
            mock_service = Mock()
            mock_response = MultiSearchResponse(
                results={"repo1-global": [], "repo2-global": []},
                metadata=MultiSearchMetadata(
                    total_results=0,
                    total_repos_searched=2,  # Only 2 succeeded
                    execution_time_ms=100,
                ),
                errors={"repo3-global": "Not found"},
            )
            mock_service.search = Mock(return_value=mock_response)
            mock_service_class.return_value = mock_service

            result = _omni_search_code(params, mock_user)

            response_data = json.loads(result["content"][0]["text"])
            assert response_data["results"]["total_repos_searched"] == 2
