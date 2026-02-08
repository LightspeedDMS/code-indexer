"""
Unit tests for Repository Health REST endpoint.

Tests the GET /api/repositories/{repo_alias}/health endpoint that provides
HNSW index health checks via REST API.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from fastapi.testclient import TestClient
from datetime import datetime, timezone
from pathlib import Path

from code_indexer.server.app import app
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.auth.dependencies import get_current_user_hybrid
from code_indexer.services.hnsw_health_service import HealthCheckResult


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
def mock_golden_repo_manager():
    """Mock GoldenRepoManager for testing."""
    with patch(
        "code_indexer.server.routers.repository_health._get_golden_repo_manager"
    ) as mock:
        manager = Mock()
        mock.return_value = manager
        yield manager


@pytest.fixture
def mock_health_service():
    """Mock HNSWHealthService for testing."""
    with patch("code_indexer.server.routers.repository_health._health_service") as mock:
        yield mock


@pytest.fixture
def mock_filesystem():
    """Mock filesystem for index directory scanning (single collection)."""
    with patch("code_indexer.server.routers.repository_health.Path") as mock_path_class:

        def path_factory(path_str):
            """Factory to create Path-like mocks."""
            mock_path = MagicMock(spec=Path)
            mock_path.__truediv__ = lambda self, other: path_factory(
                f"{path_str}/{other}"
            )
            mock_path.__str__ = lambda self: path_str

            # Set up index base path behavior
            if path_str.endswith(".code-indexer/index"):
                mock_path.exists.return_value = True
                mock_path.is_dir.return_value = True

                # Mock voyage-code-3 collection directory
                mock_collection = MagicMock(spec=Path)
                mock_collection.is_dir.return_value = True
                mock_collection.name = "voyage-code-3"

                # Mock hnsw_index.bin file
                mock_hnsw_file = MagicMock(spec=Path)
                mock_hnsw_file.exists.return_value = True
                mock_hnsw_file.__str__ = (
                    lambda self: f"{path_str}/voyage-code-3/hnsw_index.bin"
                )
                mock_collection.__truediv__ = lambda self, name: (
                    mock_hnsw_file if name == "hnsw_index.bin" else MagicMock()
                )

                # iterdir returns the collection directory
                mock_path.iterdir.return_value = [mock_collection]

            return mock_path

        mock_path_class.side_effect = path_factory
        yield mock_path_class


@pytest.fixture
def mock_filesystem_multiple_collections():
    """Mock filesystem for index directory scanning with 3 collections."""
    with patch("code_indexer.server.routers.repository_health.Path") as mock_path_class:

        def path_factory(path_str):
            """Factory to create Path-like mocks."""
            mock_path = MagicMock(spec=Path)
            mock_path.__truediv__ = lambda self, other: path_factory(
                f"{path_str}/{other}"
            )
            mock_path.__str__ = lambda self: path_str

            # Set up index base path behavior
            if path_str.endswith(".code-indexer/index"):
                mock_path.exists.return_value = True
                mock_path.is_dir.return_value = True

                # Mock 3 collection directories
                collections = []

                # 1. voyage-code-3 (semantic) - healthy
                mock_semantic = MagicMock(spec=Path)
                mock_semantic.is_dir.return_value = True
                mock_semantic.name = "voyage-code-3"
                mock_hnsw_semantic = MagicMock(spec=Path)
                mock_hnsw_semantic.exists.return_value = True
                mock_hnsw_semantic.__str__ = (
                    lambda self: f"{path_str}/voyage-code-3/hnsw_index.bin"
                )
                mock_semantic.__truediv__ = lambda self, name: (
                    mock_hnsw_semantic if name == "hnsw_index.bin" else MagicMock()
                )
                collections.append(mock_semantic)

                # 2. temporal-voyage-code-3 (temporal) - unhealthy
                mock_temporal = MagicMock(spec=Path)
                mock_temporal.is_dir.return_value = True
                mock_temporal.name = "temporal-voyage-code-3"
                mock_hnsw_temporal = MagicMock(spec=Path)
                mock_hnsw_temporal.exists.return_value = True
                mock_hnsw_temporal.__str__ = (
                    lambda self: f"{path_str}/temporal-voyage-code-3/hnsw_index.bin"
                )
                mock_temporal.__truediv__ = lambda self, name: (
                    mock_hnsw_temporal if name == "hnsw_index.bin" else MagicMock()
                )
                collections.append(mock_temporal)

                # 3. multimodal-voyage-code-3 (multimodal) - healthy
                mock_multimodal = MagicMock(spec=Path)
                mock_multimodal.is_dir.return_value = True
                mock_multimodal.name = "multimodal-voyage-code-3"
                mock_hnsw_multimodal = MagicMock(spec=Path)
                mock_hnsw_multimodal.exists.return_value = True
                mock_hnsw_multimodal.__str__ = (
                    lambda self: f"{path_str}/multimodal-voyage-code-3/hnsw_index.bin"
                )
                mock_multimodal.__truediv__ = lambda self, name: (
                    mock_hnsw_multimodal if name == "hnsw_index.bin" else MagicMock()
                )
                collections.append(mock_multimodal)

                # iterdir returns all 3 collection directories
                mock_path.iterdir.return_value = collections

            return mock_path

        mock_path_class.side_effect = path_factory
        yield mock_path_class


def test_get_repository_health_success(
    authenticated_client, mock_golden_repo_manager, mock_health_service, mock_filesystem
):
    """Test successful health check returns 200 with RepositoryHealthResult."""
    # Setup: mock golden repo manager to return GoldenRepo object
    mock_repo = Mock()
    mock_repo.clone_path = "/path/to/repo"
    mock_golden_repo_manager.get_golden_repo.return_value = mock_repo

    # Setup: mock health service to return healthy result
    healthy_result = HealthCheckResult(
        valid=True,
        file_exists=True,
        readable=True,
        loadable=True,
        element_count=1000,
        connections_checked=5000,
        min_inbound=2,
        max_inbound=10,
        index_path="/path/to/repo/.code-indexer/index/default/index.bin",
        file_size_bytes=1024000,
        last_modified=datetime(2024, 2, 7, 12, 0, 0, tzinfo=timezone.utc),
        errors=[],
        check_duration_ms=45.5,
        from_cache=False,
    )
    mock_health_service.check_health.return_value = healthy_result

    # Execute
    response = authenticated_client.get("/api/repositories/test-repo/health")

    # Assert - now returns RepositoryHealthResult with collections array
    assert response.status_code == 200
    data = response.json()
    assert data["repo_alias"] == "test-repo"
    assert data["overall_healthy"] is True
    assert data["total_collections"] == 1
    assert data["healthy_count"] == 1
    assert data["unhealthy_count"] == 0
    assert len(data["collections"]) == 1

    # Check first collection
    collection = data["collections"][0]
    assert collection["collection_name"] == "voyage-code-3"
    assert collection["index_type"] == "semantic"
    assert collection["valid"] is True
    assert collection["file_exists"] is True
    assert collection["readable"] is True
    assert collection["loadable"] is True
    assert collection["element_count"] == 1000
    assert collection["connections_checked"] == 5000
    assert collection["min_inbound"] == 2
    assert collection["max_inbound"] == 10
    assert collection["errors"] == []
    assert collection["check_duration_ms"] == 45.5


def test_get_repository_health_unauthenticated():
    """Test unauthenticated request returns 401."""
    # Clear any dependency overrides to ensure no auth bypass
    app.dependency_overrides.clear()
    client = TestClient(app)

    # Execute: unauthenticated request
    response = client.get("/api/repositories/test-repo/health")

    # Assert
    assert response.status_code == 401


def test_get_repository_health_unknown_repo(
    authenticated_client, mock_golden_repo_manager, mock_health_service
):
    """Test unknown repository returns 404."""
    # Setup: mock golden repo manager to return None for unknown repo
    mock_golden_repo_manager.get_golden_repo.return_value = None

    # Execute
    response = authenticated_client.get("/api/repositories/unknown-repo/health")

    # Assert
    assert response.status_code == 404
    data = response.json()
    assert "not found" in data["detail"].lower()


def test_get_repository_health_force_refresh(
    authenticated_client, mock_golden_repo_manager, mock_health_service, mock_filesystem
):
    """Test force_refresh=true bypasses cache."""
    # Setup: mock golden repo manager to return GoldenRepo object
    mock_repo = Mock()
    mock_repo.clone_path = "/path/to/repo"
    mock_golden_repo_manager.get_golden_repo.return_value = mock_repo

    # Setup: mock health service
    fresh_result = HealthCheckResult(
        valid=True,
        file_exists=True,
        readable=True,
        loadable=True,
        element_count=2000,
        connections_checked=10000,
        min_inbound=3,
        max_inbound=12,
        index_path="/path/to/repo/.code-indexer/index/default/index.bin",
        file_size_bytes=2048000,
        last_modified=datetime(2024, 2, 7, 14, 0, 0, tzinfo=timezone.utc),
        errors=[],
        check_duration_ms=150.0,
        from_cache=False,
    )
    mock_health_service.check_health.return_value = fresh_result

    # Execute
    response = authenticated_client.get(
        "/api/repositories/test-repo/health?force_refresh=true"
    )

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["from_cache"] is False

    # Verify health service was called with force_refresh=True
    mock_health_service.check_health.assert_called_once()
    call_args = mock_health_service.check_health.call_args
    assert call_args[1]["force_refresh"] is True


def test_get_repository_health_unhealthy_index(
    authenticated_client, mock_golden_repo_manager, mock_health_service, mock_filesystem
):
    """Test unhealthy index returns valid response with errors."""
    # Setup: mock golden repo manager to return GoldenRepo object
    mock_repo = Mock()
    mock_repo.clone_path = "/path/to/repo"
    mock_golden_repo_manager.get_golden_repo.return_value = mock_repo

    # Setup: mock health service to return unhealthy result
    unhealthy_result = HealthCheckResult(
        valid=False,
        file_exists=True,
        readable=True,
        loadable=False,
        element_count=None,
        connections_checked=None,
        min_inbound=None,
        max_inbound=None,
        index_path="/path/to/repo/.code-indexer/index/default/index.bin",
        file_size_bytes=512,
        last_modified=datetime(2024, 2, 7, 12, 0, 0, tzinfo=timezone.utc),
        errors=["Failed to load index: corrupted file"],
        check_duration_ms=25.0,
        from_cache=False,
    )
    mock_health_service.check_health.return_value = unhealthy_result

    # Execute
    response = authenticated_client.get("/api/repositories/test-repo/health")

    # Assert - now returns RepositoryHealthResult with unhealthy collection
    assert response.status_code == 200  # Still 200, but overall_healthy=False
    data = response.json()
    assert data["overall_healthy"] is False
    assert data["total_collections"] == 1
    assert data["healthy_count"] == 0
    assert data["unhealthy_count"] == 1

    # Check unhealthy collection
    collection = data["collections"][0]
    assert collection["valid"] is False
    assert collection["file_exists"] is True
    assert collection["readable"] is True
    assert collection["loadable"] is False
    assert len(collection["errors"]) == 1
    assert "corrupted file" in collection["errors"][0]


def test_get_repository_health_cached_result(
    authenticated_client, mock_golden_repo_manager, mock_health_service, mock_filesystem
):
    """Test cached result returns with from_cache=True."""
    # Setup: mock golden repo manager to return GoldenRepo object
    mock_repo = Mock()
    mock_repo.clone_path = "/path/to/repo"
    mock_golden_repo_manager.get_golden_repo.return_value = mock_repo

    # Setup: mock health service to return cached result
    cached_result = HealthCheckResult(
        valid=True,
        file_exists=True,
        readable=True,
        loadable=True,
        element_count=1000,
        connections_checked=5000,
        min_inbound=2,
        max_inbound=10,
        index_path="/path/to/repo/.code-indexer/index/default/index.bin",
        file_size_bytes=1024000,
        last_modified=datetime(2024, 2, 7, 12, 0, 0, tzinfo=timezone.utc),
        errors=[],
        check_duration_ms=5.0,  # Fast = cached
        from_cache=True,
    )
    mock_health_service.check_health.return_value = cached_result

    # Execute
    response = authenticated_client.get("/api/repositories/test-repo/health")

    # Assert - aggregated from_cache should be True if any collection was cached
    assert response.status_code == 200
    data = response.json()
    assert data["from_cache"] is True
    assert data["collections"][0]["check_duration_ms"] == 5.0  # Fast cache hit


def test_get_repository_health_multiple_collections(
    authenticated_client,
    mock_golden_repo_manager,
    mock_health_service,
    mock_filesystem_multiple_collections,
):
    """Test health check with multiple collections aggregates results correctly."""
    # Setup: mock golden repo manager to return GoldenRepo object
    mock_repo = Mock()
    mock_repo.clone_path = "/path/to/repo"
    mock_golden_repo_manager.get_golden_repo.return_value = mock_repo

    # Setup: mock health service to return different results for each collection
    # Call 1: voyage-code-3 (semantic) - healthy
    healthy_semantic = HealthCheckResult(
        valid=True,
        file_exists=True,
        readable=True,
        loadable=True,
        element_count=1000,
        connections_checked=5000,
        min_inbound=2,
        max_inbound=10,
        index_path="/path/to/repo/.code-indexer/index/voyage-code-3/hnsw_index.bin",
        file_size_bytes=1024000,
        last_modified=datetime(2024, 2, 7, 12, 0, 0, tzinfo=timezone.utc),
        errors=[],
        check_duration_ms=45.5,
        from_cache=False,
    )

    # Call 2: temporal-voyage-code-3 (temporal) - unhealthy
    unhealthy_temporal = HealthCheckResult(
        valid=False,
        file_exists=True,
        readable=True,
        loadable=False,
        element_count=None,
        connections_checked=None,
        min_inbound=None,
        max_inbound=None,
        index_path="/path/to/repo/.code-indexer/index/temporal-voyage-code-3/hnsw_index.bin",
        file_size_bytes=512,
        last_modified=datetime(2024, 2, 7, 10, 0, 0, tzinfo=timezone.utc),
        errors=["Failed to load temporal index: corrupted file"],
        check_duration_ms=30.0,
        from_cache=False,
    )

    # Call 3: multimodal-voyage-code-3 (multimodal) - healthy
    healthy_multimodal = HealthCheckResult(
        valid=True,
        file_exists=True,
        readable=True,
        loadable=True,
        element_count=2500,
        connections_checked=12000,
        min_inbound=3,
        max_inbound=15,
        index_path="/path/to/repo/.code-indexer/index/multimodal-voyage-code-3/hnsw_index.bin",
        file_size_bytes=2048000,
        last_modified=datetime(2024, 2, 7, 13, 0, 0, tzinfo=timezone.utc),
        errors=[],
        check_duration_ms=60.0,
        from_cache=False,
    )

    # Use side_effect to return different results for each call
    mock_health_service.check_health.side_effect = [
        healthy_semantic,
        unhealthy_temporal,
        healthy_multimodal,
    ]

    # Execute
    response = authenticated_client.get("/api/repositories/test-repo/health")

    # Assert - aggregated response
    assert response.status_code == 200
    data = response.json()

    # Check aggregated summary
    assert data["repo_alias"] == "test-repo"
    assert data["overall_healthy"] is False  # Because one is unhealthy
    assert data["total_collections"] == 3
    assert data["healthy_count"] == 2
    assert data["unhealthy_count"] == 1
    assert data["from_cache"] is False  # None were cached

    # Verify we have 3 collections
    assert len(data["collections"]) == 3

    # Check collection 1: voyage-code-3 (semantic) - healthy
    semantic_coll = data["collections"][0]
    assert semantic_coll["collection_name"] == "voyage-code-3"
    assert semantic_coll["index_type"] == "semantic"
    assert semantic_coll["valid"] is True
    assert semantic_coll["file_exists"] is True
    assert semantic_coll["readable"] is True
    assert semantic_coll["loadable"] is True
    assert semantic_coll["element_count"] == 1000
    assert semantic_coll["connections_checked"] == 5000
    assert semantic_coll["min_inbound"] == 2
    assert semantic_coll["max_inbound"] == 10
    assert semantic_coll["errors"] == []
    assert semantic_coll["check_duration_ms"] == 45.5

    # Check collection 2: temporal-voyage-code-3 (temporal) - unhealthy
    temporal_coll = data["collections"][1]
    assert temporal_coll["collection_name"] == "temporal-voyage-code-3"
    assert temporal_coll["index_type"] == "temporal"
    assert temporal_coll["valid"] is False
    assert temporal_coll["file_exists"] is True
    assert temporal_coll["readable"] is True
    assert temporal_coll["loadable"] is False
    assert temporal_coll["element_count"] is None
    assert temporal_coll["connections_checked"] is None
    assert temporal_coll["min_inbound"] is None
    assert temporal_coll["max_inbound"] is None
    assert len(temporal_coll["errors"]) == 1
    assert "corrupted file" in temporal_coll["errors"][0]
    assert temporal_coll["check_duration_ms"] == 30.0

    # Check collection 3: multimodal-voyage-code-3 (multimodal) - healthy
    multimodal_coll = data["collections"][2]
    assert multimodal_coll["collection_name"] == "multimodal-voyage-code-3"
    assert multimodal_coll["index_type"] == "multimodal"
    assert multimodal_coll["valid"] is True
    assert multimodal_coll["file_exists"] is True
    assert multimodal_coll["readable"] is True
    assert multimodal_coll["loadable"] is True
    assert multimodal_coll["element_count"] == 2500
    assert multimodal_coll["connections_checked"] == 12000
    assert multimodal_coll["min_inbound"] == 3
    assert multimodal_coll["max_inbound"] == 15
    assert multimodal_coll["errors"] == []
    assert multimodal_coll["check_duration_ms"] == 60.0

    # Verify health service was called 3 times
    assert mock_health_service.check_health.call_count == 3


def test_get_repository_health_golden_repo_base_alias(
    authenticated_client, mock_golden_repo_manager, mock_health_service, mock_filesystem
):
    """Test golden repo query by base alias (without -global suffix)."""
    # Setup: mock golden repo manager to return repo for base alias
    mock_repo = Mock()
    mock_repo.clone_path = "/path/to/repo"
    mock_golden_repo_manager.get_golden_repo.return_value = mock_repo
    mock_golden_repo_manager.get_actual_repo_path.return_value = "/path/to/repo"

    # Setup: mock health service
    healthy_result = HealthCheckResult(
        valid=True,
        file_exists=True,
        readable=True,
        loadable=True,
        element_count=1000,
        connections_checked=5000,
        min_inbound=2,
        max_inbound=10,
        index_path="/path/to/repo/.code-indexer/index/voyage-code-3/hnsw_index.bin",
        file_size_bytes=1024000,
        last_modified=datetime(2024, 2, 7, 12, 0, 0, tzinfo=timezone.utc),
        errors=[],
        check_duration_ms=45.5,
        from_cache=False,
    )
    mock_health_service.check_health.return_value = healthy_result

    # Execute: Query with base alias (no -global suffix)
    response = authenticated_client.get("/api/repositories/code-indexer-python/health")

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["repo_alias"] == "code-indexer-python"
    assert data["overall_healthy"] is True

    # Verify golden repo manager was called with base alias
    mock_golden_repo_manager.get_golden_repo.assert_called_with("code-indexer-python")


def test_get_repository_health_global_repo_with_suffix(
    authenticated_client, mock_golden_repo_manager, mock_health_service, mock_filesystem
):
    """Test global repo query with -global suffix (should strip suffix and retry)."""
    # Setup: mock golden repo manager to return None for -global, then repo for base
    mock_repo = Mock()
    mock_repo.clone_path = "/path/to/repo"

    # First call with -global suffix returns None, second call without suffix returns repo
    mock_golden_repo_manager.get_golden_repo.side_effect = [None, mock_repo]
    mock_golden_repo_manager.get_actual_repo_path.return_value = "/path/to/repo"

    # Setup: mock health service
    healthy_result = HealthCheckResult(
        valid=True,
        file_exists=True,
        readable=True,
        loadable=True,
        element_count=2000,
        connections_checked=10000,
        min_inbound=3,
        max_inbound=12,
        index_path="/path/to/repo/.code-indexer/index/voyage-code-3/hnsw_index.bin",
        file_size_bytes=2048000,
        last_modified=datetime(2024, 2, 7, 14, 0, 0, tzinfo=timezone.utc),
        errors=[],
        check_duration_ms=50.0,
        from_cache=False,
    )
    mock_health_service.check_health.return_value = healthy_result

    # Execute: Query with -global suffix
    response = authenticated_client.get(
        "/api/repositories/code-indexer-python-global/health"
    )

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["repo_alias"] == "code-indexer-python-global"
    assert data["overall_healthy"] is True

    # Verify golden repo manager was called twice: first with -global, then without
    assert mock_golden_repo_manager.get_golden_repo.call_count == 2
    calls = mock_golden_repo_manager.get_golden_repo.call_args_list
    assert calls[0][0][0] == "code-indexer-python-global"
    assert calls[1][0][0] == "code-indexer-python"


@pytest.fixture
def mock_activated_repo_manager():
    """Mock ActivatedRepoManager for testing."""
    with patch(
        "code_indexer.server.routers.repository_health._get_activated_repo_manager"
    ) as mock:
        manager = Mock()
        mock.return_value = manager
        yield manager


def test_get_repository_health_user_activated_repo(
    authenticated_client,
    mock_golden_repo_manager,
    mock_activated_repo_manager,
    mock_health_service,
    mock_filesystem,
):
    """Test user-activated repo query by user alias."""
    # Setup: mock golden repo manager to return None (not a golden repo)
    mock_golden_repo_manager.get_golden_repo.return_value = None

    # Setup: mock activated repo manager to return path
    mock_activated_repo_manager.get_activated_repo_path.return_value = "/path/to/repo"

    # Patch Path.exists() to return True for activated repo path validation
    with patch("pathlib.Path.exists", return_value=True):
        # Setup: mock health service
        healthy_result = HealthCheckResult(
            valid=True,
            file_exists=True,
            readable=True,
            loadable=True,
            element_count=1500,
            connections_checked=7500,
            min_inbound=2,
            max_inbound=11,
            index_path="/path/to/repo/.code-indexer/index/voyage-code-3/hnsw_index.bin",
            file_size_bytes=1536000,
            last_modified=datetime(2024, 2, 7, 15, 0, 0, tzinfo=timezone.utc),
            errors=[],
            check_duration_ms=55.0,
            from_cache=False,
        )
        mock_health_service.check_health.return_value = healthy_result

        # Execute: Query with user alias
        response = authenticated_client.get("/api/repositories/my-custom-repo/health")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["repo_alias"] == "my-custom-repo"
        assert data["overall_healthy"] is True

        # Verify activated repo manager was called correctly
        mock_activated_repo_manager.get_activated_repo_path.assert_called_once_with(
            "testuser", "my-custom-repo"
        )


def test_get_repository_health_priority_golden_over_activated(
    authenticated_client,
    mock_golden_repo_manager,
    mock_activated_repo_manager,
    mock_health_service,
    mock_filesystem,
):
    """Test that golden repo takes priority if both golden and activated exist with same alias."""
    # Setup: mock golden repo manager to return repo (golden exists)
    mock_golden_repo = Mock()
    mock_golden_repo.clone_path = "/path/to/golden/repo"
    mock_golden_repo_manager.get_golden_repo.return_value = mock_golden_repo
    mock_golden_repo_manager.get_actual_repo_path.return_value = "/path/to/golden/repo"

    # Setup: mock activated repo (should NOT be called since golden exists)
    mock_activated_repo = Mock()
    mock_activated_repo_manager.get_activated_repo.return_value = mock_activated_repo

    # Setup: mock health service
    healthy_result = HealthCheckResult(
        valid=True,
        file_exists=True,
        readable=True,
        loadable=True,
        element_count=1000,
        connections_checked=5000,
        min_inbound=2,
        max_inbound=10,
        index_path="/path/to/golden/repo/.code-indexer/index/voyage-code-3/hnsw_index.bin",
        file_size_bytes=1024000,
        last_modified=datetime(2024, 2, 7, 12, 0, 0, tzinfo=timezone.utc),
        errors=[],
        check_duration_ms=45.5,
        from_cache=False,
    )
    mock_health_service.check_health.return_value = healthy_result

    # Execute: Query with alias that exists in both golden and activated
    response = authenticated_client.get("/api/repositories/shared-alias/health")

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["repo_alias"] == "shared-alias"

    # Verify golden repo manager was called, but activated repo manager was NOT
    mock_golden_repo_manager.get_golden_repo.assert_called_with("shared-alias")
    mock_activated_repo_manager.get_activated_repo.assert_not_called()


# ============================================================================
# Tests for GET /api/repositories/{repo_alias}/indexes endpoint (Story #161)
# ============================================================================


@pytest.fixture
def mock_index_paths():
    """Mock Path objects for index file existence checks."""
    with patch("code_indexer.server.routers.repository_health.Path") as mock_path_class:

        def path_factory(path_str):
            """Factory to create Path-like mocks."""
            mock_path = MagicMock(spec=Path)
            mock_path.__truediv__ = lambda self, other: path_factory(
                f"{path_str}/{other}"
            )
            mock_path.__str__ = lambda self: path_str

            # Semantic index: .code-indexer/index/voyage-code-3/hnsw_index.bin
            if path_str.endswith("voyage-code-3/hnsw_index.bin"):
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

            return mock_path

        mock_path_class.side_effect = path_factory
        yield mock_path_class


def test_get_repository_indexes_all_present(
    authenticated_client, mock_golden_repo_manager, mock_index_paths
):
    """Test indexes endpoint returns all indexes present for golden repo."""
    # Setup: mock golden repo manager
    mock_repo = Mock()
    mock_repo.clone_path = "/path/to/repo"
    mock_golden_repo_manager.get_golden_repo.return_value = mock_repo
    mock_golden_repo_manager.get_actual_repo_path.return_value = "/path/to/repo"

    # Execute
    response = authenticated_client.get("/api/repositories/test-repo/indexes")

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["has_semantic"] is True
    assert data["has_fts"] is True
    assert data["has_temporal"] is True
    assert data["has_scip"] is True


def test_get_repository_indexes_none_present(
    authenticated_client, mock_golden_repo_manager
):
    """Test indexes endpoint returns all false when no indexes exist."""
    # Setup: mock golden repo manager
    mock_repo = Mock()
    mock_repo.clone_path = "/path/to/repo"
    mock_golden_repo_manager.get_golden_repo.return_value = mock_repo
    mock_golden_repo_manager.get_actual_repo_path.return_value = "/path/to/repo"

    # Mock Path to return non-existent for all index paths
    with patch("code_indexer.server.routers.repository_health.Path") as mock_path_class:

        def path_factory(path_str):
            mock_path = MagicMock(spec=Path)
            mock_path.__truediv__ = lambda self, other: path_factory(
                f"{path_str}/{other}"
            )
            mock_path.exists.return_value = False
            mock_path.is_file.return_value = False
            mock_path.is_dir.return_value = False
            mock_path.glob.return_value = []
            return mock_path

        mock_path_class.side_effect = path_factory

        # Execute
        response = authenticated_client.get("/api/repositories/test-repo/indexes")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["has_semantic"] is False
        assert data["has_fts"] is False
        assert data["has_temporal"] is False
        assert data["has_scip"] is False


def test_get_repository_indexes_global_suffix_stripped(
    authenticated_client, mock_golden_repo_manager, mock_index_paths
):
    """Test indexes endpoint strips -global suffix and retries."""
    # Setup: first call with -global returns None, second without suffix returns repo
    mock_repo = Mock()
    mock_repo.clone_path = "/path/to/repo"
    mock_golden_repo_manager.get_golden_repo.side_effect = [None, mock_repo]
    mock_golden_repo_manager.get_actual_repo_path.return_value = "/path/to/repo"

    # Execute
    response = authenticated_client.get("/api/repositories/python-mock-global/indexes")

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["has_semantic"] is True
    assert data["has_fts"] is True
    assert data["has_temporal"] is True
    assert data["has_scip"] is True

    # Verify golden repo manager was called twice
    assert mock_golden_repo_manager.get_golden_repo.call_count == 2
    calls = mock_golden_repo_manager.get_golden_repo.call_args_list
    assert calls[0][0][0] == "python-mock-global"
    assert calls[1][0][0] == "python-mock"


def test_get_repository_indexes_user_activated_fallback(
    authenticated_client, mock_golden_repo_manager, mock_activated_repo_manager
):
    """Test indexes endpoint falls back to user-activated repo."""
    # Setup: golden repo not found
    mock_golden_repo_manager.get_golden_repo.return_value = None

    # Setup: activated repo found
    mock_activated_repo_manager.get_activated_repo_path.return_value = "/path/to/repo"

    # Mock Path for both validation and index checks
    with patch("code_indexer.server.routers.repository_health.Path") as mock_path_class:

        def path_factory(path_str):
            """Factory to create Path-like mocks."""
            mock_path = MagicMock(spec=Path)
            mock_path.__truediv__ = lambda self, other: path_factory(
                f"{path_str}/{other}"
            )
            mock_path.__str__ = lambda self: path_str

            # Path validation for activated repo (initial check)
            if path_str == "/path/to/repo":
                mock_path.exists.return_value = True

            # Semantic index
            elif path_str.endswith("voyage-code-3/hnsw_index.bin"):
                mock_path.exists.return_value = True
                mock_path.is_file.return_value = True
            # FTS index
            elif path_str.endswith("index/tantivy"):
                mock_path.exists.return_value = True
                mock_path.is_dir.return_value = True
            # Temporal index
            elif path_str.endswith("index/temporal"):
                mock_path.exists.return_value = True
                mock_path.is_dir.return_value = True
            elif path_str.endswith("temporal/hnsw_index.bin"):
                mock_path.exists.return_value = True
                mock_path.is_file.return_value = True
            # SCIP index
            elif path_str.endswith(".code-indexer/scip"):
                mock_path.exists.return_value = True
                mock_path.is_dir.return_value = True
                mock_scip_file = MagicMock(spec=Path)
                mock_path.glob.return_value = [mock_scip_file]
            else:
                mock_path.exists.return_value = False
                mock_path.is_file.return_value = False
                mock_path.is_dir.return_value = False
                mock_path.glob.return_value = []

            return mock_path

        mock_path_class.side_effect = path_factory

        # Execute
        response = authenticated_client.get("/api/repositories/my-repo/indexes")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["has_semantic"] is True
        assert data["has_fts"] is True
        assert data["has_temporal"] is True
        assert data["has_scip"] is True

        # Verify activated repo manager was called
        mock_activated_repo_manager.get_activated_repo_path.assert_called_once_with(
            "testuser", "my-repo"
        )


def test_get_repository_indexes_not_found(
    authenticated_client, mock_golden_repo_manager, mock_activated_repo_manager
):
    """Test indexes endpoint returns 404 when repo not found."""
    # Setup: repo not found in any strategy
    mock_golden_repo_manager.get_golden_repo.return_value = None
    mock_activated_repo_manager.get_activated_repo_path.return_value = "/nonexistent"

    # Patch Path.exists() to return False
    with patch("pathlib.Path.exists", return_value=False):
        # Execute
        response = authenticated_client.get("/api/repositories/unknown-repo/indexes")

        # Assert
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


def test_get_repository_indexes_unauthenticated():
    """Test indexes endpoint returns 401 for unauthenticated requests."""
    # Clear any dependency overrides
    app.dependency_overrides.clear()
    client = TestClient(app)

    # Execute
    response = client.get("/api/repositories/test-repo/indexes")

    # Assert
    assert response.status_code == 401


# ============================================================================
# Tests for admin checking health of other users' activated repos (Bug Fix)
# ============================================================================


def test_get_activated_repo_health_admin_with_owner_param(
    authenticated_client,
):
    """Test admin can check health of another user's activated repo by passing owner parameter."""
    # Setup: admin user is already set in authenticated_client fixture
    # Admin is "testuser" with role ADMIN

    # Patch _get_activated_repo_manager in activated_repos module
    with patch("code_indexer.server.routers.activated_repos._get_activated_repo_manager") as mock_get_manager:
        mock_activated_repo_manager = Mock()
        mock_get_manager.return_value = mock_activated_repo_manager

        # Setup: mock activated repo manager to return path for test_no_group user
        mock_activated_repo_manager.get_activated_repo_path.return_value = "/path/to/test_no_group/python-mock"

        # Patch Path in activated_repos module to mock filesystem
        with patch("code_indexer.server.routers.activated_repos.Path") as mock_path_class:
            # Mock Path constructor and instance methods
            mock_repo_path = MagicMock()
            mock_repo_path.exists.return_value = True

            # Mock index directory
            mock_index_dir = MagicMock()
            mock_index_dir.exists.return_value = True

            # Mock collection directory
            mock_collection = MagicMock()
            mock_collection.is_dir.return_value = True
            mock_collection.name = "voyage-code-3"

            # Mock hnsw file
            mock_hnsw_file = MagicMock()
            mock_hnsw_file.exists.return_value = True
            mock_hnsw_file.__str__ = lambda self: "/path/to/test_no_group/python-mock/.code-indexer/index/voyage-code-3/hnsw_index.bin"

            # Setup Path division operator to return appropriate mocks
            def path_div(self, other):
                if other == ".code-indexer":
                    result = MagicMock()
                    result.__truediv__ = lambda s, o: mock_index_dir if o == "index" else MagicMock()
                    return result
                elif other == "hnsw_index.bin":
                    return mock_hnsw_file
                return MagicMock()

            mock_repo_path.__truediv__ = path_div
            mock_index_dir.iterdir.return_value = [mock_collection]
            mock_collection.__truediv__ = lambda self, other: mock_hnsw_file if other == "hnsw_index.bin" else MagicMock()

            # Path constructor returns our mock
            mock_path_class.return_value = mock_repo_path

            # Patch HNSWHealthService
            with patch("code_indexer.server.routers.activated_repos.HNSWHealthService") as mock_health_class:
                mock_health_service = Mock()
                mock_health_class.return_value = mock_health_service

                healthy_result = HealthCheckResult(
                    valid=True,
                    file_exists=True,
                    readable=True,
                    loadable=True,
                    element_count=1500,
                    connections_checked=7500,
                    min_inbound=2,
                    max_inbound=11,
                    index_path="/path/to/test_no_group/python-mock/.code-indexer/index/voyage-code-3/hnsw_index.bin",
                    file_size_bytes=1536000,
                    last_modified=datetime(2024, 2, 7, 15, 0, 0, tzinfo=timezone.utc),
                    errors=[],
                    check_duration_ms=55.0,
                    from_cache=False,
                )
                mock_health_service.check_health.return_value = healthy_result

                # Execute: Admin queries health with owner parameter
                response = authenticated_client.get(
                    "/api/activated-repos/python-mock/health?owner=test_no_group"
                )

                # Assert
                assert response.status_code == 200
                data = response.json()
                assert data["user_alias"] == "python-mock"
                assert data["overall_healthy"] is True

                # Verify activated repo manager was called with test_no_group username
                mock_activated_repo_manager.get_activated_repo_path.assert_called_once_with(
                    "test_no_group", "python-mock"
                )


