"""
Unit tests for Activated Repository REST endpoints.

Tests the activated repository management endpoints including indexes with owner parameter.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from fastapi.testclient import TestClient
from datetime import datetime, timezone
from pathlib import Path

from code_indexer.server.app import app
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.auth.dependencies import get_current_user_hybrid


@pytest.fixture
def admin_client():
    """Create test client with admin user."""
    admin_user = User(
        username="admin",
        password_hash="hashed_password",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    app.dependency_overrides[get_current_user_hybrid] = lambda: admin_user

    yield TestClient(app)

    app.dependency_overrides.clear()


@pytest.fixture
def regular_user_client():
    """Create test client with regular user."""
    regular_user = User(
        username="regular_user",
        password_hash="hashed_password",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    app.dependency_overrides[get_current_user_hybrid] = lambda: regular_user

    yield TestClient(app)

    app.dependency_overrides.clear()


@pytest.fixture
def mock_activated_repo_manager():
    """Mock ActivatedRepoManager for testing."""
    with patch(
        "code_indexer.server.routers.activated_repos._get_activated_repo_manager"
    ) as mock:
        manager = Mock()
        mock.return_value = manager
        yield manager


@pytest.fixture
def mock_index_paths():
    """Mock Path objects for index file existence checks."""
    with patch("code_indexer.server.routers.activated_repos.Path") as mock_path_class:

        def path_factory(path_str):
            """Factory to create Path-like mocks."""
            mock_path = MagicMock(spec=Path)
            mock_path.__truediv__ = lambda self, other: path_factory(
                f"{path_str}/{other}"
            )
            mock_path.__str__ = lambda self: path_str

            # Repository path validation (root paths like /path/to/user/repo)
            if path_str.startswith("/path/to/") and not any(x in path_str for x in ["index", "tantivy", "temporal", "scip", ".code-indexer"]):
                mock_path.exists.return_value = True
            # Index directory existence
            elif path_str.endswith(".code-indexer/index"):
                mock_path.exists.return_value = True
                mock_path.is_dir.return_value = True
            # Semantic index: .code-indexer/index/voyage-code-3/hnsw_index.bin
            elif path_str.endswith("voyage-code-3/hnsw_index.bin"):
                mock_path.exists.return_value = True
                mock_path.is_file.return_value = True
            # FTS index: .code-indexer/index/tantivy/ (directory)
            elif path_str.endswith("index/tantivy"):
                mock_path.exists.return_value = True
                mock_path.is_dir.return_value = True
            # Temporal index: .code-indexer/index/temporal/ (directory with hnsw_index.bin)
            elif path_str.endswith("index/temporal"):
                mock_path.exists.return_value = True
                mock_path.is_dir.return_value = True
            elif path_str.endswith("temporal/hnsw_index.bin"):
                mock_path.exists.return_value = True
                mock_path.is_file.return_value = True
            # SCIP index: .code-indexer/scip/ (directory with .scip.db files)
            elif path_str.endswith(".code-indexer/scip"):
                mock_path.exists.return_value = True
                mock_path.is_dir.return_value = True
                # Mock glob for .scip.db files
                mock_scip_file = MagicMock(spec=Path)
                mock_path.glob.return_value = [mock_scip_file]
            else:
                mock_path.exists.return_value = False
                mock_path.is_file.return_value = False
                mock_path.is_dir.return_value = False
                mock_path.glob.return_value = []

            return mock_path

        mock_path_class.side_effect = path_factory
        yield mock_path_class


# ============================================================================
# Tests for GET /api/activated-repos/{user_alias}/indexes with owner parameter
# ============================================================================


def test_get_activated_repo_indexes_admin_with_owner_param(
    admin_client, mock_activated_repo_manager, mock_index_paths
):
    """Test admin can fetch indexes of another user's activated repo by passing owner parameter."""
    # Setup: mock activated repo manager to return path for test_no_group user
    mock_activated_repo_manager.get_activated_repo_path.return_value = (
        "/path/to/test_no_group/python-mock"
    )

    # Execute: Admin queries indexes with owner parameter
    response = admin_client.get(
        "/api/activated-repos/python-mock/indexes?owner=test_no_group"
    )

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["user_alias"] == "python-mock"
    assert "indexes" in data

    # Verify activated repo manager was called with test_no_group username
    mock_activated_repo_manager.get_activated_repo_path.assert_called_once_with(
        "test_no_group", "python-mock"
    )


