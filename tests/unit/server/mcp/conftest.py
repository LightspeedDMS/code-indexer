"""
Shared pytest fixtures for MCP handler unit tests.

Provides common test infrastructure for cidx-meta file access tests (Bug #336)
and other MCP handler tests that need real GroupAccessManager / AccessFilteringService.
"""

import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.access_filtering_service import AccessFilteringService
from code_indexer.server.services.group_access_manager import GroupAccessManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def extract_mcp_data(mcp_response: dict) -> dict:
    """Extract the JSON payload from an MCP content-array response."""
    content = mcp_response.get("content", [])
    if content and content[0].get("type") == "text":
        return cast(dict, json.loads(content[0]["text"]))
    return {}


def make_file_info(path: str) -> MagicMock:
    """Return a mock FileInfo object with model_dump support."""
    info = MagicMock()
    info.path = path
    info.model_dump = MagicMock(
        return_value={
            "path": path,
            "size": 512,
            "modified_at": "2025-01-01T00:00:00",
            "language": "markdown",
        }
    )
    return info


# ---------------------------------------------------------------------------
# cidx-meta file list fixture used across tests
# repo-a.md, repo-b.md, repo-c.md are repo-specific; README.md is not
# ---------------------------------------------------------------------------

CIDX_META_ALL_FILES = [
    make_file_info("repo-a.md"),
    make_file_info("repo-b.md"),
    make_file_info("repo-c.md"),
    make_file_info("README.md"),
]


def make_file_service_with_cidx_meta() -> MagicMock:
    """Return a mock file_service that returns CIDX_META_ALL_FILES."""
    mock_service = MagicMock()
    mock_service.list_files.return_value = MagicMock(files=CIDX_META_ALL_FILES)
    mock_service.list_files_by_path.return_value = MagicMock(files=CIDX_META_ALL_FILES)
    return mock_service


# ---------------------------------------------------------------------------
# Database and service fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db_path():
    """Temporary SQLite DB file for GroupAccessManager."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def group_access_manager(temp_db_path):
    """
    GroupAccessManager pre-populated with:
    - admins  : repo-a, repo-b, repo-c
    - powerusers: repo-a, repo-b
    - users   : cidx-meta only (no extra repos)

    Test users:
    - admin_user  → admins
    - power_user  → powerusers
    - regular_user → users
    """
    manager = GroupAccessManager(temp_db_path)
    admins = manager.get_group_by_name("admins")
    powerusers = manager.get_group_by_name("powerusers")
    users = manager.get_group_by_name("users")

    manager.grant_repo_access("repo-a", admins.id, "system:test")
    manager.grant_repo_access("repo-b", admins.id, "system:test")
    manager.grant_repo_access("repo-c", admins.id, "system:test")
    manager.grant_repo_access("repo-a", powerusers.id, "system:test")
    manager.grant_repo_access("repo-b", powerusers.id, "system:test")

    manager.assign_user_to_group("admin_user", admins.id, "admin")
    manager.assign_user_to_group("power_user", powerusers.id, "admin")
    manager.assign_user_to_group("regular_user", users.id, "admin")

    return manager


@pytest.fixture
def access_filtering_service(group_access_manager):
    """Real AccessFilteringService backed by a real GroupAccessManager."""
    return AccessFilteringService(group_access_manager)


# ---------------------------------------------------------------------------
# User fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_user():
    return User(
        username="admin_user",
        password_hash="hash",
        role=UserRole.ADMIN,
        created_at=datetime.now(),
    )


@pytest.fixture
def power_user():
    return User(
        username="power_user",
        password_hash="hash",
        role=UserRole.POWER_USER,
        created_at=datetime.now(),
    )


@pytest.fixture
def regular_user():
    return User(
        username="regular_user",
        password_hash="hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )
