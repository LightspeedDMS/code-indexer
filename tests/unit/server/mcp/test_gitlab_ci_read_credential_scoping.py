"""
Tests for repository-level authorization and credential scoping on the
legacy GitLab CI read handlers.

`handle_gitlab_ci_list_pipelines`, `handle_gitlab_ci_get_pipeline`,
`handle_gitlab_ci_search_logs`, and `handle_gitlab_ci_get_job_logs`
(`mcp/handlers/cicd.py`) must: (1) enforce CIDX group access on every
`project_id` target via `_resolve_cicd_project_access_detailed` before
touching any credential; (2) permit the shared/global CI credential only
for a project that is both a registered CIDX golden repo AND one the
caller is allowed to see -- any other identifier (unregistered, a numeric
project id, an encoded path, or a denied project referenced under a
different identifier) must fall back to the caller's own personal
credential, or be refused; and (3) send the shared credential only to the
configured GitLab host, never to a caller-supplied `base_url`.

These tests call the handlers directly (the REAL handler code path) --
only the golden-repo registry, the AccessFilteringService, git-credential
lookup, and the outbound GitLabCIClient are mocked. The code under test
(`_resolve_cicd_project_access_detailed`, `_resolve_cicd_write_token`, and
the four `handle_gitlab_ci_*` read handlers) is never mocked.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from code_indexer.server.mcp.handlers import (
    handle_gitlab_ci_list_pipelines,
    handle_gitlab_ci_get_pipeline,
    handle_gitlab_ci_search_logs,
    handle_gitlab_ci_get_job_logs,
)

_PROJECT_PATH = "example-group/example-project"
_NUMERIC_PROJECT_ID = "987654"
_PIPELINE_ID = 42
_JOB_ID = 99

_GOLDEN_ALIAS = "example-project-global"
_GITLAB_REPO_URL = f"https://gitlab.com/{_PROJECT_PATH}.git"


def _parse_mcp_response(response: dict) -> dict:
    """Decode the inner JSON payload from an MCP envelope response."""
    content = response.get("content", [])
    assert len(content) > 0, f"Empty MCP response: {response}"
    return json.loads(content[0]["text"])  # type: ignore[no-any-return]


def _make_user(username: str = "bob"):
    user = MagicMock()
    user.username = username
    return user


def _registry_with_repo(alias: str, repo_url: str):
    return [{"alias_name": alias, "repo_url": repo_url}]


def _denied_access_patches():
    """Registry has a matching golden repo, but the caller's group does not
    include it -- the "invisible repo" denial path."""
    access_svc = MagicMock()
    access_svc.get_accessible_repos.return_value = {"some-other-project"}
    access_svc.is_admin_user.return_value = False
    return (
        patch(
            "code_indexer.server.mcp.handlers._list_global_repos",
            return_value=_registry_with_repo(_GOLDEN_ALIAS, _GITLAB_REPO_URL),
        ),
        patch(
            "code_indexer.server.mcp.handlers._get_access_filtering_service",
            return_value=access_svc,
        ),
    )


def _allowed_access_patches():
    access_svc = MagicMock()
    access_svc.get_accessible_repos.return_value = {"example-project"}
    return (
        patch(
            "code_indexer.server.mcp.handlers._list_global_repos",
            return_value=_registry_with_repo(_GOLDEN_ALIAS, _GITLAB_REPO_URL),
        ),
        patch(
            "code_indexer.server.mcp.handlers._get_access_filtering_service",
            return_value=access_svc,
        ),
    )


def _unregistered_registry_patch():
    return patch(
        "code_indexer.server.mcp.handlers._list_global_repos",
        return_value=[],
    )


_READ_HANDLER_CASES = [
    pytest.param(
        handle_gitlab_ci_list_pipelines,
        {},
        "list_pipelines",
        [],
        id="list_pipelines",
    ),
    pytest.param(
        handle_gitlab_ci_get_pipeline,
        {"pipeline_id": _PIPELINE_ID},
        "get_pipeline",
        {"id": _PIPELINE_ID, "status": "success"},
        id="get_pipeline",
    ),
    pytest.param(
        handle_gitlab_ci_search_logs,
        {"pipeline_id": _PIPELINE_ID, "pattern": "error"},
        "search_logs",
        [],
        id="search_logs",
    ),
    pytest.param(
        handle_gitlab_ci_get_job_logs,
        {"job_id": _JOB_ID},
        "get_job_logs",
        "log text",
        id="get_job_logs",
    ),
]


class TestUnregisteredProjectNeverUsesGlobalToken:
    """A `project_id` that does not match ANY registered CIDX golden repo
    is an "unregistered" (ad-hoc) target. Reads against it must use the
    caller's OWN personal credential -- refused when absent -- and must
    NEVER silently fall back to the server-wide global CI token, even
    though the global token IS configured and reachable. Parametrized over
    all 4 read handlers so each one is independently proven to route
    through this check."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,extra_args,method_name,return_value", _READ_HANDLER_CASES
    )
    async def test_unregistered_without_personal_credential_is_refused(
        self, handler, extra_args, method_name, return_value
    ):
        user = _make_user()
        args = {"project_id": _PROJECT_PATH, **extra_args}
        with (
            _unregistered_registry_patch(),
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
                return_value="GLOBAL_TOKEN_MUST_NEVER_BE_USED",
            ) as mock_global_token,
            patch(
                "code_indexer.server.mcp.handlers._get_personal_credential_for_host",
                return_value=None,
            ),
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as mock_client_cls,
        ):
            response = await handler(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert "credential" in data["error"].lower()
        mock_global_token.assert_not_called()
        mock_client_cls.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,extra_args,method_name,return_value", _READ_HANDLER_CASES
    )
    async def test_unregistered_with_personal_credential_uses_it_never_global(
        self, handler, extra_args, method_name, return_value
    ):
        user = _make_user()
        args = {"project_id": _PROJECT_PATH, **extra_args}
        with (
            _unregistered_registry_patch(),
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
                return_value="GLOBAL_TOKEN_MUST_NEVER_BE_USED",
            ) as mock_global_token,
            patch(
                "code_indexer.server.mcp.handlers._get_personal_credential_for_host",
                return_value={"token": "personal_pat_xyz"},
            ),
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as mock_client_cls,
        ):
            mock_instance = MagicMock()
            setattr(mock_instance, method_name, AsyncMock(return_value=return_value))
            mock_instance.last_rate_limit = None
            mock_client_cls.return_value = mock_instance

            response = await handler(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is True
        mock_global_token.assert_not_called()
        mock_client_cls.assert_called_once_with(
            "personal_pat_xyz", base_url="https://gitlab.com"
        )


class TestRegisteredAndAllowedProjectUsesGlobalToken:
    """Contrast case: a project that IS a registered + allowed golden repo
    is permitted to use the global CI token (unlike the unregistered case
    above). Parametrized over all 4 read handlers."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,extra_args,method_name,return_value", _READ_HANDLER_CASES
    )
    async def test_registered_and_allowed_uses_global_token(
        self, handler, extra_args, method_name, return_value
    ):
        user = _make_user()
        args = {"project_id": _PROJECT_PATH, **extra_args}
        p1, p2 = _allowed_access_patches()
        with (
            p1,
            p2,
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
                return_value="global_gitlab_token",
            ) as mock_global_token,
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as mock_client_cls,
        ):
            mock_instance = MagicMock()
            setattr(mock_instance, method_name, AsyncMock(return_value=return_value))
            mock_instance.last_rate_limit = None
            mock_client_cls.return_value = mock_instance

            response = await handler(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is True
        mock_global_token.assert_called_once()
        mock_client_cls.assert_called_once_with(
            "global_gitlab_token", base_url="https://gitlab.com"
        )


class TestOtherIdentifierForDeniedProjectNeverReachesGlobalToken:
    """A project registered under its path form, and DENIED to the caller
    under that path, must stay denied a global credential even when
    referenced by a different identifier for the same project (e.g. a
    numeric project id) that does not textually match the registered
    path -- that identifier is simply unregistered from the matcher's
    point of view, so it takes the personal-credential path rather than
    silently regaining access to the shared global token. Parametrized
    over all 4 read handlers."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,extra_args,method_name,return_value", _READ_HANDLER_CASES
    )
    async def test_numeric_id_for_denied_project_never_reaches_global_token(
        self, handler, extra_args, method_name, return_value
    ):
        user = _make_user()
        args = {"project_id": _NUMERIC_PROJECT_ID, **extra_args}
        p1, p2 = _denied_access_patches()
        with (
            p1,
            p2,
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
                return_value="GLOBAL_TOKEN_MUST_NEVER_BE_USED",
            ) as mock_global_token,
            patch(
                "code_indexer.server.mcp.handlers._get_personal_credential_for_host",
                return_value={"token": "personal_pat_xyz"},
            ),
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as mock_client_cls,
        ):
            mock_instance = MagicMock()
            setattr(mock_instance, method_name, AsyncMock(return_value=return_value))
            mock_instance.last_rate_limit = None
            mock_client_cls.return_value = mock_instance

            response = await handler(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is True
        mock_global_token.assert_not_called()
        mock_client_cls.assert_called_once_with(
            "personal_pat_xyz", base_url="https://gitlab.com"
        )


class TestCallerSuppliedBaseUrlNeverReceivesGlobalToken:
    """When a registered-and-allowed project is eligible for the shared
    global CI token, the outbound call must target the configured GitLab
    host -- a `base_url` supplied in the request arguments must never
    change which host the shared credential is sent to. Parametrized over
    all 4 read handlers."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,extra_args,method_name,return_value", _READ_HANDLER_CASES
    )
    async def test_caller_base_url_never_receives_global_token(
        self, handler, extra_args, method_name, return_value
    ):
        user = _make_user()
        args = {
            "project_id": _PROJECT_PATH,
            "base_url": "https://other-host.example.com",
            **extra_args,
        }
        p1, p2 = _allowed_access_patches()
        with (
            p1,
            p2,
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
                return_value="global_gitlab_token",
            ) as mock_global_token,
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as mock_client_cls,
        ):
            mock_instance = MagicMock()
            setattr(mock_instance, method_name, AsyncMock(return_value=return_value))
            mock_instance.last_rate_limit = None
            mock_client_cls.return_value = mock_instance

            response = await handler(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is True
        mock_global_token.assert_called_once()
        mock_client_cls.assert_called_once_with(
            "global_gitlab_token", base_url="https://gitlab.com"
        )
