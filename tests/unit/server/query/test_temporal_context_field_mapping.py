"""Unit tests for temporal_context field mapping in _execute_temporal_query.

Verifies that the mapping code in semantic_query_manager.py correctly maps
the NEW diff-based temporal_context fields (commit_hash, commit_date, etc.)
that TemporalSearchService actually produces — NOT legacy fields
(first_seen, last_seen, commit_count, commits) that no longer exist.

Bug: Lines 2020-2026 of semantic_query_manager.py used wrong field names.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.code_indexer.server.query.semantic_query_manager import (
    SemanticQueryManager,
)
from src.code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResult,
    TemporalSearchResults,
)


@pytest.fixture
def temp_data_dir():
    """Create temporary data directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_activated_repo_manager():
    """Mock activated repo manager."""
    mock = MagicMock()
    mock.list_activated_repositories.return_value = []
    return mock


@pytest.fixture
def semantic_query_manager(temp_data_dir, mock_activated_repo_manager):
    """Create semantic query manager with mocked dependencies."""
    return SemanticQueryManager(
        data_dir=temp_data_dir,
        activated_repo_manager=mock_activated_repo_manager,
    )


def _make_temporal_results_with_diff_context(repo_path: Path):
    """Create TemporalSearchResults using the actual field names produced by
    TemporalSearchService (diff-based indexing).

    temporal_context fields are exactly what temporal_search_service.py lines
    697-704 produce: commit_hash, commit_date, commit_message, author_name,
    commit_timestamp, diff_type.
    """
    temporal_result = TemporalSearchResult(
        file_path="src/auth.py",
        chunk_index=0,
        content="def authenticate_user(username, password): return True",
        score=0.91,
        metadata={
            "commit_hash": "abc123def456",
            "commit_date": "2024-01-15",
            "author_name": "Test Author",
            "author_email": "author@example.com",
            "commit_message": "feat: add authentication function",
            "diff_type": "added",
            "path": "src/auth.py",
        },
        temporal_context={
            # These are the ACTUAL fields from temporal_search_service.py lines 697-704
            "commit_hash": "abc123def456",
            "commit_date": "2024-01-15",
            "commit_message": "feat: add authentication function",
            "author_name": "Test Author",
            "commit_timestamp": 1705276800.0,
            "diff_type": "added",
        },
    )

    (repo_path / ".code-indexer").mkdir(parents=True, exist_ok=True)

    return TemporalSearchResults(
        results=[temporal_result],
        query="authentication function",
        filter_type="time_range",
        filter_value=("2024-01-01", "2024-12-31"),
        total_found=1,
    )


def _execute_query_with_mock(semantic_query_manager, repo_path, temporal_results):
    """Execute _execute_temporal_query with mocked fusion dispatch."""
    with (
        patch(
            "src.code_indexer.proxy.config_manager.ConfigManager"
        ) as MockConfigManager,
        patch(
            "src.code_indexer.backends.backend_factory.BackendFactory"
        ) as MockBackendFactory,
        patch(
            "src.code_indexer.services.temporal.temporal_fusion_dispatch.execute_temporal_query_with_fusion",
            return_value=temporal_results,
        ),
    ):
        mock_config = MagicMock()
        mock_config.embedding_provider = "voyage-ai"
        mock_config.voyage_ai.model = "voyage-code-3"
        mock_config_manager = MagicMock()
        mock_config_manager.get_config.return_value = mock_config
        MockConfigManager.create_with_backtrack.return_value = mock_config_manager

        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = MagicMock()
        MockBackendFactory.create.return_value = mock_backend

        return semantic_query_manager._execute_temporal_query(
            repo_path=repo_path,
            repository_alias="test-repo",
            query_text="authentication function",
            limit=10,
            min_score=None,
            time_range="2024-01-01..2024-12-31",
            at_commit=None,
            include_removed=False,
            show_evolution=False,
            evolution_limit=None,
        )


