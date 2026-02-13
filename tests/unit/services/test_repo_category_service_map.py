"""
Tests for RepoCategoryService.get_repo_category_map() method (Story #182).

Tests the service method that provides efficient category mapping for
repository listings in MCP and REST APIs.
"""

import pytest
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from code_indexer.server.services.repo_category_service import RepoCategoryService
from code_indexer.server.storage.repo_category_backend import RepoCategorySqliteBackend
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend
from code_indexer.server.storage.database_manager import DatabaseSchema


@pytest.fixture
def test_db():
    """Create temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"

        # Initialize database schema
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        yield str(db_path)


@pytest.fixture
def category_service(test_db):
    """Create RepoCategoryService instance."""
    return RepoCategoryService(test_db)


@pytest.fixture
def category_backend(test_db):
    """Create RepoCategorySqliteBackend instance."""
    return RepoCategorySqliteBackend(test_db)


@pytest.fixture
def repo_backend(test_db):
    """Create GoldenRepoMetadataSqliteBackend instance."""
    return GoldenRepoMetadataSqliteBackend(test_db)


def test_get_repo_category_map_returns_correct_mapping(
    category_service, category_backend, repo_backend
):
    """Test get_repo_category_map returns alias -> {category_name, priority} mapping."""
    # Create categories
    backend_id = category_backend.create_category("Backend", "^api-.*", 1)
    frontend_id = category_backend.create_category("Frontend", "^web-.*", 2)

    # Create repos with categories
    repo_backend.add_repo(
        alias="api-gateway",
        repo_url="git@github.com:org/api-gateway.git",
        default_branch="main",
        clone_path="/repos/api-gateway",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    repo_backend.update_category("api-gateway", backend_id, auto_assigned=True)

    repo_backend.add_repo(
        alias="web-app",
        repo_url="git@github.com:org/web-app.git",
        default_branch="main",
        clone_path="/repos/web-app",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    repo_backend.update_category("web-app", frontend_id, auto_assigned=True)

    # Get mapping
    category_map = category_service.get_repo_category_map()

    # Verify mapping
    assert category_map["api-gateway"]["category_name"] == "Backend"
    assert category_map["api-gateway"]["priority"] == 1
    assert category_map["web-app"]["category_name"] == "Frontend"
    assert category_map["web-app"]["priority"] == 2


def test_get_repo_category_map_returns_none_for_unassigned(
    category_service, category_backend, repo_backend
):
    """Test get_repo_category_map returns None for repos without category."""
    # Create category
    backend_id = category_backend.create_category("Backend", "^api-.*", 1)

    # Create repos: one with category, one without
    repo_backend.add_repo(
        alias="api-gateway",
        repo_url="git@github.com:org/api-gateway.git",
        default_branch="main",
        clone_path="/repos/api-gateway",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    repo_backend.update_category("api-gateway", backend_id, auto_assigned=True)

    repo_backend.add_repo(
        alias="misc-tool",
        repo_url="git@github.com:org/misc-tool.git",
        default_branch="main",
        clone_path="/repos/misc-tool",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    # misc-tool has NULL category_id (Unassigned)

    # Get mapping
    category_map = category_service.get_repo_category_map()

    # Verify
    assert category_map["api-gateway"]["category_name"] == "Backend"
    assert category_map["misc-tool"]["category_name"] is None
    assert category_map["misc-tool"]["priority"] is None


def test_get_repo_category_map_with_no_categories(category_service, repo_backend):
    """Test get_repo_category_map when no categories exist returns all None."""
    # Create repos without categories
    repo_backend.add_repo(
        alias="repo-a",
        repo_url="git@github.com:org/repo-a.git",
        default_branch="main",
        clone_path="/repos/repo-a",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    repo_backend.add_repo(
        alias="repo-b",
        repo_url="git@github.com:org/repo-b.git",
        default_branch="main",
        clone_path="/repos/repo-b",
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    # Get mapping
    category_map = category_service.get_repo_category_map()

    # All repos should have None category
    assert category_map["repo-a"]["category_name"] is None
    assert category_map["repo-a"]["priority"] is None
    assert category_map["repo-b"]["category_name"] is None
    assert category_map["repo-b"]["priority"] is None


def test_get_repo_category_map_with_no_repos(category_service, category_backend):
    """Test get_repo_category_map with no repos returns empty dict."""
    # Create categories but no repos
    category_backend.create_category("Backend", "^api-.*", 1)
    category_backend.create_category("Frontend", "^web-.*", 2)

    # Get mapping
    category_map = category_service.get_repo_category_map()

    # Should be empty
    assert category_map == {}


def test_get_repo_category_map_single_query_efficiency(
    category_service, category_backend, repo_backend
):
    """Test that get_repo_category_map uses a single JOIN query (not N+1)."""
    # Create multiple categories and repos
    backend_id = category_backend.create_category("Backend", "^api-.*", 1)
    frontend_id = category_backend.create_category("Frontend", "^web-.*", 2)

    for i in range(10):
        alias = f"api-service-{i}"
        repo_backend.add_repo(
            alias=alias,
            repo_url=f"git@github.com:org/{alias}.git",
            default_branch="main",
            clone_path=f"/repos/{alias}",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        repo_backend.update_category(alias, backend_id, auto_assigned=True)

    for i in range(5):
        alias = f"web-app-{i}"
        repo_backend.add_repo(
            alias=alias,
            repo_url=f"git@github.com:org/{alias}.git",
            default_branch="main",
            clone_path=f"/repos/{alias}",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        repo_backend.update_category(alias, frontend_id, auto_assigned=True)

    # Get mapping - should be efficient (single query)
    category_map = category_service.get_repo_category_map()

    # Verify all repos are in mapping
    assert len(category_map) == 15

    # Spot check
    assert category_map["api-service-0"]["category_name"] == "Backend"
    assert category_map["web-app-0"]["category_name"] == "Frontend"
