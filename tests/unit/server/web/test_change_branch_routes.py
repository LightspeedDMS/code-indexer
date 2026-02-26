"""
Unit tests for Story #303: Change Active Branch web routes.

Tests:
1. POST /golden-repos/{alias}/change-branch requires admin session
2. POST /golden-repos/{alias}/change-branch returns 400 when branch field missing
3. POST /golden-repos/{alias}/change-branch calls manager.change_branch and returns success
4. POST /golden-repos/{alias}/change-branch returns 404 on FileNotFoundError
5. POST /golden-repos/{alias}/change-branch returns 409 on conflict RuntimeError
6. GET /golden-repos/{alias}/branches requires admin session
7. GET /golden-repos/{alias}/branches returns 500 when branch service not available
8. GET /golden-repos/{alias}/branches returns branch list on success
9. GET /golden-repos/{alias}/branches returns 404 on FileNotFoundError
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi import Request
from fastapi.responses import JSONResponse


def _make_request():
    """Create a mock request with cookies dict."""
    mock_request = MagicMock(spec=Request)
    mock_request.cookies = {}
    return mock_request


def _make_session(username="admin"):
    """Create a mock admin session."""
    mock_session = MagicMock()
    mock_session.username = username
    mock_session.role = "admin"
    return mock_session


def _make_branch(name, is_default=False):
    """Create a mock branch info object."""
    branch = MagicMock()
    branch.name = name
    branch.last_commit_hash = "abc123"
    branch.last_commit_author = "Test Author"
    branch.branch_type = "feature"
    branch.is_default = is_default
    return branch


class TestChangeBranchRoute:
    """Tests for POST /golden-repos/{alias}/change-branch endpoint."""

    @pytest.mark.asyncio
    async def test_change_branch_requires_admin_session(self):
        """Auth check: no session returns 401."""
        from src.code_indexer.server.web.routes import change_golden_repo_branch

        mock_request = _make_request()

        with patch(
            "src.code_indexer.server.web.routes._require_admin_session",
            return_value=None,
        ):
            result = await change_golden_repo_branch(
                request=mock_request,
                alias="my-repo",
            )

        assert result.status_code == 401
        import json
        body = json.loads(result.body)
        assert body["error"] == "Authentication required"

    @pytest.mark.asyncio
    async def test_change_branch_missing_branch_field_returns_400(self):
        """Returns 400 when JSON body is missing 'branch' field."""
        from src.code_indexer.server.web.routes import change_golden_repo_branch

        mock_request = _make_request()
        mock_request.json = AsyncMock(return_value={})  # no 'branch' key

        with patch(
            "src.code_indexer.server.web.routes._require_admin_session",
            return_value=_make_session(),
        ):
            result = await change_golden_repo_branch(
                request=mock_request,
                alias="my-repo",
            )

        assert result.status_code == 400
        import json
        body = json.loads(result.body)
        assert "branch" in body["error"]

    @pytest.mark.asyncio
    async def test_change_branch_success(self):
        """Returns 200 with success message when manager.change_branch succeeds."""
        from src.code_indexer.server.web.routes import change_golden_repo_branch

        mock_request = _make_request()
        mock_request.json = AsyncMock(return_value={"branch": "feature/new"})

        mock_manager = MagicMock()
        mock_manager.change_branch.return_value = {"success": True, "message": "Branch changed to feature/new"}

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=_make_session(),
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_manager",
                return_value=mock_manager,
            ),
        ):
            result = await change_golden_repo_branch(
                request=mock_request,
                alias="my-repo",
            )

        assert result.status_code == 200
        import json
        body = json.loads(result.body)
        assert body["success"] is True
        assert "feature/new" in body["message"]
        mock_manager.change_branch.assert_called_once_with("my-repo", "feature/new")

    @pytest.mark.asyncio
    async def test_change_branch_returns_404_on_not_found(self):
        """Returns 404 when FileNotFoundError raised."""
        from src.code_indexer.server.web.routes import change_golden_repo_branch

        mock_request = _make_request()
        mock_request.json = AsyncMock(return_value={"branch": "main"})

        mock_manager = MagicMock()
        mock_manager.change_branch.side_effect = FileNotFoundError("Repo not found")

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=_make_session(),
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_manager",
                return_value=mock_manager,
            ),
        ):
            result = await change_golden_repo_branch(
                request=mock_request,
                alias="missing-repo",
            )

        assert result.status_code == 404
        import json
        body = json.loads(result.body)
        assert "not found" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_change_branch_returns_409_on_conflict(self):
        """Returns 409 when RuntimeError contains 'conflict'."""
        from src.code_indexer.server.web.routes import change_golden_repo_branch

        mock_request = _make_request()
        mock_request.json = AsyncMock(return_value={"branch": "main"})

        mock_manager = MagicMock()
        mock_manager.change_branch.side_effect = RuntimeError(
            "conflict: repo is locked"
        )

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=_make_session(),
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_manager",
                return_value=mock_manager,
            ),
        ):
            result = await change_golden_repo_branch(
                request=mock_request,
                alias="busy-repo",
            )

        assert result.status_code == 409

    @pytest.mark.asyncio
    async def test_change_branch_returns_400_on_value_error(self):
        """Returns 400 when ValueError raised (e.g. invalid branch name)."""
        from src.code_indexer.server.web.routes import change_golden_repo_branch

        mock_request = _make_request()
        mock_request.json = AsyncMock(return_value={"branch": "invalid branch!"})

        mock_manager = MagicMock()
        mock_manager.change_branch.side_effect = ValueError("Invalid branch name")

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=_make_session(),
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_manager",
                return_value=mock_manager,
            ),
        ):
            result = await change_golden_repo_branch(
                request=mock_request,
                alias="my-repo",
            )

        assert result.status_code == 400
        import json
        body = json.loads(result.body)
        assert "Invalid branch" in body["error"]


class TestGetBranchesRoute:
    """Tests for GET /golden-repos/{alias}/branches endpoint."""

    def test_get_branches_requires_admin_session(self):
        """Auth check: no session returns 401."""
        from src.code_indexer.server.web.routes import get_golden_repo_branches

        mock_request = _make_request()

        with patch(
            "src.code_indexer.server.web.routes._require_admin_session",
            return_value=None,
        ):
            result = get_golden_repo_branches(
                request=mock_request,
                alias="my-repo",
            )

        assert result.status_code == 401
        import json
        body = json.loads(result.body)
        assert body["error"] == "Authentication required"

    def test_get_branches_service_not_available_returns_500(self):
        """Returns 500 when golden_repo_branch_service is not on app.state."""
        from src.code_indexer.server.web.routes import get_golden_repo_branches

        mock_request = _make_request()

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=_make_session(),
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_branch_service",
                return_value=None,
            ),
        ):
            result = get_golden_repo_branches(
                request=mock_request,
                alias="my-repo",
            )

        assert result.status_code == 500
        import json
        body = json.loads(result.body)
        assert "not available" in body["error"]

    def test_get_branches_returns_branch_list(self):
        """Returns 200 with list of branches when service is available."""
        from src.code_indexer.server.web.routes import get_golden_repo_branches

        mock_request = _make_request()

        b1 = _make_branch("main", is_default=True)
        b1.branch_type = "main"
        b2 = _make_branch("feature/auth", is_default=False)
        b2.branch_type = "feature"

        mock_branch_service = MagicMock()
        mock_branch_service.get_golden_repo_branches.return_value = [b1, b2]

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=_make_session(),
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_branch_service",
                return_value=mock_branch_service,
            ),
        ):
            result = get_golden_repo_branches(
                request=mock_request,
                alias="my-repo",
            )

        assert result.status_code == 200
        import json
        body = json.loads(result.body)
        assert "branches" in body
        assert len(body["branches"]) == 2
        assert body["branches"][0]["name"] == "main"
        assert body["branches"][0]["is_default"] is True
        assert body["branches"][1]["name"] == "feature/auth"
        mock_branch_service.get_golden_repo_branches.assert_called_once_with("my-repo")

    def test_get_branches_returns_404_on_not_found(self):
        """Returns 404 when FileNotFoundError raised by branch service."""
        from src.code_indexer.server.web.routes import get_golden_repo_branches

        mock_request = _make_request()

        mock_branch_service = MagicMock()
        mock_branch_service.get_golden_repo_branches.side_effect = FileNotFoundError(
            "Repo not found"
        )

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=_make_session(),
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_branch_service",
                return_value=mock_branch_service,
            ),
        ):
            result = get_golden_repo_branches(
                request=mock_request,
                alias="missing-repo",
            )

        assert result.status_code == 404
        import json
        body = json.loads(result.body)
        assert "not found" in body["error"].lower()