def test_get_activated_repo_indexes_admin_without_owner_param(
    admin_client, mock_activated_repo_manager, mock_index_paths
):
    """Test admin fetches their own repo when owner param not provided."""
    # Setup: mock activated repo manager to return path for admin user
    mock_activated_repo_manager.get_activated_repo_path.return_value = (
        "/path/to/admin/my-repo"
    )

    # Execute: Admin queries indexes without owner parameter
    response = admin_client.get("/api/activated-repos/my-repo/indexes")

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["user_alias"] == "my-repo"

    # Verify activated repo manager was called with admin username
    mock_activated_repo_manager.get_activated_repo_path.assert_called_once_with(
        "admin", "my-repo"
    )


def test_get_activated_repo_indexes_non_admin_ignores_owner_param(
    regular_user_client, mock_activated_repo_manager, mock_index_paths
):
    """Test non-admin user cannot use owner parameter - should use their own username."""
    # Setup: mock activated repo manager
    mock_activated_repo_manager.get_activated_repo_path.return_value = (
        "/path/to/regular_user/my-repo"
    )

    # Execute: Non-admin tries to query with owner parameter
    response = regular_user_client.get(
        "/api/activated-repos/my-repo/indexes?owner=someone_else"
    )

    # Assert: Should succeed but use regular_user's username, not someone_else
    assert response.status_code == 200
    data = response.json()
    assert data["user_alias"] == "my-repo"

    # Verify activated repo manager was called with regular_user, NOT someone_else
    mock_activated_repo_manager.get_activated_repo_path.assert_called_once_with(
        "regular_user", "my-repo"
    )


def test_get_activated_repo_indexes_not_found(
    admin_client, mock_activated_repo_manager
):
    """Test indexes endpoint returns 404 when repo not found."""
    # Setup: mock activated repo manager to return path that doesn't exist
    mock_activated_repo_manager.get_activated_repo_path.return_value = (
        "/nonexistent/path"
    )

    # Patch Path to return non-existent
    with patch("code_indexer.server.routers.activated_repos.Path") as mock_path_class:
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        mock_path_class.return_value = mock_path

        # Execute
        response = admin_client.get(
            "/api/activated-repos/unknown-repo/indexes?owner=some_user"
        )

        # Assert
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


def test_get_activated_repo_indexes_unauthenticated():
    """Test indexes endpoint returns 401 for unauthenticated requests."""
    # Clear any dependency overrides
    app.dependency_overrides.clear()
    client = TestClient(app)

    # Execute
    response = client.get("/api/activated-repos/test-repo/indexes")

    # Assert
    assert response.status_code == 401


def test_get_activated_repo_indexes_all_present(
    admin_client, mock_activated_repo_manager, mock_index_paths
):
    """Test indexes endpoint returns all indexes present."""
    # Setup
    mock_activated_repo_manager.get_activated_repo_path.return_value = (
        "/path/to/admin/test-repo"
    )

    # Execute
    response = admin_client.get("/api/activated-repos/test-repo/indexes")

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["user_alias"] == "test-repo"
    assert len(data["indexes"]) == 4

    # Check all index types are present
    index_types = [idx["index_type"] for idx in data["indexes"]]
    assert "semantic" in index_types
    assert "fts" in index_types
    assert "temporal" in index_types
    assert "scip" in index_types


def test_get_activated_repo_indexes_none_present(
    admin_client, mock_activated_repo_manager
):
    """Test indexes endpoint when no index directory exists."""
    # Setup
    mock_activated_repo_manager.get_activated_repo_path.return_value = (
        "/path/to/admin/test-repo"
    )

    # Mock Path to simulate no index directory
    with patch("code_indexer.server.routers.activated_repos.Path") as mock_path_class:
        def path_factory(path_str):
            mock_path = MagicMock(spec=Path)
            mock_path.__truediv__ = lambda self, other: path_factory(
                f"{path_str}/{other}"
            )

            # Repository exists but no index directory
            if path_str == "/path/to/admin/test-repo":
                mock_path.exists.return_value = True
            elif path_str.endswith(".code-indexer/index"):
                mock_path.exists.return_value = False
            else:
                mock_path.exists.return_value = False
                mock_path.is_file.return_value = False
                mock_path.is_dir.return_value = False

            return mock_path

        mock_path_class.side_effect = path_factory

        # Execute
        response = admin_client.get("/api/activated-repos/test-repo/indexes")

        # Assert
        assert response.status_code == 404
        assert "Index directory not found" in response.json()["detail"]
