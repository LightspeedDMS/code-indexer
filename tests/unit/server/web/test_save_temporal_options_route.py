"""
Unit tests for save_temporal_options route - since_date validation (Story #478 bug fix).

Tests that the POST /golden-repos/{alias}/temporal-options route validates
the since_date field is in YYYY-MM-DD format before saving to SQLite.
"""

from unittest.mock import MagicMock, patch
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


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


class TestSaveTemporalOptionsSinceDateValidation:
    """Tests for since_date format validation in save_temporal_options route."""

    def test_invalid_since_date_format_returns_400(self):
        """
        Bug fix: since_date with invalid format (e.g. 'not-a-date') must be
        rejected with a 400 error response and NOT saved to SQLite.
        """
        from src.code_indexer.server.web.routes import save_temporal_options

        mock_request = _make_request()
        mock_session = _make_session()
        mock_manager = MagicMock()

        mock_templates = MagicMock(spec=Jinja2Templates)
        mock_templates.TemplateResponse.return_value = HTMLResponse(
            content="<html>error</html>", status_code=400
        )

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=mock_session,
            ),
            patch(
                "src.code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=True,
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_manager",
                return_value=mock_manager,
            ),
            patch(
                "src.code_indexer.server.web.routes.templates",
                mock_templates,
            ),
        ):
            save_temporal_options(
                request=mock_request,
                alias="test-repo",
                max_commits=None,
                diff_context=None,
                since_date="not-a-date",
                all_branches=None,
                csrf_token="valid-token",
            )

        # Must have returned a 400 error response via templates.TemplateResponse
        mock_templates.TemplateResponse.assert_called_once()
        call_args = mock_templates.TemplateResponse.call_args
        assert call_args[0][0] == "partials/error_message.html"
        context = call_args[0][1]
        assert "error" in context
        assert "YYYY-MM-DD" in context["error"]
        assert call_args[1]["status_code"] == 400

        # Manager must NOT have saved anything
        mock_manager.save_temporal_options.assert_not_called()

    def test_invalid_since_date_wrong_separator_returns_400(self):
        """
        Bug fix: since_date with wrong separator (e.g. '2024/01/15') must be rejected.
        """
        from src.code_indexer.server.web.routes import save_temporal_options

        mock_request = _make_request()
        mock_session = _make_session()
        mock_manager = MagicMock()

        mock_templates = MagicMock(spec=Jinja2Templates)
        mock_templates.TemplateResponse.return_value = HTMLResponse(
            content="<html>error</html>", status_code=400
        )

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=mock_session,
            ),
            patch(
                "src.code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=True,
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_manager",
                return_value=mock_manager,
            ),
            patch(
                "src.code_indexer.server.web.routes.templates",
                mock_templates,
            ),
        ):
            save_temporal_options(
                request=mock_request,
                alias="test-repo",
                max_commits=None,
                diff_context=None,
                since_date="2024/01/15",
                all_branches=None,
                csrf_token="valid-token",
            )

        mock_templates.TemplateResponse.assert_called_once()
        call_args = mock_templates.TemplateResponse.call_args
        assert call_args[1]["status_code"] == 400
        mock_manager.save_temporal_options.assert_not_called()

    def test_valid_since_date_is_saved(self):
        """
        Valid since_date in YYYY-MM-DD format must pass validation and be saved.
        """
        from src.code_indexer.server.web.routes import save_temporal_options

        mock_request = _make_request()
        mock_session = _make_session()
        mock_manager = MagicMock()

        mock_page = MagicMock(return_value=HTMLResponse(content="<html>ok</html>"))

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=mock_session,
            ),
            patch(
                "src.code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=True,
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_manager",
                return_value=mock_manager,
            ),
            patch(
                "src.code_indexer.server.web.routes._create_golden_repos_page_response",
                mock_page,
            ),
        ):
            save_temporal_options(
                request=mock_request,
                alias="test-repo",
                max_commits=None,
                diff_context=None,
                since_date="2024-01-15",
                all_branches=None,
                csrf_token="valid-token",
            )

        # save_temporal_options must have been called with since_date included
        mock_manager.save_temporal_options.assert_called_once()
        call_args = mock_manager.save_temporal_options.call_args
        saved_options = call_args[0][1]
        assert saved_options.get("since_date") == "2024-01-15"

    def test_empty_since_date_is_not_saved(self):
        """
        Empty/blank since_date must be silently ignored (not saved, no error).
        """
        from src.code_indexer.server.web.routes import save_temporal_options

        mock_request = _make_request()
        mock_session = _make_session()
        mock_manager = MagicMock()

        mock_page = MagicMock(return_value=HTMLResponse(content="<html>ok</html>"))

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=mock_session,
            ),
            patch(
                "src.code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=True,
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_manager",
                return_value=mock_manager,
            ),
            patch(
                "src.code_indexer.server.web.routes._create_golden_repos_page_response",
                mock_page,
            ),
        ):
            save_temporal_options(
                request=mock_request,
                alias="test-repo",
                max_commits=None,
                diff_context=None,
                since_date="   ",
                all_branches=None,
                csrf_token="valid-token",
            )

        # save_temporal_options must have been called, but without since_date key
        mock_manager.save_temporal_options.assert_called_once()
        call_args = mock_manager.save_temporal_options.call_args
        saved_options = call_args[0][1]
        assert "since_date" not in saved_options
