"""
Tests for Smart Context-Aware Defaults for Multi-Repo Search.

When repository_alias is an array with multiple repos (2+):
- aggregation_mode defaults to 'per_repo' (not 'global')
- response_format defaults to 'grouped' (not 'flat')

Written following TDD methodology - tests first, implementation second.

Story #51: Converted to sync tests since _omni_search_code and
MultiSearchService.search() are now synchronous for FastAPI thread pool execution.
"""

import json
import pytest
from datetime import datetime
from unittest.mock import Mock, patch
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.multi.models import MultiSearchResponse, MultiSearchMetadata


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


def create_mock_response(results: dict, total_repos: int) -> MultiSearchResponse:
    """Create a mock MultiSearchResponse with given results."""
    total_results = sum(len(r) for r in results.values())
    return MultiSearchResponse(
        results=results,
        metadata=MultiSearchMetadata(
            total_results=total_results,
            total_repos_searched=total_repos,
            execution_time_ms=100,
        ),
        errors=None,
    )


def create_repo_results(count: int, base_score: float) -> list:
    """Create mock results for a repository with descending scores."""
    return [
        {
            "file_path": f"src/file{i}.py",
            "score": base_score - i * 0.01,
            "content": f"code{i}",
        }
        for i in range(count)
    ]


class TestMultiRepoSmartDefaultsAggregation:
    """Test smart defaults for aggregation_mode based on repo count."""

    def test_multi_repo_defaults_to_per_repo_aggregation(
        self, mock_user, mock_config_service, mock_wildcard_expansion
    ):
        """When repository_alias has 2+ repos, aggregation_mode defaults to 'per_repo'.

        per_repo aggregation distributes results evenly: limit / num_repos per repo.
        """
        from code_indexer.server.mcp.handlers import _omni_search_code

        limit = 10
        num_repos = 2
        expected_per_repo = limit // num_repos  # 5 results per repo

        params = {
            "query_text": "authentication",
            "repository_alias": ["repo1-global", "repo2-global"],
            "limit": limit,
            # No aggregation_mode - should default to "per_repo" for multi-repo
        }

        # repo1 has much higher scores - would dominate in global aggregation
        service_results = {
            "repo1-global": create_repo_results(10, 0.95),  # Scores: 0.95, 0.94, ...
            "repo2-global": create_repo_results(10, 0.50),  # Scores: 0.50, 0.49, ...
        }

        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_class:
            mock_service = Mock()
            mock_service.search = Mock(
                return_value=create_mock_response(service_results, num_repos)
            )
            mock_class.return_value = mock_service

            with patch(
                "code_indexer.server.mcp.handlers._apply_payload_truncation",
                side_effect=lambda r: r,
            ):
                result = _omni_search_code(params, mock_user)

        response = json.loads(result["content"][0]["text"])
        results = response["results"]

        # Count results per repo to verify per_repo distribution
        if "results_by_repo" in results:
            repo1_count = results["results_by_repo"]["repo1-global"]["count"]
            repo2_count = results["results_by_repo"]["repo2-global"]["count"]
        else:
            all_results = results.get("results", [])
            repo1_count = sum(
                1 for r in all_results if r.get("source_repo") == "repo1-global"
            )
            repo2_count = sum(
                1 for r in all_results if r.get("source_repo") == "repo2-global"
            )

        # With per_repo: evenly distributed. With global: repo1 would get all 10.
        assert (
            repo1_count == expected_per_repo
        ), f"Expected {expected_per_repo} from repo1, got {repo1_count}"
        assert (
            repo2_count == expected_per_repo
        ), f"Expected {expected_per_repo} from repo2, got {repo2_count}"

    def test_explicit_global_aggregation_overrides_smart_default(
        self, mock_user, mock_config_service, mock_wildcard_expansion
    ):
        """Explicit aggregation_mode='global' overrides per_repo smart default.

        global aggregation takes top N by score across all repos.
        """
        from code_indexer.server.mcp.handlers import _omni_search_code

        limit = 10

        params = {
            "query_text": "authentication",
            "repository_alias": ["repo1-global", "repo2-global"],
            "limit": limit,
            "aggregation_mode": "global",  # Explicit override
            "response_format": "flat",
        }

        # repo1 has much higher scores - should dominate in global aggregation
        service_results = {
            "repo1-global": create_repo_results(10, 0.95),  # Scores: 0.95, 0.94, ...
            "repo2-global": create_repo_results(10, 0.20),  # Scores: 0.20, 0.19, ...
        }

        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_class:
            mock_service = Mock()
            mock_service.search = Mock(
                return_value=create_mock_response(service_results, 2)
            )
            mock_class.return_value = mock_service

            with patch(
                "code_indexer.server.mcp.handlers._apply_payload_truncation",
                side_effect=lambda r: r,
            ):
                result = _omni_search_code(params, mock_user)

        response = json.loads(result["content"][0]["text"])
        all_results = response["results"].get("results", [])

        repo1_count = sum(
            1 for r in all_results if r.get("source_repo") == "repo1-global"
        )

        # With global: top 10 by score = all from repo1 (scores 0.95-0.86 >> 0.20-0.11)
        assert (
            repo1_count == limit
        ), f"Expected all {limit} from repo1 with global, got {repo1_count}"

    def test_three_repos_uses_per_repo_aggregation(
        self, mock_user, mock_config_service, mock_wildcard_expansion
    ):
        """Verify per_repo smart default works with 3+ repos."""
        from code_indexer.server.mcp.handlers import _omni_search_code

        limit = 12
        num_repos = 3
        expected_per_repo = limit // num_repos  # 4 results per repo

        params = {
            "query_text": "authentication",
            "repository_alias": ["repo1-global", "repo2-global", "repo3-global"],
            "limit": limit,
            # No aggregation_mode - should default to "per_repo"
        }

        service_results = {
            "repo1-global": create_repo_results(10, 0.95),
            "repo2-global": create_repo_results(10, 0.50),
            "repo3-global": create_repo_results(10, 0.30),
        }

        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_class:
            mock_service = Mock()
            mock_service.search = Mock(
                return_value=create_mock_response(service_results, num_repos)
            )
            mock_class.return_value = mock_service

            with patch(
                "code_indexer.server.mcp.handlers._apply_payload_truncation",
                side_effect=lambda r: r,
            ):
                result = _omni_search_code(params, mock_user)

        response = json.loads(result["content"][0]["text"])
        results = response["results"]

        # Should use grouped format and per_repo aggregation
        if "results_by_repo" in results:
            repo1_count = results["results_by_repo"]["repo1-global"]["count"]
            repo2_count = results["results_by_repo"]["repo2-global"]["count"]
            repo3_count = results["results_by_repo"]["repo3-global"]["count"]
        else:
            all_results = results.get("results", [])
            repo1_count = sum(
                1 for r in all_results if r.get("source_repo") == "repo1-global"
            )
            repo2_count = sum(
                1 for r in all_results if r.get("source_repo") == "repo2-global"
            )
            repo3_count = sum(
                1 for r in all_results if r.get("source_repo") == "repo3-global"
            )

        assert (
            repo1_count == expected_per_repo
        ), f"Expected {expected_per_repo} from repo1"
        assert (
            repo2_count == expected_per_repo
        ), f"Expected {expected_per_repo} from repo2"
        assert (
            repo3_count == expected_per_repo
        ), f"Expected {expected_per_repo} from repo3"


class TestMultiRepoSmartDefaultsResponseFormat:
    """Test smart defaults for response_format based on repo count."""

    def test_multi_repo_defaults_to_grouped_format(
        self, mock_user, mock_config_service, mock_wildcard_expansion
    ):
        """When repository_alias has 2+ repos, response_format defaults to 'grouped'.

        grouped format uses results_by_repo structure instead of flat results array.
        """
        from code_indexer.server.mcp.handlers import _omni_search_code

        params = {
            "query_text": "authentication",
            "repository_alias": ["repo1-global", "repo2-global"],
            "limit": 10,
            # No response_format - should default to "grouped" for multi-repo
        }

        service_results = {
            "repo1-global": [
                {"file_path": "src/auth.py", "score": 0.95, "content": "code"}
            ],
            "repo2-global": [
                {"file_path": "src/login.js", "score": 0.90, "content": "code"}
            ],
        }

        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_class:
            mock_service = Mock()
            mock_service.search = Mock(
                return_value=create_mock_response(service_results, 2)
            )
            mock_class.return_value = mock_service

            with patch(
                "code_indexer.server.mcp.handlers._apply_payload_truncation",
                side_effect=lambda r: r,
            ):
                result = _omni_search_code(params, mock_user)

        response = json.loads(result["content"][0]["text"])
        results = response["results"]

        # grouped format should have results_by_repo, not flat results array
        assert (
            "results_by_repo" in results
        ), "Multi-repo should default to grouped format"
        assert (
            "results" not in results
        ), "Grouped format should not have top-level 'results'"
        assert "repo1-global" in results["results_by_repo"]
        assert "repo2-global" in results["results_by_repo"]

    def test_single_repo_defaults_to_flat_format(
        self, mock_user, mock_config_service, mock_wildcard_expansion
    ):
        """When repository_alias is single repo, response_format defaults to 'flat'.

        flat format uses results array for backward compatibility.
        """
        from code_indexer.server.mcp.handlers import _omni_search_code

        params = {
            "query_text": "authentication",
            "repository_alias": ["repo1-global"],  # Single repo
            "limit": 10,
            # No response_format - should default to "flat" for single repo
        }

        service_results = {
            "repo1-global": [
                {"file_path": "src/auth.py", "score": 0.95, "content": "code"},
                {"file_path": "src/user.py", "score": 0.85, "content": "code"},
            ],
        }

        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_class:
            mock_service = Mock()
            mock_service.search = Mock(
                return_value=create_mock_response(service_results, 1)
            )
            mock_class.return_value = mock_service

            with patch(
                "code_indexer.server.mcp.handlers._apply_payload_truncation",
                side_effect=lambda r: r,
            ):
                result = _omni_search_code(params, mock_user)

        response = json.loads(result["content"][0]["text"])
        results = response["results"]

        # flat format should have results array, not results_by_repo
        assert "results" in results, "Single repo should default to flat format"
        assert (
            "results_by_repo" not in results
        ), "Flat format should not have results_by_repo"
        assert len(results["results"]) == 2

    def test_explicit_flat_format_overrides_smart_default(
        self, mock_user, mock_config_service, mock_wildcard_expansion
    ):
        """Explicit response_format='flat' overrides grouped smart default."""
        from code_indexer.server.mcp.handlers import _omni_search_code

        params = {
            "query_text": "authentication",
            "repository_alias": ["repo1-global", "repo2-global"],
            "limit": 10,
            "response_format": "flat",  # Explicit override
        }

        service_results = {
            "repo1-global": [
                {"file_path": "src/auth.py", "score": 0.95, "content": "code"}
            ],
            "repo2-global": [
                {"file_path": "src/login.js", "score": 0.90, "content": "code"}
            ],
        }

        with patch(
            "code_indexer.server.multi.multi_search_service.MultiSearchService"
        ) as mock_class:
            mock_service = Mock()
            mock_service.search = Mock(
                return_value=create_mock_response(service_results, 2)
            )
            mock_class.return_value = mock_service

            with patch(
                "code_indexer.server.mcp.handlers._apply_payload_truncation",
                side_effect=lambda r: r,
            ):
                result = _omni_search_code(params, mock_user)

        response = json.loads(result["content"][0]["text"])
        results = response["results"]

        # Explicit flat should produce results array
        assert "results" in results, "Explicit flat should have 'results'"
        assert (
            "results_by_repo" not in results
        ), "Explicit flat should not have results_by_repo"
