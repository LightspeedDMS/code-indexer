"""
Unit tests for Activated Repository REST API Router.

Tests the REST endpoints for managing activated repositories.
Starting with GET /api/activated-repos/{user_alias}/indexes endpoint.
"""

import pytest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from datetime import datetime, timezone

from code_indexer.server.app import app
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.auth.dependencies import get_current_user_hybrid


@pytest.fixture
def authenticated_client():
    """Create test client with mocked authentication."""
    admin_user = User(
        username="testuser",
        password_hash="hashed_password",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    app.dependency_overrides[get_current_user_hybrid] = lambda: admin_user

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


class TestGetIndexesStatus:
    """Tests for GET /api/activated-repos/{user_alias}/indexes endpoint."""

    def test_get_indexes_status_success(
        self, authenticated_client, mock_activated_repo_manager
    ):
        """Test successful retrieval of index status."""
        # Arrange
        user_alias = "my-backend"
        repo_path = "/home/user/.cidx-server/data/activated-repos/testuser/my-backend"

        mock_activated_repo_manager.get_activated_repo_path.return_value = repo_path

        with patch("code_indexer.server.routers.activated_repos.Path") as mock_path_cls:
            # Create a function to build mock Path objects with proper __truediv__ support
            def create_mock_path(path_str):
                mock_p = Mock()
                mock_p.__str__ = lambda self: path_str
                mock_p.exists = Mock()
                mock_p.stat = Mock()

                # Support chaining with / operator
                def truediv(self, other):
                    new_path = f"{path_str}/{other}" if not path_str.endswith('/') else f"{path_str}{other}"
                    return create_mock_path(new_path)

                mock_p.__truediv__ = truediv

                # Configure specific paths
                if path_str == repo_path:
                    mock_p.exists.return_value = True
                elif ".code-indexer/index" in path_str and not any(x in path_str for x in ["voyage", "tantivy", "temporal", "scip"]):
                    mock_p.exists.return_value = True
                elif "voyage-code-3/hnsw_index.bin" in path_str:
                    mock_p.exists.return_value = True
                    mock_p.stat.return_value.st_size = 1024000
                    mock_p.stat.return_value.st_mtime = 1234567890.0
                elif path_str.endswith("/tantivy"):
                    mock_p.exists.return_value = True
                    mock_p.stat.return_value.st_size = 512000
                    mock_p.stat.return_value.st_mtime = 1234567890.0
                elif path_str.endswith("/temporal"):
                    mock_p.exists.return_value = False
                elif ".code-indexer/scip" in path_str:
                    mock_p.exists.return_value = False
                else:
                    mock_p.exists.return_value = True

                return mock_p

            # Mock Path class to return our mock path objects
            mock_path_cls.side_effect = create_mock_path

            # Act
            response = authenticated_client.get(
                f"/api/activated-repos/{user_alias}/indexes"
            )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["user_alias"] == user_alias
        assert data["repo_path"] == repo_path
        assert len(data["indexes"]) == 4

        # Check semantic index
        semantic = next(i for i in data["indexes"] if i["index_type"] == "semantic")
        assert semantic["exists"] is True
        assert semantic["healthy"] is True
        assert semantic["file_size_bytes"] == 1024000

        # Check FTS index
        fts = next(i for i in data["indexes"] if i["index_type"] == "fts")
        assert fts["exists"] is True
        assert fts["healthy"] is True

        # Check temporal index
        temporal = next(i for i in data["indexes"] if i["index_type"] == "temporal")
        assert temporal["exists"] is False

        # Check SCIP index
        scip = next(i for i in data["indexes"] if i["index_type"] == "scip")
        assert scip["exists"] is False

    def test_get_indexes_status_repo_not_found(
        self, authenticated_client, mock_activated_repo_manager
    ):
        """Test GET indexes status when repository doesn't exist."""
        # Arrange
        user_alias = "nonexistent-repo"
        mock_activated_repo_manager.get_activated_repo_path.return_value = (
            "/nonexistent/path"
        )

        with patch("code_indexer.server.routers.activated_repos.Path") as mock_path:
            mock_index_dir = Mock()
            mock_index_dir.exists.return_value = False
            mock_path.return_value = mock_index_dir

            # Act
            response = authenticated_client.get(
                f"/api/activated-repos/{user_alias}/indexes"
            )

        # Assert
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


@pytest.fixture
def mock_background_job_manager():
    """Mock BackgroundJobManager for testing."""
    with patch(
        "code_indexer.server.routers.activated_repos._get_background_job_manager"
    ) as mock:
        manager = Mock()
        mock.return_value = manager
        yield manager


class TestTriggerReindex:
    """Tests for POST /api/activated-repos/{user_alias}/reindex endpoint."""

    def test_trigger_reindex_success(
        self,
        authenticated_client,
        mock_activated_repo_manager,
        mock_background_job_manager,
    ):
        """Test successful reindex trigger."""
        # Arrange
        user_alias = "my-backend"
        job_id = "job-12345"
        repo_path = "/home/user/.cidx-server/data/activated-repos/testuser/my-backend"

        mock_activated_repo_manager.get_activated_repo_path.return_value = repo_path
        mock_background_job_manager.submit_job.return_value = job_id

        with patch("code_indexer.server.routers.activated_repos.Path") as mock_path_cls:
            # Mock repository path exists
            mock_repo_path = Mock()
            mock_repo_path.exists.return_value = True
            mock_path_cls.return_value = mock_repo_path

            # Act
            response = authenticated_client.post(
                f"/api/activated-repos/{user_alias}/reindex",
                json={"index_types": ["semantic", "fts"]},
            )

        # Assert
        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == job_id
        assert data["message"] == "Reindex job started"
        assert "semantic" in data["index_types"]
        assert "fts" in data["index_types"]

    def test_trigger_reindex_all_indexes(
        self,
        authenticated_client,
        mock_activated_repo_manager,
        mock_background_job_manager,
    ):
        """Test reindex with no specific index types (reindex all)."""
        # Arrange
        user_alias = "my-backend"
        job_id = "job-67890"
        repo_path = "/home/user/.cidx-server/data/activated-repos/testuser/my-backend"

        mock_activated_repo_manager.get_activated_repo_path.return_value = repo_path
        mock_background_job_manager.submit_job.return_value = job_id

        with patch("code_indexer.server.routers.activated_repos.Path") as mock_path_cls:
            # Mock repository path exists
            mock_repo_path = Mock()
            mock_repo_path.exists.return_value = True
            mock_path_cls.return_value = mock_repo_path

            # Act
            response = authenticated_client.post(
                f"/api/activated-repos/{user_alias}/reindex", json={}
            )

        # Assert
        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == job_id
        # When no index types specified, should reindex all existing indexes
        assert len(data["index_types"]) > 0

    def test_trigger_reindex_repo_not_found(
        self, authenticated_client, mock_activated_repo_manager
    ):
        """Test reindex when repository doesn't exist."""
        # Arrange
        user_alias = "nonexistent-repo"
        mock_activated_repo_manager.get_activated_repo_path.side_effect = Exception(
            "Repository not found"
        )

        # Act
        response = authenticated_client.post(
            f"/api/activated-repos/{user_alias}/reindex", json={}
        )

        # Assert
        assert response.status_code == 404


class TestAddIndexType:
    """Tests for POST /api/activated-repos/{user_alias}/indexes/{index_type} endpoint."""

    def test_add_index_type_success(
        self,
        authenticated_client,
        mock_activated_repo_manager,
        mock_background_job_manager,
    ):
        """Test successfully adding a specific index type."""
        # Arrange
        user_alias = "my-backend"
        index_type = "scip"
        job_id = "job-add-scip-123"
        repo_path = "/home/user/.cidx-server/data/activated-repos/testuser/my-backend"

        mock_activated_repo_manager.get_activated_repo_path.return_value = repo_path
        mock_background_job_manager.submit_job.return_value = job_id

        with patch("code_indexer.server.routers.activated_repos.Path") as mock_path_cls:
            mock_repo_path = Mock()
            mock_repo_path.exists.return_value = True
            mock_path_cls.return_value = mock_repo_path

            # Act
            response = authenticated_client.post(
                f"/api/activated-repos/{user_alias}/indexes/{index_type}"
            )

        # Assert
        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == job_id
        assert data["message"] == f"Adding {index_type} index"
        assert data["index_type"] == index_type

    def test_add_index_type_invalid_type(
        self, authenticated_client, mock_activated_repo_manager
    ):
        """Test adding an invalid index type."""
        # Arrange
        user_alias = "my-backend"
        invalid_index_type = "invalid_index"
        repo_path = "/home/user/.cidx-server/data/activated-repos/testuser/my-backend"

        mock_activated_repo_manager.get_activated_repo_path.return_value = repo_path

        with patch("code_indexer.server.routers.activated_repos.Path") as mock_path_cls:
            mock_repo_path = Mock()
            mock_repo_path.exists.return_value = True
            mock_path_cls.return_value = mock_repo_path

            # Act
            response = authenticated_client.post(
                f"/api/activated-repos/{user_alias}/indexes/{invalid_index_type}"
            )

        # Assert
        assert response.status_code == 400
        assert "invalid" in response.json()["detail"].lower()

    def test_add_index_type_repo_not_found(
        self, authenticated_client, mock_activated_repo_manager
    ):
        """Test adding index type when repository doesn't exist."""
        # Arrange
        user_alias = "nonexistent-repo"
        index_type = "scip"
        mock_activated_repo_manager.get_activated_repo_path.side_effect = Exception(
            "Repository not found"
        )

        # Act
        response = authenticated_client.post(
            f"/api/activated-repos/{user_alias}/indexes/{index_type}"
        )

        # Assert
        assert response.status_code == 404


class TestGetHealth:
    """Tests for GET /api/activated-repos/{user_alias}/health endpoint."""

    def test_get_health_success_single_collection(
        self, authenticated_client, mock_activated_repo_manager
    ):
        """Test successful health check with single collection."""
        # Arrange
        user_alias = "my-backend"
        repo_path = "/home/user/.cidx-server/data/activated-repos/testuser/my-backend"
        index_base_path = f"{repo_path}/.code-indexer/index"

        mock_activated_repo_manager.get_activated_repo_path.return_value = repo_path

        with patch("code_indexer.server.routers.activated_repos.Path") as mock_path_cls:
            # Create mock directory structure
            def create_mock_path(path_str):
                mock_p = Mock()
                mock_p.__str__ = lambda self: path_str

                # Repository path exists
                if path_str == repo_path:
                    mock_p.exists.return_value = True
                # Index base path exists
                elif path_str == index_base_path:
                    mock_p.exists.return_value = True
                    # Mock iterdir to return collection directories
                    collection_dir = create_mock_path(f"{index_base_path}/voyage-code-3")
                    mock_p.iterdir.return_value = [collection_dir]
                # Collection directory
                elif "voyage-code-3" in path_str and not path_str.endswith(".bin"):
                    mock_p.exists.return_value = True
                    mock_p.is_dir.return_value = True
                    mock_p.name = "voyage-code-3"
                # HNSW index file
                elif path_str.endswith("hnsw_index.bin"):
                    mock_p.exists.return_value = True
                    mock_p.is_dir.return_value = False
                else:
                    mock_p.exists.return_value = False
                    mock_p.is_dir.return_value = False

                # Support chaining with / operator
                def truediv(self, other):
                    new_path = f"{path_str}/{other}" if not path_str.endswith('/') else f"{path_str}{other}"
                    return create_mock_path(new_path)

                mock_p.__truediv__ = truediv
                return mock_p

            mock_path_cls.side_effect = create_mock_path

            with patch(
                "code_indexer.server.routers.activated_repos.HNSWHealthService"
            ) as mock_health_service_cls:
                from code_indexer.services.hnsw_health_service import HealthCheckResult

                mock_health_service = Mock()
                mock_health_service_cls.return_value = mock_health_service

                # Mock check_health to return HealthCheckResult object (not dict)
                mock_health_service.check_health.return_value = HealthCheckResult(
                    valid=True,
                    file_exists=True,
                    readable=True,
                    loadable=True,
                    element_count=1500,
                    connections_checked=7500,
                    min_inbound=5,
                    max_inbound=10,
                    index_path=f"{index_base_path}/voyage-code-3/hnsw_index.bin",
                    file_size_bytes=1024000,
                    errors=[],
                    check_duration_ms=45.5,
                    from_cache=False,
                )

                # Act
                response = authenticated_client.get(
                    f"/api/activated-repos/{user_alias}/health"
                )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["user_alias"] == user_alias
        assert data["status"] == "healthy"
        assert len(data["collections"]) == 1

        collection = data["collections"][0]
        assert collection["collection_name"] == "voyage-code-3"
        assert collection["index_type"] == "semantic"
        assert collection["valid"] is True
        assert collection["element_count"] == 1500
        assert collection["connections_checked"] == 7500
        assert collection["min_inbound"] == 5
        assert collection["max_inbound"] == 10
        assert collection["file_size_bytes"] == 1024000

        # Verify check_health was called with FILE path, not directory
        mock_health_service.check_health.assert_called_once()
        call_args = mock_health_service.check_health.call_args
        assert call_args[1]["index_path"].endswith("hnsw_index.bin")

    def test_get_health_success_multiple_collections(
        self, authenticated_client, mock_activated_repo_manager
    ):
        """Test successful health check with multiple collections."""
        # Arrange
        user_alias = "my-backend"
        repo_path = "/home/user/.cidx-server/data/activated-repos/testuser/my-backend"
        index_base_path = f"{repo_path}/.code-indexer/index"

        mock_activated_repo_manager.get_activated_repo_path.return_value = repo_path

        with patch("code_indexer.server.routers.activated_repos.Path") as mock_path_cls:
            # Create mock directory structure with multiple collections
            def create_mock_path(path_str):
                mock_p = Mock()
                mock_p.__str__ = lambda self: path_str

                if path_str == repo_path:
                    mock_p.exists.return_value = True
                elif path_str == index_base_path:
                    mock_p.exists.return_value = True
                    # Return two collection directories
                    collection1 = create_mock_path(f"{index_base_path}/voyage-code-3")
                    collection2 = create_mock_path(f"{index_base_path}/voyage-code-3-temporal")
                    mock_p.iterdir.return_value = [collection1, collection2]
                elif any(coll in path_str for coll in ["voyage-code-3", "temporal"]) and not path_str.endswith(".bin"):
                    mock_p.exists.return_value = True
                    mock_p.is_dir.return_value = True
                    if "temporal" in path_str:
                        mock_p.name = "voyage-code-3-temporal"
                    else:
                        mock_p.name = "voyage-code-3"
                elif path_str.endswith("hnsw_index.bin"):
                    mock_p.exists.return_value = True
                    mock_p.is_dir.return_value = False
                else:
                    mock_p.exists.return_value = False
                    mock_p.is_dir.return_value = False

                def truediv(self, other):
                    new_path = f"{path_str}/{other}" if not path_str.endswith('/') else f"{path_str}{other}"
                    return create_mock_path(new_path)

                mock_p.__truediv__ = truediv
                return mock_p

            mock_path_cls.side_effect = create_mock_path

            with patch(
                "code_indexer.server.routers.activated_repos.HNSWHealthService"
            ) as mock_health_service_cls:
                from code_indexer.services.hnsw_health_service import HealthCheckResult

                mock_health_service = Mock()
                mock_health_service_cls.return_value = mock_health_service

                # Return different results for each collection
                def mock_check_health(index_path, force_refresh=False):
                    if "temporal" in index_path:
                        return HealthCheckResult(
                            valid=False,
                            file_exists=True,
                            readable=True,
                            loadable=False,
                            element_count=None,
                            connections_checked=None,
                            min_inbound=None,
                            max_inbound=None,
                            index_path=index_path,
                            file_size_bytes=512000,
                            errors=["Failed to load index"],
                            check_duration_ms=20.0,
                            from_cache=False,
                        )
                    else:
                        return HealthCheckResult(
                            valid=True,
                            file_exists=True,
                            readable=True,
                            loadable=True,
                            element_count=1500,
                            connections_checked=7500,
                            min_inbound=5,
                            max_inbound=10,
                            index_path=index_path,
                            file_size_bytes=1024000,
                            errors=[],
                            check_duration_ms=45.5,
                            from_cache=False,
                        )

                mock_health_service.check_health.side_effect = mock_check_health

                # Act
                response = authenticated_client.get(
                    f"/api/activated-repos/{user_alias}/health"
                )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["user_alias"] == user_alias
        assert data["status"] == "unhealthy"  # One collection is unhealthy
        assert len(data["collections"]) == 2

        # Verify both collections are present
        collection_names = [c["collection_name"] for c in data["collections"]]
        assert "voyage-code-3" in collection_names
        assert "voyage-code-3-temporal" in collection_names

        # Verify check_health was called twice with FILE paths
        assert mock_health_service.check_health.call_count == 2

    def test_get_health_repo_not_found(
        self, authenticated_client, mock_activated_repo_manager
    ):
        """Test health check when repository doesn't exist."""
        # Arrange
        user_alias = "nonexistent-repo"
        mock_activated_repo_manager.get_activated_repo_path.return_value = (
            "/nonexistent/path"
        )

        with patch("code_indexer.server.routers.activated_repos.Path") as mock_path:
            mock_repo_path = Mock()
            mock_repo_path.exists.return_value = False
            mock_path.return_value = mock_repo_path

            # Act
            response = authenticated_client.get(
                f"/api/activated-repos/{user_alias}/health"
            )

        # Assert
        assert response.status_code == 404


class TestSyncRepository:
    """Tests for POST /api/activated-repos/{user_alias}/sync endpoint."""

    def test_sync_repository_success(
        self,
        authenticated_client,
        mock_activated_repo_manager,
        mock_background_job_manager,
    ):
        """Test successful repository sync."""
        # Arrange
        user_alias = "my-backend"
        job_id = "job-sync-123"
        repo_path = "/home/user/.cidx-server/data/activated-repos/testuser/my-backend"

        mock_activated_repo_manager.get_activated_repo_path.return_value = repo_path
        mock_background_job_manager.submit_job.return_value = job_id

        with patch("code_indexer.server.routers.activated_repos.Path") as mock_path_cls:
            mock_repo_path = Mock()
            mock_repo_path.exists.return_value = True
            mock_path_cls.return_value = mock_repo_path

            # Act
            response = authenticated_client.post(
                f"/api/activated-repos/{user_alias}/sync", json={"reindex": True}
            )

        # Assert
        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == job_id
        assert data["message"] == "Sync job started"
        assert data["reindex"] is True

    def test_sync_repository_no_reindex(
        self,
        authenticated_client,
        mock_activated_repo_manager,
        mock_background_job_manager,
    ):
        """Test repository sync without reindexing."""
        # Arrange
        user_alias = "my-backend"
        job_id = "job-sync-456"
        repo_path = "/home/user/.cidx-server/data/activated-repos/testuser/my-backend"

        mock_activated_repo_manager.get_activated_repo_path.return_value = repo_path
        mock_background_job_manager.submit_job.return_value = job_id

        with patch("code_indexer.server.routers.activated_repos.Path") as mock_path_cls:
            mock_repo_path = Mock()
            mock_repo_path.exists.return_value = True
            mock_path_cls.return_value = mock_repo_path

            # Act
            response = authenticated_client.post(
                f"/api/activated-repos/{user_alias}/sync", json={}
            )

        # Assert
        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == job_id
        assert data["reindex"] is False

    def test_sync_repository_not_found(
        self, authenticated_client, mock_activated_repo_manager
    ):
        """Test sync when repository doesn't exist."""
        # Arrange
        user_alias = "nonexistent-repo"
        mock_activated_repo_manager.get_activated_repo_path.side_effect = Exception(
            "Repository not found"
        )

        # Act
        response = authenticated_client.post(
            f"/api/activated-repos/{user_alias}/sync", json={}
        )

        # Assert
        assert response.status_code == 404


class TestSwitchBranch:
    """Tests for POST /api/activated-repos/{user_alias}/branch endpoint."""

    def test_switch_branch_success(
        self,
        authenticated_client,
        mock_activated_repo_manager,
        mock_background_job_manager,
    ):
        """Test successful branch switch."""
        # Arrange
        user_alias = "my-backend"
        job_id = "job-branch-123"
        repo_path = "/home/user/.cidx-server/data/activated-repos/testuser/my-backend"

        mock_activated_repo_manager.get_activated_repo_path.return_value = repo_path
        mock_background_job_manager.submit_job.return_value = job_id

        with patch("code_indexer.server.routers.activated_repos.Path") as mock_path_cls:
            mock_repo_path = Mock()
            mock_repo_path.exists.return_value = True
            mock_path_cls.return_value = mock_repo_path

            # Act
            response = authenticated_client.post(
                f"/api/activated-repos/{user_alias}/branch",
                json={"branch_name": "feature/new-feature"},
            )

        # Assert
        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == job_id
        assert data["message"] == "Branch switch job started"
        assert data["branch_name"] == "feature/new-feature"

    def test_switch_branch_missing_branch_name(
        self, authenticated_client, mock_activated_repo_manager
    ):
        """Test branch switch with missing branch name."""
        # Arrange
        user_alias = "my-backend"

        # Act
        response = authenticated_client.post(
            f"/api/activated-repos/{user_alias}/branch", json={}
        )

        # Assert
        assert response.status_code == 422  # Unprocessable Entity (Pydantic validation)

    def test_switch_branch_repo_not_found(
        self, authenticated_client, mock_activated_repo_manager
    ):
        """Test branch switch when repository doesn't exist."""
        # Arrange
        user_alias = "nonexistent-repo"
        mock_activated_repo_manager.get_activated_repo_path.side_effect = Exception(
            "Repository not found"
        )

        # Act
        response = authenticated_client.post(
            f"/api/activated-repos/{user_alias}/branch",
            json={"branch_name": "develop"},
        )

        # Assert
        assert response.status_code == 404


class TestListBranches:
    """Tests for GET /api/activated-repos/{user_alias}/branches endpoint."""

    def test_list_branches_success(
        self, authenticated_client, mock_activated_repo_manager
    ):
        """Test successful branch listing."""
        # Arrange
        user_alias = "my-backend"
        repo_path = "/home/user/.cidx-server/data/activated-repos/testuser/my-backend"

        mock_activated_repo_manager.get_activated_repo_path.return_value = repo_path

        with patch("code_indexer.server.routers.activated_repos.Path") as mock_path_cls:
            mock_repo_path = Mock()
            mock_repo_path.exists.return_value = True
            mock_path_cls.return_value = mock_repo_path

            with patch(
                "code_indexer.server.routers.activated_repos.subprocess.run"
            ) as mock_run:
                # Mock git branch output
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = "* main\n  develop\n  feature/new-feature\n"

                # Act
                response = authenticated_client.get(
                    f"/api/activated-repos/{user_alias}/branches"
                )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["user_alias"] == user_alias
        assert data["current_branch"] == "main"
        assert len(data["branches"]) == 3
        assert "main" in data["branches"]
        assert "develop" in data["branches"]
        assert "feature/new-feature" in data["branches"]

    def test_list_branches_repo_not_found(
        self, authenticated_client, mock_activated_repo_manager
    ):
        """Test list branches when repository doesn't exist."""
        # Arrange
        user_alias = "nonexistent-repo"
        mock_activated_repo_manager.get_activated_repo_path.return_value = (
            "/nonexistent/path"
        )

        with patch("code_indexer.server.routers.activated_repos.Path") as mock_path:
            mock_repo_path = Mock()
            mock_repo_path.exists.return_value = False
            mock_path.return_value = mock_repo_path

            # Act
            response = authenticated_client.get(
                f"/api/activated-repos/{user_alias}/branches"
            )

        # Assert
        assert response.status_code == 404
