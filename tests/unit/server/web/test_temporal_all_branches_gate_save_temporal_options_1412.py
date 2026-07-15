"""Unit tests for Story #1412 - Web UI temporal-options form must reject
all_branches=true when the temporal_all_branches_enabled gate is off.

Mirrors tests/unit/server/web/test_save_temporal_options_route.py's mocking
pattern (patch _require_admin_session, validate_login_csrf_token,
_get_golden_repo_manager, templates).
"""

from unittest.mock import MagicMock, patch
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


def _make_request():
    mock_request = MagicMock(spec=Request)
    mock_request.cookies = {}
    return mock_request


def _make_session(username="admin"):
    mock_session = MagicMock()
    mock_session.username = username
    mock_session.role = "admin"
    return mock_session


def _make_gate_config(enabled: bool):
    """Return a mock get_config_service() whose gate resolves to `enabled`."""
    mock_svc = MagicMock()
    mock_indexing = MagicMock()
    mock_indexing.temporal_all_branches_enabled = enabled
    mock_server_cfg = MagicMock()
    mock_server_cfg.indexing_config = mock_indexing
    mock_svc.get_config.return_value = mock_server_cfg
    return mock_svc


class TestSaveTemporalOptionsGateOffRejectsAllBranches:
    """AC2/Scenario 3: gate off + all_branches=1 -> 400, not saved."""

    def test_gate_off_all_branches_returns_400(self):
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
                "src.code_indexer.server.web.routes.get_config_service",
                return_value=_make_gate_config(False),
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
                since_date=None,
                all_branches="1",
                csrf_token="valid-token",
            )

        mock_templates.TemplateResponse.assert_called_once()
        call_args = mock_templates.TemplateResponse.call_args
        assert call_args[0][1] == "partials/error_message.html"
        context = call_args[0][2]
        assert "error" in context
        assert (
            "all-branches" in context["error"].lower()
            or "all branches" in context["error"].lower()
        )
        assert "temporal_all_branches_enabled" in context["error"]
        assert call_args[1]["status_code"] == 400

        mock_manager.save_temporal_options.assert_not_called()

    def test_gate_off_all_branches_not_requested_saves_fine(self):
        """Gate off + all_branches not checked -> saved normally (no rejection)."""
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
                "src.code_indexer.server.web.routes.get_config_service",
                return_value=_make_gate_config(False),
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
                since_date=None,
                all_branches=None,
                csrf_token="valid-token",
            )

        mock_manager.save_temporal_options.assert_called_once()
        call_args = mock_manager.save_temporal_options.call_args
        saved_options = call_args[0][1]
        assert saved_options.get("all_branches") is False


class TestSaveTemporalOptionsGateOnAcceptsAllBranches:
    """AC6/Scenario 6: gate on + all_branches=1 -> saved as True."""

    def test_gate_on_all_branches_is_saved(self):
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
                "src.code_indexer.server.web.routes.get_config_service",
                return_value=_make_gate_config(True),
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
                since_date=None,
                all_branches="1",
                csrf_token="valid-token",
            )

        mock_manager.save_temporal_options.assert_called_once()
        call_args = mock_manager.save_temporal_options.call_args
        saved_options = call_args[0][1]
        assert saved_options.get("all_branches") is True
