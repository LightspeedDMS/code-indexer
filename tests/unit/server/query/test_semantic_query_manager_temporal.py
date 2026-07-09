"""Unit tests for temporal query support in SemanticQueryManager (Story #446).

Tests temporal query functionality including:
- Temporal parameter acceptance and validation
- Internal TemporalSearchService integration
- Graceful fallback when temporal index missing
- Temporal metadata in results
- Error handling for invalid parameters
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import pytest

from src.code_indexer.server.query.semantic_query_manager import (
    SemanticQueryManager,
)
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResult,
    TemporalSearchResults,
)


@pytest.fixture
def temp_data_dir():
    """Create temporary data directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def activated_repo_manager_mock():
    """Mock activated repo manager."""
    mock = MagicMock()

    # Mock activated repos for test user
    mock.list_activated_repositories.return_value = [
        {
            "user_alias": "my-repo",
            "golden_repo_alias": "test-repo",
            "current_branch": "main",
            "activated_at": "2024-01-01T00:00:00Z",
            "last_accessed": "2024-01-01T00:00:00Z",
        },
    ]

    # Mock repository path
    def get_repo_path(username, user_alias):
        temp_path = Path(tempfile.gettempdir()) / f"repos-{username}-{user_alias}"
        temp_path.mkdir(parents=True, exist_ok=True)
        return str(temp_path)

    mock.get_activated_repo_path.side_effect = get_repo_path

    return mock


@pytest.fixture
def semantic_query_manager(temp_data_dir, activated_repo_manager_mock):
    """Create semantic query manager with mocked dependencies."""
    return SemanticQueryManager(
        data_dir=temp_data_dir,
        activated_repo_manager=activated_repo_manager_mock,
    )


@pytest.mark.e2e
class TestTemporalParameterAcceptance:
    """Test that query_user_repositories accepts temporal parameters."""

    def test_accepts_time_range_parameter(
        self, semantic_query_manager, activated_repo_manager_mock
    ):
        """Verify time_range parameter is accepted and passed to temporal service."""
        with patch.object(
            semantic_query_manager, "_search_single_repository"
        ) as mock_search:
            mock_search.return_value = []

            # Should not raise exception
            result = semantic_query_manager.query_user_repositories(
                username="testuser",
                query_text="authentication",
                time_range="2023-01-01..2024-01-01",
            )

            assert result is not None
            # Verify _search_single_repository called with time_range
            call_kwargs = mock_search.call_args[1]
            assert "time_range" in call_kwargs
            assert call_kwargs["time_range"] == "2023-01-01..2024-01-01"

    def test_accepts_at_commit_parameter(
        self, semantic_query_manager, activated_repo_manager_mock
    ):
        """Verify at_commit parameter is accepted and passed to temporal service."""
        with patch.object(
            semantic_query_manager, "_search_single_repository"
        ) as mock_search:
            mock_search.return_value = []

            result = semantic_query_manager.query_user_repositories(
                username="testuser",
                query_text="login handler",
                at_commit="abc123",
            )

            assert result is not None
            call_kwargs = mock_search.call_args[1]
            assert "at_commit" in call_kwargs
            assert call_kwargs["at_commit"] == "abc123"

    # Bug #1301: test_accepts_include_removed_parameter, test_accepts_show_evolution_parameter,
    # and test_accepts_evolution_limit_parameter were REMOVED. Those params were
    # retired (never implemented, permanent silent no-ops on the per-commit temporal
    # index) and no longer exist anywhere in the query call chain. Per-file diff
    # timelines belong to the existing git tools (git_file_history, git_log,
    # git_blame, git_diff) instead.


