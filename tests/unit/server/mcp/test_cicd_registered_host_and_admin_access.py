"""
Tests for two host-and-credential-scoping behaviors of the legacy CI/CD
read handlers:

1. A registered golden repo hosted on a self-hosted GitLab instance is
   queried against ITS OWN registered host -- never the default
   gitlab.com host, and never a caller-supplied `base_url` -- while still
   being permitted to use the shared global credential.
2. An admin caller has access to the shared global credential for CI/CD
   reads of a project that is not itself a registered CIDX golden repo,
   matching the admin access already used elsewhere in the access model.
   A non-admin caller (whether or not an access-control service is
   configured) must still use a personal credential for the same
   project. Even for an admin caller, the shared credential is sent only
   to the configured GitLab host, never to a caller-supplied `base_url`.

These tests call the handlers directly -- only the golden-repo registry,
the AccessFilteringService, git-credential lookup, and the outbound
GitLabCIClient/GitHubActionsClient are mocked. The code under test
(`_resolve_cicd_project_access_detailed` and the read handlers) is never
mocked.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from code_indexer.server.mcp.handlers import (
    handle_gitlab_ci_list_pipelines,
    handle_gitlab_ci_get_pipeline,
    handle_gitlab_ci_search_logs,
    handle_gitlab_ci_get_job_logs,
    handle_gh_actions_list_runs,
)

_PROJECT_PATH = "example-group/example-project"
_SELF_HOSTED_URL = f"https://gitlab.example.internal/{_PROJECT_PATH}.git"
_SELF_HOSTED_BASE_URL = "https://gitlab.example.internal"
_GOLDEN_ALIAS = "example-project-global"
_PIPELINE_ID = 7
_JOB_ID = 21

_GH_OWNER = "example-org"
_GH_REPO = "example-repo"

_UNREGISTERED_PROJECT_ID = "unregistered-group/unregistered-project"


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


def _registered_project_patches(repo_url: str, accessible_repos: set):
    access_svc = MagicMock()
    access_svc.get_accessible_repos.return_value = accessible_repos
    access_svc.is_admin_user.return_value = False
    return (
        patch(
            "code_indexer.server.mcp.handlers._list_global_repos",
            return_value=_registry_with_repo(_GOLDEN_ALIAS, repo_url),
        ),
        patch(
            "code_indexer.server.mcp.handlers._get_access_filtering_service",
            return_value=access_svc,
        ),
    )


def _unregistered_admin_patches():
    access_svc = MagicMock()
    access_svc.is_admin_user.return_value = True
    return (
        patch(
            "code_indexer.server.mcp.handlers._list_global_repos",
            return_value=[],
        ),
        patch(
            "code_indexer.server.mcp.handlers._get_access_filtering_service",
            return_value=access_svc,
        ),
    )


def _unregistered_non_admin_with_access_service_patches():
    access_svc = MagicMock()
    access_svc.is_admin_user.return_value = False
    return (
        patch(
            "code_indexer.server.mcp.handlers._list_global_repos",
            return_value=[],
        ),
        patch(
            "code_indexer.server.mcp.handlers._get_access_filtering_service",
            return_value=access_svc,
        ),
    )


def _unregistered_no_access_service_patches():
    return (
        patch(
            "code_indexer.server.mcp.handlers._list_global_repos",
            return_value=[],
        ),
        patch(
            "code_indexer.server.mcp.handlers._get_access_filtering_service",
            return_value=None,
        ),
    )


_GITLAB_READ_HANDLER_CASES = [
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


def _mock_gitlab_client(mock_client_cls, method_name, return_value):
    mock_instance = MagicMock()
    setattr(mock_instance, method_name, AsyncMock(return_value=return_value))
    mock_instance.last_rate_limit = None
    mock_client_cls.return_value = mock_instance


class TestRegisteredSelfHostedProjectTargetsItsOwnHost:
    """A registered golden repo on a self-hosted GitLab instance is queried
    against its own registered host, using the shared global credential --
    a caller-supplied base_url is ignored. Parametrized over all 4 GitLab
    read handlers."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,extra_args,method_name,return_value", _GITLAB_READ_HANDLER_CASES
    )
    async def test_self_hosted_registered_project_uses_its_own_host(
        self, handler, extra_args, method_name, return_value
    ):
        user = _make_user()
        args = {
            "project_id": _PROJECT_PATH,
            "base_url": "https://other-host.example.com",
            **extra_args,
        }
        p1, p2 = _registered_project_patches(_SELF_HOSTED_URL, {"example-project"})
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
            _mock_gitlab_client(mock_client_cls, method_name, return_value)
            response = await handler(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is True
        mock_global_token.assert_called_once()
        mock_client_cls.assert_called_once_with(
            "global_gitlab_token", base_url=_SELF_HOSTED_BASE_URL
        )


class TestAdminAccessForUnregisteredProject:
    """An admin's CI/CD read of a project that is not itself a registered
    CIDX golden repo still uses the shared global credential. A non-admin
    caller -- with an access-control service configured, or with none
    configured at all -- must still use a personal credential for the
    same project."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,extra_args,method_name,return_value", _GITLAB_READ_HANDLER_CASES
    )
    async def test_admin_uses_global_token_for_gitlab_unregistered_project(
        self, handler, extra_args, method_name, return_value
    ):
        user = _make_user()
        args = {"project_id": _UNREGISTERED_PROJECT_ID, **extra_args}
        p1, p2 = _unregistered_admin_patches()
        with (
            p1,
            p2,
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
                return_value="global_gitlab_token",
            ) as mock_global_token,
            patch(
                "code_indexer.server.mcp.handlers._get_personal_credential_for_host"
            ) as mock_personal_cred,
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as mock_client_cls,
        ):
            _mock_gitlab_client(mock_client_cls, method_name, return_value)
            response = await handler(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is True
        mock_global_token.assert_called_once()
        mock_personal_cred.assert_not_called()
        mock_client_cls.assert_called_once_with(
            "global_gitlab_token", base_url="https://gitlab.com"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,extra_args,method_name,return_value", _GITLAB_READ_HANDLER_CASES
    )
    async def test_admin_with_caller_base_url_still_only_targets_gitlab_com(
        self, handler, extra_args, method_name, return_value
    ):
        """Even for an admin caller, a caller-supplied base_url never
        changes where the shared global credential is sent -- the
        outbound call always targets the configured GitLab host."""
        user = _make_user()
        args = {
            "project_id": _UNREGISTERED_PROJECT_ID,
            "base_url": "https://other-host.example.com",
            **extra_args,
        }
        p1, p2 = _unregistered_admin_patches()
        with (
            p1,
            p2,
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
                return_value="global_gitlab_token",
            ) as mock_global_token,
            patch(
                "code_indexer.server.mcp.handlers._get_personal_credential_for_host"
            ) as mock_personal_cred,
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as mock_client_cls,
        ):
            _mock_gitlab_client(mock_client_cls, method_name, return_value)
            response = await handler(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is True
        mock_global_token.assert_called_once()
        mock_personal_cred.assert_not_called()
        mock_client_cls.assert_called_once_with(
            "global_gitlab_token", base_url="https://gitlab.com"
        )

    @pytest.mark.asyncio
    async def test_admin_uses_global_token_for_github_unregistered_repo(self):
        user = _make_user()
        args = {"repository": f"{_GH_OWNER}/{_GH_REPO}"}
        p1, p2 = _unregistered_admin_patches()
        with (
            p1,
            p2,
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
                return_value="global_github_token",
            ) as mock_global_token,
            patch(
                "code_indexer.server.mcp.handlers._get_personal_credential_for_host"
            ) as mock_personal_cred,
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as mock_client_cls,
        ):
            mock_instance = MagicMock()
            mock_instance.list_runs = AsyncMock(return_value=[])
            mock_instance.last_rate_limit = None
            mock_client_cls.return_value = mock_instance

            response = await handle_gh_actions_list_runs(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is True
        mock_global_token.assert_called_once()
        mock_personal_cred.assert_not_called()
        mock_client_cls.assert_called_once_with("global_github_token")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,extra_args,method_name,return_value", _GITLAB_READ_HANDLER_CASES
    )
    async def test_non_admin_with_access_service_still_needs_personal_credential(
        self, handler, extra_args, method_name, return_value
    ):
        user = _make_user()
        args = {"project_id": _UNREGISTERED_PROJECT_ID, **extra_args}
        p1, p2 = _unregistered_non_admin_with_access_service_patches()
        with (
            p1,
            p2,
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
        "handler,extra_args,method_name,return_value", _GITLAB_READ_HANDLER_CASES
    )
    async def test_no_access_service_configured_still_needs_personal_credential(
        self, handler, extra_args, method_name, return_value
    ):
        user = _make_user()
        args = {"project_id": _UNREGISTERED_PROJECT_ID, **extra_args}
        with (
            _unregistered_no_access_service_patches()[0],
            _unregistered_no_access_service_patches()[1],
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
