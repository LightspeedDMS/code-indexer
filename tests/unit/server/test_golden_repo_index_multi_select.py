"""
Unit tests for Golden Repo Index Multi-Select API support.

Tests that POST /api/admin/golden-repos/{alias}/indexes supports:
1. Single index_type (string) - backward compatibility
2. Multiple index_types (array) - multi-select support

Story #2: Fix Add Index functionality - CRITICAL-2
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import Mock, patch


@pytest.fixture
def test_app():
    """Create a test FastAPI app with minimal setup."""
    from code_indexer.server.app import app

    return app


@pytest.fixture
def test_client(test_app):
    """Create a test client for the app."""
    return TestClient(test_app)


@pytest.fixture
def mock_golden_repo_manager(tmp_path):
    """Mock golden repo manager."""
    manager = Mock()
    manager.golden_repos_dir = str(tmp_path / "golden-repos")
    manager.golden_repos = {
        "test-repo": Mock(
            alias="test-repo",
            repo_url="https://github.com/test/repo.git",
            default_branch="main",
            clone_path=str(tmp_path / "golden-repos" / "test-repo"),
            created_at="2024-01-01T00:00:00Z",
            enable_temporal=False,
            temporal_options=None,
        )
    }
    return manager


@pytest.fixture
def mock_auth_admin():
    """Mock authentication for admin user."""
    from datetime import datetime, timezone
    from code_indexer.server.auth.user_manager import User, UserRole

    with (
        patch("code_indexer.server.auth.dependencies.jwt_manager") as mock_jwt,
        patch("code_indexer.server.auth.dependencies.user_manager") as mock_user_mgr,
    ):
        mock_jwt.validate_token.return_value = {
            "username": "admin",
            "role": "admin",
            "exp": 9999999999,
        }

        admin_user = User(
            username="admin",
            password_hash="$2b$12$test_hash",
            role=UserRole.ADMIN,
            created_at=datetime.now(timezone.utc),
        )
        mock_user_mgr.get_user.return_value = admin_user

        yield {"Authorization": "Bearer fake_admin_token"}


class TestAddIndexMultiSelectAPI:
    """Tests for multi-select support in add index API."""

    def test_single_index_type_string_backward_compatible(
        self, test_client, mock_golden_repo_manager, mock_auth_admin
    ):
        """
        Test backward compatibility: single index_type as string still works.

        JavaScript sends: { index_type: "semantic" }
        API should accept and process.
        """
        # Arrange
        mock_golden_repo_manager.add_index_to_golden_repo.return_value = "job-single-123"

        with patch("code_indexer.server.app.golden_repo_manager", mock_golden_repo_manager):
            # Act
            response = test_client.post(
                "/api/admin/golden-repos/test-repo/indexes",
                json={"index_type": "semantic"},
                headers=mock_auth_admin,
            )

        # Assert
        assert response.status_code == 202
        data = response.json()
        assert "job_id" in data

        # Verify manager was called once for single type
        mock_golden_repo_manager.add_index_to_golden_repo.assert_called_once_with(
            alias="test-repo", index_type="semantic", submitter_username="admin"
        )

    def test_multiple_index_types_array_multi_select(
        self, test_client, mock_golden_repo_manager, mock_auth_admin
    ):
        """
        Test multi-select: index_types as array creates multiple jobs.

        JavaScript sends: { index_types: ["semantic", "fts", "temporal"] }
        API should accept and create jobs for each type.
        """
        # Arrange - return different job_ids for each call
        mock_golden_repo_manager.add_index_to_golden_repo.side_effect = [
            "job-semantic-123",
            "job-fts-456",
            "job-temporal-789",
        ]

        with patch("code_indexer.server.app.golden_repo_manager", mock_golden_repo_manager):
            # Act
            response = test_client.post(
                "/api/admin/golden-repos/test-repo/indexes",
                json={"index_types": ["semantic", "fts", "temporal"]},
                headers=mock_auth_admin,
            )

        # Assert - should succeed with 202
        assert response.status_code == 202
        data = response.json()

        # Response should contain job_ids (array for multi-select)
        assert "job_ids" in data or "job_id" in data

        # Verify manager was called for each type
        assert mock_golden_repo_manager.add_index_to_golden_repo.call_count == 3

    def test_single_element_array_behaves_like_single_string(
        self, test_client, mock_golden_repo_manager, mock_auth_admin
    ):
        """
        Test that single-element array works like single string.

        JavaScript may send: { index_types: ["scip"] }
        Should behave identically to: { index_type: "scip" }
        """
        # Arrange
        mock_golden_repo_manager.add_index_to_golden_repo.return_value = "job-scip-single"

        with patch("code_indexer.server.app.golden_repo_manager", mock_golden_repo_manager):
            # Act
            response = test_client.post(
                "/api/admin/golden-repos/test-repo/indexes",
                json={"index_types": ["scip"]},
                headers=mock_auth_admin,
            )

        # Assert
        assert response.status_code == 202

        # Verify manager was called once
        mock_golden_repo_manager.add_index_to_golden_repo.assert_called_once_with(
            alias="test-repo", index_type="scip", submitter_username="admin"
        )

    def test_empty_array_returns_400(
        self, test_client, mock_golden_repo_manager, mock_auth_admin
    ):
        """
        Test that empty index_types array returns 400 Bad Request.

        JavaScript sends: { index_types: [] }
        API should reject with clear error.
        """
        with patch("code_indexer.server.app.golden_repo_manager", mock_golden_repo_manager):
            # Act
            response = test_client.post(
                "/api/admin/golden-repos/test-repo/indexes",
                json={"index_types": []},
                headers=mock_auth_admin,
            )

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        # Should explain that at least one index type is required

    def test_neither_index_type_nor_index_types_returns_400(
        self, test_client, mock_golden_repo_manager, mock_auth_admin
    ):
        """
        Test that missing both parameters returns 400.

        JavaScript sends: {}
        API should reject with clear error.
        """
        with patch("code_indexer.server.app.golden_repo_manager", mock_golden_repo_manager):
            # Act
            response = test_client.post(
                "/api/admin/golden-repos/test-repo/indexes",
                json={},
                headers=mock_auth_admin,
            )

        # Assert
        assert response.status_code in [400, 422]  # 422 for pydantic validation

    def test_invalid_type_in_array_returns_400(
        self, test_client, mock_golden_repo_manager, mock_auth_admin
    ):
        """
        Test that invalid type in array returns 400.

        JavaScript sends: { index_types: ["semantic", "invalid_type"] }
        API should reject before processing.
        """
        with patch("code_indexer.server.app.golden_repo_manager", mock_golden_repo_manager):
            # Act
            response = test_client.post(
                "/api/admin/golden-repos/test-repo/indexes",
                json={"index_types": ["semantic", "invalid_type"]},
                headers=mock_auth_admin,
            )

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "invalid_type" in data["detail"].lower() or "invalid" in data["detail"].lower()

    def test_all_four_valid_types_accepted(
        self, test_client, mock_golden_repo_manager, mock_auth_admin
    ):
        """
        Test that all four valid types are accepted in array.

        Valid types: semantic, fts, temporal, scip
        """
        # Arrange
        mock_golden_repo_manager.add_index_to_golden_repo.side_effect = [
            "job-1", "job-2", "job-3", "job-4"
        ]

        with patch("code_indexer.server.app.golden_repo_manager", mock_golden_repo_manager):
            # Act
            response = test_client.post(
                "/api/admin/golden-repos/test-repo/indexes",
                json={"index_types": ["semantic", "fts", "temporal", "scip"]},
                headers=mock_auth_admin,
            )

        # Assert
        assert response.status_code == 202
        assert mock_golden_repo_manager.add_index_to_golden_repo.call_count == 4


class TestAddIndexRequestModel:
    """Tests for AddIndexRequest Pydantic model validation."""

    def test_model_accepts_index_type_string(self):
        """Test that AddIndexRequest accepts index_type as string."""
        from code_indexer.server.app import AddIndexRequest

        request = AddIndexRequest(index_type="semantic")
        assert request.index_type == "semantic"

    def test_model_accepts_index_types_array(self):
        """Test that AddIndexRequest accepts index_types as array."""
        from code_indexer.server.app import AddIndexRequest

        request = AddIndexRequest(index_types=["semantic", "fts"])
        assert request.index_types == ["semantic", "fts"]

    def test_model_allows_either_but_not_both_empty(self):
        """Test that at least one of index_type or index_types must be provided."""
        from code_indexer.server.app import AddIndexRequest
        from pydantic import ValidationError

        # Neither provided should raise ValidationError
        with pytest.raises(ValidationError):
            AddIndexRequest()

    def test_model_prefers_index_types_over_index_type(self):
        """Test behavior when both are provided (edge case)."""
        from code_indexer.server.app import AddIndexRequest

        # If both provided, index_types should take precedence
        # This is an edge case - JavaScript shouldn't send both
        request = AddIndexRequest(index_type="semantic", index_types=["fts", "temporal"])
        # The implementation decides which to use - we just verify both are stored
        assert request.index_types == ["fts", "temporal"]