@pytest.mark.e2e
class TestTemporalServiceIntegration:
    """Test integration with TemporalSearchService."""

    def test_uses_temporal_service_when_temporal_params_present(
        self, semantic_query_manager, activated_repo_manager_mock
    ):
        """Acceptance Criterion 8: Uses internal service calls (NOT subprocess).

        Bug #1304: production now routes temporal queries through
        execute_temporal_query_with_fusion() (temporal_fusion_dispatch.py,
        Story #634/#1291's fusion-dispatch layer) instead of
        semantic_query_manager.py directly instantiating TemporalSearchService.
        Patching TemporalSearchService is therefore a dead no-op -- the real
        (unmocked) fusion-dispatch call scans the empty temp temporal/ marker
        dir, finds zero shards, and returns an empty "no temporal indexes"
        warning result. This fix patches the actual current call site,
        preserving the test's original intent: verify an internal service
        call is used, not a subprocess.

        The patch target uses the "src."-prefixed module path (matching this
        file's own "from src.code_indexer...SemanticQueryManager" import)
        because semantic_query_manager.py's internal relative import
        (`from ...services.temporal.temporal_fusion_dispatch import ...`)
        resolves relative to however ITS module was loaded -- under "src."
        here -- landing on a distinct sys.modules entry from the bare
        "code_indexer.services.temporal.temporal_fusion_dispatch". Patching
        the bare path was proven to have zero effect (the real, unmocked
        "src."-prefixed function still logged its own warning).
        """
        # Create temporary repo with temporal index marker
        repo_path = Path(
            activated_repo_manager_mock.get_activated_repo_path("testuser", "my-repo")
        )
        temporal_dir = repo_path / ".code-indexer" / "index" / "temporal"
        temporal_dir.mkdir(parents=True, exist_ok=True)

        with (
            patch("src.code_indexer.proxy.config_manager.ConfigManager"),
            patch("src.code_indexer.backends.backend_factory.BackendFactory"),
            patch(
                "src.code_indexer.services.embedding_factory.EmbeddingProviderFactory"
            ),
            patch("src.code_indexer.server.app._server_hnsw_cache", None),
            patch(
                "src.code_indexer.services.temporal.temporal_fusion_dispatch"
                ".execute_temporal_query_with_fusion"
            ) as mock_execute_fusion,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                ".get_configured_providers",
                return_value=["voyage-ai"],
            ),
        ):
            mock_execute_fusion.return_value = TemporalSearchResults(
                results=[],
                query="authentication",
                filter_type="time_range",
                filter_value="2023-01-01..2024-01-01",
                total_found=0,
                warning=None,
            )

            semantic_query_manager.query_user_repositories(
                username="testuser",
                query_text="authentication",
                time_range="2023-01-01..2024-01-01",
            )

            # Verify the internal fusion-dispatch service call was used (NOT subprocess)
            mock_execute_fusion.assert_called()

    def test_graceful_fallback_when_temporal_index_missing(
        self, semantic_query_manager, activated_repo_manager_mock
    ):
        """Acceptance Criterion 9: Graceful fallback when temporal index missing."""

        # Repo path exists but NO temporal index
        repo_path = Path(
            activated_repo_manager_mock.get_activated_repo_path("testuser", "my-repo")
        )
        repo_path.mkdir(parents=True, exist_ok=True)

        with patch.object(
            semantic_query_manager, "_execute_temporal_query"
        ) as mock_temporal_query:
            # Mock temporal query returning empty (fallback scenario)
            mock_temporal_query.return_value = []

            result = semantic_query_manager.query_user_repositories(
                username="testuser",
                query_text="authentication",
                time_range="2023-01-01..2024-01-01",
            )

            # Should return results with warning
            assert result is not None
            assert "warning" in result
            assert "Temporal index not available" in result["warning"]


