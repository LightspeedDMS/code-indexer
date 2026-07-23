"""Unit tests for Story #1412 - golden_repo_details_partial must pass the
temporal_all_branches_enabled gate value into the template context so the
partial can disable/hide the all-branches checkbox with an explanatory note
when the gate is off.

Mirrors the mocking pattern from
tests/unit/server/web/test_golden_repo_details_endpoint.py.
"""

from typing import cast
from unittest.mock import MagicMock, patch

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


def _make_request() -> MagicMock:
    req = MagicMock(spec=Request)
    req.cookies = {}
    req.app = MagicMock()
    req.app.state.backend_registry = None
    return req


def _make_admin_session(username: str = "admin") -> MagicMock:
    session = MagicMock()
    session.username = username
    session.role = "admin"
    return session


def _make_base_repo_dict(alias: str) -> dict:
    return {
        "alias": alias,
        "repo_url": "git+https://example.test/org/repo",
        "default_branch": "main",
        "status": "ready",
        "error_message": None,
        "created_at": "2024-01-01T00:00:00",
        "file_count": 42,
        "chunk_count": 1234,
        "wiki_enabled": False,
        "temporal_options": None,
        "clone_path": None,
    }


def _make_mock_templates() -> MagicMock:
    mock = MagicMock(spec=Jinja2Templates)
    mock.TemplateResponse.return_value = HTMLResponse(
        content="<html>details</html>", status_code=200
    )
    return mock


def _make_gate_config(enabled: bool):
    mock_svc = MagicMock()
    mock_indexing = MagicMock()
    mock_indexing.temporal_all_branches_enabled = enabled
    mock_server_cfg = MagicMock()
    mock_server_cfg.indexing_config = mock_indexing
    mock_svc.get_config.return_value = mock_server_cfg
    return mock_svc


def _invoke_handler(alias: str, gate_enabled: bool) -> dict:
    """Invoke golden_repo_details_partial and return the template context."""
    from src.code_indexer.server.web.routes import golden_repo_details_partial

    req = _make_request()
    session = _make_admin_session()
    repo = _make_base_repo_dict(alias)

    mock_manager = MagicMock()
    mock_manager.list_golden_repos.return_value = [repo]
    mock_manager.golden_repos = {alias: repo}

    mock_category_service = MagicMock()
    mock_category_service.list_categories.return_value = []
    mock_category_service.get_repo_category_map.return_value = {}

    mock_tmpl = _make_mock_templates()

    with (
        patch(
            "src.code_indexer.server.web.routes._require_admin_session",
            return_value=session,
        ),
        patch(
            "src.code_indexer.server.web.routes._get_golden_repo_manager",
            return_value=mock_manager,
        ),
        patch(
            "src.code_indexer.server.web.routes._get_repo_category_service",
            return_value=mock_category_service,
        ),
        patch(
            "src.code_indexer.server.web.routes.get_csrf_token_from_cookie",
            return_value="csrf-tok",
        ),
        patch("src.code_indexer.server.web.routes.set_csrf_cookie", MagicMock()),
        patch("src.code_indexer.server.web.routes.templates", mock_tmpl),
        patch(
            "src.code_indexer.server.web.routes.get_config_service",
            return_value=_make_gate_config(gate_enabled),
        ),
    ):
        golden_repo_details_partial(request=req, alias=alias)

    assert mock_tmpl.TemplateResponse.called
    return cast(dict, mock_tmpl.TemplateResponse.call_args[0][2])


class TestGoldenRepoDetailsPartialGateContext:
    """AC3/Scenario 3: template context must expose the gate value."""

    def test_context_includes_gate_true_when_enabled(self) -> None:
        context = _invoke_handler("gate-on-repo", gate_enabled=True)
        assert "temporal_all_branches_enabled" in context
        assert context["temporal_all_branches_enabled"] is True

    def test_context_includes_gate_false_when_disabled(self) -> None:
        context = _invoke_handler("gate-off-repo", gate_enabled=False)
        assert "temporal_all_branches_enabled" in context
        assert context["temporal_all_branches_enabled"] is False
