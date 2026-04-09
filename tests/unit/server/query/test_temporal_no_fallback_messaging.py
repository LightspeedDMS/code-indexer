"""Tests verifying honest messaging when temporal index is unavailable.

These tests enforce Messi Rule #2 (Anti-Fallback): when a temporal query is
requested and no temporal index exists, the server MUST return empty results
and say exactly that — NOT claim it is "showing results from current code only".

Story: Fix misleading "fallback to regular search" messaging in temporal pipeline.
"""

import logging
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.code_indexer.server.query.semantic_query_manager import SemanticQueryManager
from src.code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResults,
)


@pytest.fixture
def temp_data_dir():
    """Create temporary data directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def activated_repo_manager_mock():
    """Mock activated repo manager with one repository."""
    mock = MagicMock()
    mock.list_activated_repositories.return_value = [
        {
            "user_alias": "my-repo",
            "golden_repo_alias": "test-repo",
            "current_branch": "main",
            "activated_at": "2024-01-01T00:00:00Z",
            "last_accessed": "2024-01-01T00:00:00Z",
        },
    ]

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


def _no_temporal_index_result() -> TemporalSearchResults:
    """Return a TemporalSearchResults representing 'no temporal index' state."""
    return TemporalSearchResults(
        results=[],
        query="authentication",
        filter_type="time_range",
        filter_value=None,
        warning=(
            "No temporal indexes available. "
            "Run cidx index --index-commits to create temporal indexes."
        ),
    )


def _make_config_manager_mock():
    """Return a mock ConfigManager that produces a minimal config."""
    config_mock = MagicMock()
    config_mock.codebase_dir = Path(tempfile.gettempdir())
    config_manager_mock = MagicMock()
    config_manager_mock.get_config.return_value = config_mock
    return config_manager_mock


@pytest.fixture
def patched_temporal_deps():
    """Patch all external dependencies needed to exercise the temporal query path.

    Patches (all external to SemanticQueryManager SUT):
    - ConfigManager.create_with_backtrack — avoid needing real .code-indexer on disk
    - BackendFactory.create — avoid real vector store initialisation
    - execute_temporal_query_with_fusion — return empty results with warning
    - _server_hnsw_cache — server module-level cache object
    """
    config_manager_mock = _make_config_manager_mock()
    backend_mock = MagicMock()
    backend_mock.get_vector_store_client.return_value = MagicMock()

    with (
        patch(
            "code_indexer.proxy.config_manager.ConfigManager.create_with_backtrack",
            return_value=config_manager_mock,
        ),
        patch(
            "code_indexer.backends.backend_factory.BackendFactory.create",
            return_value=backend_mock,
        ),
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch"
            ".execute_temporal_query_with_fusion",
            return_value=_no_temporal_index_result(),
        ),
        patch(
            "code_indexer.server.app._server_hnsw_cache",
            None,
        ),
    ):
        yield


class TestTemporalNoFallbackMessaging:
    """Verify honest messaging when the temporal index is unavailable."""

    def test_warning_message_does_not_claim_to_show_results(
        self, semantic_query_manager, patched_temporal_deps
    ):
        """When temporal index is missing, warning must NOT claim results are shown.

        'Showing results from current code only' is a lie: results==[].
        """
        response = semantic_query_manager.query_user_repositories(
            username="testuser",
            query_text="authentication",
            time_range="2024-01-01..2024-12-31",
            limit=10,
        )

        warning = response.get("warning", "")
        assert warning is not None, (
            "Warning must be set when temporal params used and results are empty"
        )
        assert "Showing results from current code only" not in warning, (
            f"Warning must not claim results are being shown when results==[]. Got: {warning!r}"
        )

    def test_warning_message_says_no_results_returned(
        self, semantic_query_manager, patched_temporal_deps
    ):
        """When temporal index is missing, warning must say 'No results returned'."""
        response = semantic_query_manager.query_user_repositories(
            username="testuser",
            query_text="authentication",
            time_range="2024-01-01..2024-12-31",
            limit=10,
        )

        warning = response.get("warning", "")
        assert "No results returned" in warning, (
            f"Warning must say 'No results returned' when empty. Got: {warning!r}"
        )

    def test_warning_message_mentions_build_command(
        self, semantic_query_manager, patched_temporal_deps
    ):
        """Warning must tell the user how to build the temporal index."""
        response = semantic_query_manager.query_user_repositories(
            username="testuser",
            query_text="authentication",
            time_range="2024-01-01..2024-12-31",
            limit=10,
        )

        warning = response.get("warning", "")
        assert "cidx index --index-commits" in warning, (
            f"Warning must mention 'cidx index --index-commits'. Got: {warning!r}"
        )

    def test_no_results_with_time_range_all_triggers_warning(
        self, semantic_query_manager, patched_temporal_deps
    ):
        """When time_range_all=True and results empty, warning must be set."""
        response = semantic_query_manager.query_user_repositories(
            username="testuser",
            query_text="authentication",
            time_range_all=True,
            limit=10,
        )

        assert response.get("warning") is not None, (
            "Warning must be set when time_range_all=True and results are empty"
        )
        assert len(response.get("results", [])) == 0, (
            "Results must be empty when temporal index is unavailable"
        )

    def test_execute_temporal_query_log_does_not_say_falling_back(
        self, semantic_query_manager, patched_temporal_deps, caplog
    ):
        """Log emitted when temporal index unavailable must NOT say 'falling back'.

        The actual behavior is returning empty results — the log must reflect that,
        not imply a fallback to regular search is occurring.
        """
        with caplog.at_level(logging.WARNING, logger="code_indexer"):
            semantic_query_manager.query_user_repositories(
                username="testuser",
                query_text="authentication",
                time_range="2024-01-01..2024-12-31",
                limit=10,
            )

        falling_back_messages = [
            rec.message
            for rec in caplog.records
            if "falling back" in rec.message.lower()
        ]
        assert falling_back_messages == [], (
            "Log must not say 'falling back' when temporal index is unavailable. "
            f"Found messages: {falling_back_messages}"
        )

    def test_warning_absent_when_no_temporal_params(
        self, semantic_query_manager, patched_temporal_deps
    ):
        """No warning should appear for a regular query without temporal params."""
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {
            "path": "src/auth.py",
            "score": 0.9,
            "content": "def authenticate():",
            "repository_alias": "my-repo",
        }

        # SemanticSearchService is lazily imported inside the SUT method body, so
        # patch it at its source module (consistent with all other patches in this file
        # using the code_indexer. prefix without src.).
        with patch(
            "code_indexer.server.services.search_service.SemanticSearchService",
        ) as mock_svc_cls:
            mock_svc = MagicMock()
            mock_svc_cls.return_value = mock_svc
            mock_svc.search.return_value = [mock_result]

            response = semantic_query_manager.query_user_repositories(
                username="testuser",
                query_text="authentication",
                limit=10,
            )

        assert response.get("warning") is None, (
            "Warning must not be set for regular queries without temporal params. "
            f"Got: {response.get('warning')!r}"
        )
