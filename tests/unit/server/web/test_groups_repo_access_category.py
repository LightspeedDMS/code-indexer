"""
Unit tests for Story #686: Category enrichment in groups_repo_access_partial handler.

Tests that:
- Handler enriches golden_repos with category_id, category_name, category_priority
- category_name defaults to "Unassigned" for uncategorized repos
- Graceful degradation when get_repo_category_map() raises inside the try block
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.services.group_access_manager import GroupAccessManager
import tempfile
from pathlib import Path

# Domain constants
UNASSIGNED_PRIORITY = 999999
BACKEND_CATEGORY_ID = 5
BACKEND_CATEGORY_PRIORITY = 10
ENDPOINT = "/admin/partials/groups-repo-access"


@pytest.fixture
def temp_db_path():
    """Create a temporary database file for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def group_manager(temp_db_path):
    """Create a GroupAccessManager instance."""
    return GroupAccessManager(temp_db_path)


@pytest.fixture
def test_client(group_manager):
    """Create a test client with mocked session, CSRF, and golden repo manager."""
    from code_indexer.server.web.routes import web_router

    app = FastAPI()
    app.include_router(web_router, prefix="/admin")
    app.state.group_manager = group_manager

    mock_session = MagicMock()
    mock_session.username = "admin_user"
    mock_session.role = "admin"

    with patch("code_indexer.server.web.routes._require_admin_session") as mock_auth:
        mock_auth.return_value = mock_session
        with patch("code_indexer.server.web.routes._get_group_manager") as mock_gm:
            mock_gm.return_value = group_manager
            with patch(
                "code_indexer.server.web.routes.get_csrf_token_from_cookie"
            ) as mock_csrf:
                mock_csrf.return_value = "test-csrf-token"
                yield TestClient(app)


def _fetch_partial(test_client, repos, repo_map=None, map_error=None):
    """
    Patch golden repo manager and category service, then GET the partial.

    Args:
        test_client: FastAPI TestClient
        repos: list of dicts with 'alias' key for golden repos
        repo_map: dict alias -> {category_id, category_name, priority}; None = empty map
        map_error: if set, get_repo_category_map() raises this exception
                   (simulates the failure inside the handler's try block)

    Returns:
        Response object
    """
    mock_grm = MagicMock()
    mock_grm.list_golden_repos.return_value = repos

    mock_svc = MagicMock()
    if map_error is not None:
        mock_svc.get_repo_category_map.side_effect = map_error
    else:
        mock_svc.get_repo_category_map.return_value = repo_map or {}

    with (
        patch(
            "code_indexer.server.web.routes._get_golden_repo_manager"
        ) as mock_grm_patch,
        patch(
            "code_indexer.server.web.routes._get_repo_category_service"
        ) as mock_svc_patch,
        patch("code_indexer.server.web.routes.set_csrf_cookie"),
    ):
        mock_grm_patch.return_value = mock_grm
        mock_svc_patch.return_value = mock_svc
        return test_client.get(ENDPOINT)


class TestCategoryEnrichment:
    """Handler enriches repos with category_id, category_name, category_priority."""

    @pytest.mark.parametrize(
        "expected_attr",
        [
            f'data-category-id="{BACKEND_CATEGORY_ID}"',
            'data-category-name="Backend"',
            f'data-category-priority="{BACKEND_CATEGORY_PRIORITY}"',
        ],
    )
    def test_categorized_repo_renders_all_data_attributes(
        self, test_client, expected_attr
    ):
        """Each categorized repo has all three data-* attributes rendered."""
        repo_map = {
            "repo-a": {
                "category_id": BACKEND_CATEGORY_ID,
                "category_name": "Backend",
                "priority": BACKEND_CATEGORY_PRIORITY,
            }
        }
        response = _fetch_partial(test_client, [{"alias": "repo-a"}], repo_map)

        assert response.status_code == 200
        assert expected_attr in response.text

    @pytest.mark.parametrize(
        "expected_attr",
        [
            'data-category-name="Unassigned"',
            f'data-category-priority="{UNASSIGNED_PRIORITY}"',
        ],
    )
    def test_uncategorized_repo_defaults(self, test_client, expected_attr):
        """Repos absent from category map get Unassigned name and max priority."""
        response = _fetch_partial(test_client, [{"alias": "repo-unknown"}], repo_map={})

        assert response.status_code == 200
        assert expected_attr in response.text

    def test_none_category_name_falls_back_to_unassigned(self, test_client):
        """Repo whose category_name is None in map renders as 'Unassigned'."""
        repo_map = {
            "repo-partial": {
                "category_id": None,
                "category_name": None,
                "priority": UNASSIGNED_PRIORITY,
            }
        }
        response = _fetch_partial(test_client, [{"alias": "repo-partial"}], repo_map)

        assert response.status_code == 200
        assert 'data-category-name="Unassigned"' in response.text


class TestCategoryEnrichmentGracefulDegradation:
    """Handler must degrade gracefully when get_repo_category_map() raises."""

    @pytest.mark.parametrize(
        "expected_attr",
        [
            'data-category-name="Unassigned"',
            f'data-category-priority="{UNASSIGNED_PRIORITY}"',
        ],
    )
    def test_get_repo_category_map_exception_falls_back_to_defaults(
        self, test_client, expected_attr
    ):
        """When get_repo_category_map() raises inside the try block,
        handler returns 200 and repos get Unassigned defaults."""
        response = _fetch_partial(
            test_client,
            [{"alias": "repo-a"}],
            map_error=RuntimeError("DB unavailable"),
        )

        assert response.status_code == 200
        assert expected_attr in response.text