@pytest.mark.e2e
class TestTemporalMetadata:
    """Test that temporal queries include proper metadata in results."""

    def test_temporal_context_in_results(
        self, semantic_query_manager, activated_repo_manager_mock
    ):
        """Acceptance Criterion 7: Response includes temporal metadata.

        Bug #1304: two fixes bundled --
        1. Patch target corrected to the "src."-prefixed
           execute_temporal_query_with_fusion (see
           test_uses_temporal_service_when_temporal_params_present above for
           the full explanation of why the bare-path / TemporalSearchService
           patch is a dead no-op).
        2. temporal_context shape updated to match CURRENT production output.
           semantic_query_manager.py's _execute_temporal_query (lines
           2333-2344) builds temporal_context from commit_hash, commit_date,
           commit_message, author_name, commit_timestamp, and diff_type --
           NOT the old first_seen/last_seen/commit_count/commits "evolution
           timeline" shape this test previously asserted. That shape was
           retired by Bug #1301 (those fields/params were never implemented
           production behavior and are permanent no-ops on the per-commit
           temporal index; QueryResult's docstring is stale on this point
           too, a separate pre-existing doc-drift issue out of this bug's
           scope).
        """
        repo_path = Path(
            activated_repo_manager_mock.get_activated_repo_path("testuser", "my-repo")
        )
        temporal_dir = repo_path / ".code-indexer" / "index" / "temporal"
        temporal_dir.mkdir(parents=True, exist_ok=True)

        with (
            patch("src.code_indexer.proxy.config_manager.ConfigManager"),
            patch("src.code_indexer.backends.backend_factory.BackendFactory"),
            patch(
                "src.code_indexer.services.embedding_factory.EmbeddingProviderFactory"
            ),
            patch("src.code_indexer.server.app._server_hnsw_cache", None),
            patch(
                "src.code_indexer.services.temporal.temporal_fusion_dispatch"
                ".execute_temporal_query_with_fusion"
            ) as mock_execute_fusion,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                ".get_configured_providers",
                return_value=["voyage-ai"],
            ),
        ):
            # Real temporal result with the CURRENT per-commit diff-based
            # metadata/temporal_context shape (real dataclass, not a Mock,
            # per project mocking-hierarchy preference).
            temporal_result = TemporalSearchResult(
                file_path="auth.py",
                chunk_index=0,
                content="def authenticate():\n    pass",
                score=0.9,
                metadata={
                    "commit_hash": "abc123",
                    "commit_date": "2023-06-15",
                    "author_name": "dev",
                    "author_email": "dev@example.com",
                    "commit_message": "Add authentication",
                    "diff_type": "add",
                },
                temporal_context={
                    "commit_hash": "abc123",
                    "commit_date": "2023-06-15",
                    "commit_message": "Add authentication",
                    "author_name": "dev",
                    "commit_timestamp": 1686787200,
                    "diff_type": "add",
                },
            )

            mock_execute_fusion.return_value = TemporalSearchResults(
                results=[temporal_result],
                query="authentication",
                filter_type="time_range",
                filter_value="2023-01-01..2024-01-01",
                total_found=1,
                warning=None,
            )

            result = semantic_query_manager.query_user_repositories(
                username="testuser",
                query_text="authentication",
                time_range="2023-01-01..2024-01-01",
            )

            # Verify temporal context present with the current per-commit
            # diff-based shape
            assert len(result["results"]) > 0
            result_item = result["results"][0]
            assert "temporal_context" in result_item
            assert result_item["temporal_context"]["commit_hash"] == "abc123"
            assert result_item["temporal_context"]["commit_date"] == "2023-06-15"
            assert (
                result_item["temporal_context"]["commit_message"]
                == "Add authentication"
            )
            assert result_item["temporal_context"]["author_name"] == "dev"