def test_get_activated_repo_health_non_admin_ignores_owner_param():
    """Test non-admin user cannot use owner parameter - should use their own username."""
    # Setup: create non-admin user
    regular_user = User(
        username="regular_user",
        password_hash="hashed_password",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    app.dependency_overrides[get_current_user_hybrid] = lambda: regular_user
    client = TestClient(app)

    try:
        # Patch _get_activated_repo_manager in activated_repos module
        with patch("code_indexer.server.routers.activated_repos._get_activated_repo_manager") as mock_get_manager:
            mock_activated_repo_manager = Mock()
            mock_get_manager.return_value = mock_activated_repo_manager

            # Setup: mock activated repo manager
            mock_activated_repo_manager.get_activated_repo_path.return_value = "/path/to/regular_user/my-repo"

            # Patch Path in activated_repos module to mock filesystem
            with patch("code_indexer.server.routers.activated_repos.Path") as mock_path_class:
                # Mock Path constructor and instance methods
                mock_repo_path = MagicMock()
                mock_repo_path.exists.return_value = True

                # Mock index directory
                mock_index_dir = MagicMock()
                mock_index_dir.exists.return_value = True

                # Mock collection directory
                mock_collection = MagicMock()
                mock_collection.is_dir.return_value = True
                mock_collection.name = "voyage-code-3"

                # Mock hnsw file
                mock_hnsw_file = MagicMock()
                mock_hnsw_file.exists.return_value = True
                mock_hnsw_file.__str__ = lambda self: "/path/to/regular_user/my-repo/.code-indexer/index/voyage-code-3/hnsw_index.bin"

                # Setup Path division operator
                def path_div(self, other):
                    if other == ".code-indexer":
                        result = MagicMock()
                        result.__truediv__ = lambda s, o: mock_index_dir if o == "index" else MagicMock()
                        return result
                    elif other == "hnsw_index.bin":
                        return mock_hnsw_file
                    return MagicMock()

                mock_repo_path.__truediv__ = path_div
                mock_index_dir.iterdir.return_value = [mock_collection]
                mock_collection.__truediv__ = lambda self, other: mock_hnsw_file if other == "hnsw_index.bin" else MagicMock()

                # Path constructor returns our mock
                mock_path_class.return_value = mock_repo_path

                # Patch HNSWHealthService
                with patch("code_indexer.server.routers.activated_repos.HNSWHealthService") as mock_health_class:
                    mock_health_service = Mock()
                    mock_health_class.return_value = mock_health_service

                    healthy_result = HealthCheckResult(
                        valid=True,
                        file_exists=True,
                        readable=True,
                        loadable=True,
                        element_count=500,
                        connections_checked=2500,
                        min_inbound=2,
                        max_inbound=8,
                        index_path="/path/to/regular_user/my-repo/.code-indexer/index/voyage-code-3/hnsw_index.bin",
                        file_size_bytes=512000,
                        last_modified=datetime(2024, 2, 7, 10, 0, 0, tzinfo=timezone.utc),
                        errors=[],
                        check_duration_ms=30.0,
                        from_cache=False,
                    )
                    mock_health_service.check_health.return_value = healthy_result

                    # Execute: Non-admin tries to query with owner parameter
                    response = client.get(
                        "/api/activated-repos/my-repo/health?owner=someone_else"
                    )

                    # Assert: Should succeed but use regular_user's username, not someone_else
                    assert response.status_code == 200

                    # Verify activated repo manager was called with regular_user, NOT someone_else
                    mock_activated_repo_manager.get_activated_repo_path.assert_called_once_with(
                        "regular_user", "my-repo"
                    )
    finally:
        app.dependency_overrides.clear()