class TestTemporalContextFieldMapping:
    """Test that _execute_temporal_query maps the new diff-based temporal_context
    fields correctly to the QueryResult response.
    """

    def test_temporal_context_contains_commit_hash(
        self, semantic_query_manager, temp_data_dir
    ):
        """Temporal context in QueryResult must contain commit_hash from
        TemporalSearchResult.temporal_context, not map to None via wrong key.
        """
        repo_path = Path(temp_data_dir) / "test-repo"
        temporal_results = _make_temporal_results_with_diff_context(repo_path)

        results = _execute_query_with_mock(
            semantic_query_manager, repo_path, temporal_results
        )

        assert len(results) == 1
        tc = results[0].temporal_context
        assert tc is not None
        assert "commit_hash" in tc, (
            "temporal_context must contain commit_hash (new diff-based field)"
        )
        assert tc["commit_hash"] == "abc123def456", (
            f"Expected 'abc123def456' but got {tc.get('commit_hash')!r}. "
            "Mapping code is using wrong field names."
        )

    def test_temporal_context_does_not_contain_legacy_fields(
        self, semantic_query_manager, temp_data_dir
    ):
        """Temporal context in QueryResult must NOT contain legacy fields
        (first_seen, last_seen, commit_count, commits) that no longer exist
        in TemporalSearchResult.temporal_context from diff-based indexing.
        """
        repo_path = Path(temp_data_dir) / "test-repo"
        temporal_results = _make_temporal_results_with_diff_context(repo_path)

        results = _execute_query_with_mock(
            semantic_query_manager, repo_path, temporal_results
        )

        assert len(results) == 1
        tc = results[0].temporal_context
        assert tc is not None

        assert "first_seen" not in tc, (
            "temporal_context must NOT contain legacy 'first_seen' field"
        )
        assert "last_seen" not in tc, (
            "temporal_context must NOT contain legacy 'last_seen' field"
        )
        assert "commit_count" not in tc, (
            "temporal_context must NOT contain legacy 'commit_count' field"
        )
        assert "commits" not in tc, (
            "temporal_context must NOT contain legacy 'commits' field"
        )

    def test_temporal_context_commit_date_populated(
        self, semantic_query_manager, temp_data_dir
    ):
        """commit_date in temporal_context must be populated from
        TemporalSearchResult.temporal_context.commit_date (new field).
        """
        repo_path = Path(temp_data_dir) / "test-repo"
        temporal_results = _make_temporal_results_with_diff_context(repo_path)

        results = _execute_query_with_mock(
            semantic_query_manager, repo_path, temporal_results
        )

        assert len(results) == 1
        tc = results[0].temporal_context
        assert tc is not None
        assert "commit_date" in tc, (
            "temporal_context must contain commit_date (new diff-based field)"
        )
        assert tc["commit_date"] == "2024-01-15", (
            f"Expected '2024-01-15' but got {tc.get('commit_date')!r}"
        )

    def test_temporal_context_all_new_fields_present(
        self, semantic_query_manager, temp_data_dir
    ):
        """All six new diff-based temporal_context fields must appear in the
        QueryResult.temporal_context dict with correct values.
        """
        repo_path = Path(temp_data_dir) / "test-repo"
        temporal_results = _make_temporal_results_with_diff_context(repo_path)

        results = _execute_query_with_mock(
            semantic_query_manager, repo_path, temporal_results
        )

        assert len(results) == 1
        tc = results[0].temporal_context
        assert tc is not None

        expected_fields = {
            "commit_hash",
            "commit_date",
            "commit_message",
            "author_name",
            "commit_timestamp",
            "diff_type",
        }
        missing = expected_fields - set(tc.keys())
        assert not missing, (
            f"temporal_context is missing new diff-based fields: {missing}"
        )

        assert tc["commit_hash"] == "abc123def456"
        assert tc["commit_date"] == "2024-01-15"
        assert tc["commit_message"] == "feat: add authentication function"
        assert tc["author_name"] == "Test Author"
        assert tc["commit_timestamp"] == 1705276800.0
        assert tc["diff_type"] == "added"