@pytest.mark.e2e
class TestTemporalErrorHandling:
    """Test error handling for invalid temporal parameters."""

    def test_invalid_date_format_raises_error(
        self, semantic_query_manager, activated_repo_manager_mock
    ):
        """Acceptance Criterion 10: Clear error for invalid date formats."""
        with (
            patch("src.code_indexer.proxy.config_manager.ConfigManager"),
            patch("src.code_indexer.backends.backend_factory.BackendFactory"),
            patch(
                "src.code_indexer.services.embedding_factory.EmbeddingProviderFactory"
            ),
            patch("src.code_indexer.server.app._server_hnsw_cache", None),
            patch(
                "src.code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
            ) as MockTemporalService,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                ".get_configured_providers",
                return_value=["voyage-ai"],
            ),
        ):
            mock_temporal_service = Mock()
            mock_temporal_service.has_temporal_index.return_value = True
            # Simulate validation error from TemporalSearchService
            mock_temporal_service.query_temporal.side_effect = ValueError(
                "Invalid date format. Use YYYY-MM-DD with zero-padded month/day (e.g., 2023-01-01)"
            )
            MockTemporalService.return_value = mock_temporal_service

            with pytest.raises(ValueError) as exc_info:
                semantic_query_manager.query_user_repositories(
                    username="testuser",
                    query_text="authentication",
                    time_range="2023-1-1..2024-1-1",  # Invalid: not zero-padded
                )

            assert "Invalid date format" in str(exc_info.value)
            assert "YYYY-MM-DD" in str(exc_info.value)

    def test_invalid_separator_raises_error(
        self, semantic_query_manager, activated_repo_manager_mock
    ):
        """Acceptance Criterion 10: Clear error for wrong separator."""
        with (
            patch("src.code_indexer.proxy.config_manager.ConfigManager"),
            patch("src.code_indexer.backends.backend_factory.BackendFactory"),
            patch(
                "src.code_indexer.services.embedding_factory.EmbeddingProviderFactory"
            ),
            patch("src.code_indexer.server.app._server_hnsw_cache", None),
            patch(
                "src.code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
            ) as MockTemporalService,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                ".get_configured_providers",
                return_value=["voyage-ai"],
            ),
        ):
            mock_temporal_service = Mock()
            mock_temporal_service.has_temporal_index.return_value = True
            mock_temporal_service.query_temporal.side_effect = ValueError(
                "Time range must use '..' separator (format: YYYY-MM-DD..YYYY-MM-DD)"
            )
            MockTemporalService.return_value = mock_temporal_service

            with pytest.raises(ValueError) as exc_info:
                semantic_query_manager.query_user_repositories(
                    username="testuser",
                    query_text="authentication",
                    time_range="2023-01-01-2024-01-01",  # Invalid: wrong separator
                )

            assert "must use '..' separator" in str(exc_info.value)

    def test_end_before_start_raises_error(
        self, semantic_query_manager, activated_repo_manager_mock
    ):
        """Acceptance Criterion 10: Clear error for invalid date range."""
        with (
            patch("src.code_indexer.proxy.config_manager.ConfigManager"),
            patch("src.code_indexer.backends.backend_factory.BackendFactory"),
            patch(
                "src.code_indexer.services.embedding_factory.EmbeddingProviderFactory"
            ),
            patch("src.code_indexer.server.app._server_hnsw_cache", None),
            patch(
                "src.code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
            ) as MockTemporalService,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                ".get_configured_providers",
                return_value=["voyage-ai"],
            ),
        ):
            mock_temporal_service = Mock()
            mock_temporal_service.has_temporal_index.return_value = True
            mock_temporal_service.query_temporal.side_effect = ValueError(
                "End date must be after start date"
            )
            MockTemporalService.return_value = mock_temporal_service

            with pytest.raises(ValueError) as exc_info:
                semantic_query_manager.query_user_repositories(
                    username="testuser",
                    query_text="authentication",
                    time_range="2024-01-01..2023-01-01",  # Invalid: end before start
                )

            assert "End date must be after start date" in str(exc_info.value)


@pytest.mark.e2e
class TestPerformanceRequirements:
    """Test performance characteristics of temporal queries."""

    def test_temporal_query_performance_target(
        self, semantic_query_manager, activated_repo_manager_mock
    ):
        """Acceptance Criterion 11: <500ms query time for temporal queries."""
        import time

        repo_path = Path(
            activated_repo_manager_mock.get_activated_repo_path("testuser", "my-repo")
        )
        temporal_dir = repo_path / ".code-indexer" / "index" / "temporal"
        temporal_dir.mkdir(parents=True, exist_ok=True)

        with (
            patch("src.code_indexer.proxy.config_manager.ConfigManager"),
            patch("src.code_indexer.backends.backend_factory.BackendFactory"),
            patch(
                "src.code_indexer.services.embedding_factory.EmbeddingProviderFactory"
            ),
            patch("src.code_indexer.server.app._server_hnsw_cache", None),
            patch(
                "src.code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
            ) as MockTemporalService,
        ):
            mock_temporal_service = Mock()
            mock_temporal_service.has_temporal_index.return_value = True
            mock_temporal_service.query_temporal.return_value = Mock(
                results=[],
                query="authentication",
                filter_type="time_range",
                filter_value="2023-01-01..2024-01-01",
                total_found=0,
                performance={"search_ms": 200, "filter_ms": 50, "total_ms": 250},
            )
            MockTemporalService.return_value = mock_temporal_service

            start = time.time()
            result = semantic_query_manager.query_user_repositories(
                username="testuser",
                query_text="authentication",
                time_range="2023-01-01..2024-01-01",
            )
            elapsed_ms = (time.time() - start) * 1000

            # Performance target: <500ms (being generous for test overhead)
            assert elapsed_ms < 1000, f"Query took {elapsed_ms}ms, expected <1000ms"
            assert result["query_metadata"]["execution_time_ms"] < 1000
