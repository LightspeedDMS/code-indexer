"""
Tests for repository-level authorization on the legacy GitHub Actions
REST routes.

REST `/api/cicd/github/{owner}/{repo}/...` (6 routes, `routes/cicd.py`)
delegate to `handle_gh_actions_*` in `mcp/handlers/cicd.py`. These handlers
must, like their GitLab and unified `ci_*` twins: (1) enforce CIDX group
access on every `{owner}/{repo}` target via `_resolve_cicd_project_access`
before touching any credential; (2) permit a shared/global CI credential
only for a repository that is both a registered CIDX golden repo AND one
the caller is allowed to see -- any other repository must fall back to the
caller's own personal credential, or be refused; and (3) resolve mutating
operations (`retry_run`, `cancel_run`) through a per-user credential only,
never the shared global one.

These tests exercise the REST front door (FastAPI TestClient over
`routes.cicd.router`) through the REAL handler code path -- only the
golden-repo registry, the AccessFilteringService, git-credential lookup, and
the outbound GitHubActionsClient are mocked. The code under test
(`_resolve_cicd_project_access`, `_resolve_cicd_write_token`, and the six
`handle_gh_actions_*` handlers) is never mocked.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.routes.cicd import router as cicd_router
from code_indexer.server.mcp.handlers import (
    _resolve_cicd_write_token,
    handle_gh_actions_retry_run,
    handle_gh_actions_cancel_run,
)

_OWNER = "example-org"
_REPO = "example-repo"
_RUN_ID = 42
_JOB_ID = 99

_GOLDEN_ALIAS = "example-repo-global"
_GITHUB_REPO_URL = f"https://github.com/{_OWNER}/{_REPO}.git"


def _parse_mcp_response(response_json: dict) -> dict:
    """Decode the inner JSON payload from an MCP envelope response."""
    content = response_json.get("content", [])
    assert len(content) > 0, f"Empty MCP response: {response_json}"
    return json.loads(content[0]["text"])  # type: ignore[no-any-return]


def _make_user(username: str = "bob"):
    user = MagicMock()
    user.username = username
    return user


def _registry_with_repo(alias: str, repo_url: str):
    return [{"alias_name": alias, "repo_url": repo_url}]


@pytest.fixture
def app():
    fastapi_app = FastAPI()
    fastapi_app.include_router(cicd_router)
    return fastapi_app


@pytest.fixture
def client(app):
    return TestClient(app)


def _override_user(app, user):
    async def _get_user():
        return user

    app.dependency_overrides[get_current_user] = _get_user


def _denied_access_patches():
    """Registry has a matching golden repo, but the caller's group does not
    include it -- the exact "invisible repo" denial path."""
    access_svc = MagicMock()
    access_svc.get_accessible_repos.return_value = {"some-other-repo"}
    return (
        patch(
            "code_indexer.server.mcp.handlers._list_global_repos",
            return_value=_registry_with_repo(_GOLDEN_ALIAS, _GITHUB_REPO_URL),
        ),
        patch(
            "code_indexer.server.mcp.handlers._get_access_filtering_service",
            return_value=access_svc,
        ),
    )


def _allowed_access_patches():
    access_svc = MagicMock()
    access_svc.get_accessible_repos.return_value = {"example-repo"}
    return (
        patch(
            "code_indexer.server.mcp.handlers._list_global_repos",
            return_value=_registry_with_repo(_GOLDEN_ALIAS, _GITHUB_REPO_URL),
        ),
        patch(
            "code_indexer.server.mcp.handlers._get_access_filtering_service",
            return_value=access_svc,
        ),
    )


class TestGhActionsReadRoutesEnforceGroupAccess:
    """All 4 read-only legacy GitHub handlers must check group access
    BEFORE touching any credential, exactly like their GitLab and unified
    ci_* twins."""

    def test_list_runs_denied_before_token_resolution(self, app, client):
        _override_user(app, _make_user())
        p1, p2 = _denied_access_patches()
        with (
            p1,
            p2,
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token"
            ) as mock_token,
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as mock_client_cls,
        ):
            response = client.get(f"/api/cicd/github/{_OWNER}/{_REPO}/runs")

        assert response.status_code == 200
        data = _parse_mcp_response(response.json())
        assert data["success"] is False
        assert "not found" in data["error"].lower()
        mock_token.assert_not_called()
        mock_client_cls.assert_not_called()

    def test_get_run_denied_before_token_resolution(self, app, client):
        _override_user(app, _make_user())
        p1, p2 = _denied_access_patches()
        with (
            p1,
            p2,
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token"
            ) as mock_token,
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as mock_client_cls,
        ):
            response = client.get(f"/api/cicd/github/{_OWNER}/{_REPO}/runs/{_RUN_ID}")

        data = _parse_mcp_response(response.json())
        assert data["success"] is False
        assert "not found" in data["error"].lower()
        mock_token.assert_not_called()
        mock_client_cls.assert_not_called()

    def test_search_logs_denied_before_token_resolution(self, app, client):
        _override_user(app, _make_user())
        p1, p2 = _denied_access_patches()
        with (
            p1,
            p2,
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token"
            ) as mock_token,
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as mock_client_cls,
        ):
            response = client.get(
                f"/api/cicd/github/{_OWNER}/{_REPO}/runs/{_RUN_ID}/logs",
                params={"query": "error"},
            )

        data = _parse_mcp_response(response.json())
        assert data["success"] is False
        assert "not found" in data["error"].lower()
        mock_token.assert_not_called()
        mock_client_cls.assert_not_called()

    def test_get_job_logs_denied_before_token_resolution(self, app, client):
        _override_user(app, _make_user())
        p1, p2 = _denied_access_patches()
        with (
            p1,
            p2,
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token"
            ) as mock_token,
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as mock_client_cls,
        ):
            response = client.get(
                f"/api/cicd/github/{_OWNER}/{_REPO}/jobs/{_JOB_ID}/logs"
            )

        data = _parse_mcp_response(response.json())
        assert data["success"] is False
        assert "not found" in data["error"].lower()
        mock_token.assert_not_called()
        mock_client_cls.assert_not_called()


class TestGhActionsMutatingRoutesEnforceGroupAccessAndPerUserToken:
    """retry_run and cancel_run must check group access AND must never use
    the global TokenAuthenticator token."""

    def test_retry_run_denied_before_any_credential_use(self, app, client):
        _override_user(app, _make_user())
        p1, p2 = _denied_access_patches()
        with (
            p1,
            p2,
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token"
            ) as mock_global_token,
            patch(
                "code_indexer.server.mcp.handlers._get_personal_credential_for_host"
            ) as mock_personal_cred,
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as mock_client_cls,
        ):
            response = client.post(
                f"/api/cicd/github/{_OWNER}/{_REPO}/runs/{_RUN_ID}/retry"
            )

        data = _parse_mcp_response(response.json())
        assert data["success"] is False
        assert "not found" in data["error"].lower()
        mock_global_token.assert_not_called()
        mock_personal_cred.assert_not_called()
        mock_client_cls.assert_not_called()

    def test_cancel_run_denied_before_any_credential_use(self, app, client):
        _override_user(app, _make_user())
        p1, p2 = _denied_access_patches()
        with (
            p1,
            p2,
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token"
            ) as mock_global_token,
            patch(
                "code_indexer.server.mcp.handlers._get_personal_credential_for_host"
            ) as mock_personal_cred,
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as mock_client_cls,
        ):
            response = client.post(
                f"/api/cicd/github/{_OWNER}/{_REPO}/runs/{_RUN_ID}/cancel"
            )

        data = _parse_mcp_response(response.json())
        assert data["success"] is False
        assert "not found" in data["error"].lower()
        mock_global_token.assert_not_called()
        mock_personal_cred.assert_not_called()
        mock_client_cls.assert_not_called()

    def test_retry_run_fails_closed_without_personal_credential_never_uses_global(
        self, app, client
    ):
        """An allowed user with NO personal PAT configured must be refused
        with a clear error -- never silently fall back to the global CI
        token, even though the global token IS available."""
        _override_user(app, _make_user())
        p1, p2 = _allowed_access_patches()
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
        ):
            response = client.post(
                f"/api/cicd/github/{_OWNER}/{_REPO}/runs/{_RUN_ID}/retry"
            )

        data = _parse_mcp_response(response.json())
        assert data["success"] is False
        assert "credential" in data["error"].lower()
        mock_global_token.assert_not_called()

    def test_cancel_run_uses_personal_pat_never_global_token(self, app, client):
        """An allowed user WITH a personal PAT succeeds using ONLY that
        PAT -- the global token resolver is never even consulted, and the
        outbound client is constructed with the personal token."""
        _override_user(app, _make_user())
        p1, p2 = _allowed_access_patches()
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
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as mock_client_cls,
        ):
            mock_instance = MagicMock()
            mock_instance.cancel_run = AsyncMock(return_value={"status": "cancelled"})
            mock_instance.last_rate_limit = None
            mock_client_cls.return_value = mock_instance

            response = client.post(
                f"/api/cicd/github/{_OWNER}/{_REPO}/runs/{_RUN_ID}/cancel"
            )

        data = _parse_mcp_response(response.json())
        assert data["success"] is True
        mock_global_token.assert_not_called()
        mock_client_cls.assert_called_once_with("personal_pat_xyz")

    def test_retry_run_uses_personal_pat_never_global_token(self, app, client):
        """retry_run happy-path with a personal PAT configured -- mirrors
        the cancel_run success test above for the sibling mutating route."""
        _override_user(app, _make_user())
        p1, p2 = _allowed_access_patches()
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
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as mock_client_cls,
        ):
            mock_instance = MagicMock()
            mock_instance.retry_run = AsyncMock(return_value={"status": "queued"})
            mock_instance.last_rate_limit = None
            mock_client_cls.return_value = mock_instance

            response = client.post(
                f"/api/cicd/github/{_OWNER}/{_REPO}/runs/{_RUN_ID}/retry"
            )

        data = _parse_mcp_response(response.json())
        assert data["success"] is True
        mock_global_token.assert_not_called()
        mock_client_cls.assert_called_once_with("personal_pat_xyz")


_READ_HANDLER_CASES = [
    pytest.param(
        f"/api/cicd/github/{_OWNER}/{_REPO}/runs",
        None,
        "list_runs",
        [],
        id="list_runs",
    ),
    pytest.param(
        f"/api/cicd/github/{_OWNER}/{_REPO}/runs/{_RUN_ID}",
        None,
        "get_run",
        {"id": _RUN_ID, "status": "completed"},
        id="get_run",
    ),
    pytest.param(
        f"/api/cicd/github/{_OWNER}/{_REPO}/runs/{_RUN_ID}/logs",
        {"query": "error"},
        "search_logs",
        [],
        id="search_logs",
    ),
    pytest.param(
        f"/api/cicd/github/{_OWNER}/{_REPO}/jobs/{_JOB_ID}/logs",
        None,
        "get_job_logs",
        "log text",
        id="get_job_logs",
    ),
]


class TestGhActionsReadRoutesUnregisteredRepoNeverUsesGlobalToken:
    """{owner}/{repo} that does not match ANY registered CIDX golden repo
    is an "unregistered" (ad-hoc) target. Reads against it must use the
    caller's OWN personal PAT -- refused when absent -- and must NEVER
    silently fall back to the server-wide global CI token, even though the
    global token IS configured and reachable. Parametrized over all 4 read
    handlers (list_runs, get_run, search_logs, get_job_logs) so each one is
    independently proven to route through this check."""

    def _unregistered_registry_patch(self):
        return patch(
            "code_indexer.server.mcp.handlers._list_global_repos",
            return_value=[],
        )

    @pytest.mark.parametrize(
        "path,params,method_name,return_value", _READ_HANDLER_CASES
    )
    def test_unregistered_without_personal_pat_is_refused(
        self, app, client, path, params, method_name, return_value
    ):
        _override_user(app, _make_user())
        with (
            self._unregistered_registry_patch(),
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
                return_value="GLOBAL_TOKEN_MUST_NEVER_BE_USED",
            ) as mock_global_token,
            patch(
                "code_indexer.server.mcp.handlers._get_personal_credential_for_host",
                return_value=None,
            ),
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as mock_client_cls,
        ):
            response = client.get(path, params=params)

        data = _parse_mcp_response(response.json())
        assert data["success"] is False
        assert "credential" in data["error"].lower()
        mock_global_token.assert_not_called()
        mock_client_cls.assert_not_called()

    @pytest.mark.parametrize(
        "path,params,method_name,return_value", _READ_HANDLER_CASES
    )
    def test_unregistered_with_personal_pat_uses_pat_never_global(
        self, app, client, path, params, method_name, return_value
    ):
        _override_user(app, _make_user())
        with (
            self._unregistered_registry_patch(),
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
                return_value="GLOBAL_TOKEN_MUST_NEVER_BE_USED",
            ) as mock_global_token,
            patch(
                "code_indexer.server.mcp.handlers._get_personal_credential_for_host",
                return_value={"token": "personal_pat_xyz"},
            ),
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as mock_client_cls,
        ):
            mock_instance = MagicMock()
            setattr(mock_instance, method_name, AsyncMock(return_value=return_value))
            mock_instance.last_rate_limit = None
            mock_client_cls.return_value = mock_instance

            response = client.get(path, params=params)

        data = _parse_mcp_response(response.json())
        assert data["success"] is True
        mock_global_token.assert_not_called()
        mock_client_cls.assert_called_once_with("personal_pat_xyz")

    @pytest.mark.parametrize(
        "path,params,method_name,return_value", _READ_HANDLER_CASES
    )
    def test_registered_and_allowed_uses_global_token(
        self, app, client, path, params, method_name, return_value
    ):
        """Contrast case: a repo that IS a registered + allowed golden repo
        is permitted to use the global CI token (unlike the unregistered
        case above)."""
        _override_user(app, _make_user())
        p1, p2 = _allowed_access_patches()
        with (
            p1,
            p2,
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
                return_value="global_gh_token",
            ) as mock_global_token,
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as mock_client_cls,
        ):
            mock_instance = MagicMock()
            setattr(mock_instance, method_name, AsyncMock(return_value=return_value))
            mock_instance.last_rate_limit = None
            mock_client_cls.return_value = mock_instance

            response = client.get(path, params=params)

        data = _parse_mcp_response(response.json())
        assert data["success"] is True
        mock_global_token.assert_called_once()
        mock_client_cls.assert_called_once_with("global_gh_token")


class TestSharedHelperExtractsProjectPathFromAllCloneUrlForms:
    """The clone-URL matcher inside _resolve_cicd_project_access (used by
    GitHub, GitLab, and the unified ci_* handlers alike) must correctly
    match ssh://, scp-style (git@host:owner/repo), and https registry
    entries, so that a registered-and-denied repository is consistently
    identified and denied regardless of which clone-URL form the registry
    stores for it."""

    def _assert_denied_for_registry_url(self, app, client, repo_url):
        _override_user(app, _make_user())
        access_svc = MagicMock()
        access_svc.get_accessible_repos.return_value = {"some-other-repo"}
        with (
            patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=_registry_with_repo(_GOLDEN_ALIAS, repo_url),
            ),
            patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_svc,
            ),
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token"
            ) as mock_token,
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as mock_client_cls,
        ):
            response = client.get(f"/api/cicd/github/{_OWNER}/{_REPO}/runs")

        data = _parse_mcp_response(response.json())
        assert data["success"] is False
        assert "not found" in data["error"].lower()
        mock_token.assert_not_called()
        mock_client_cls.assert_not_called()

    def test_ssh_scheme_registry_url_is_matched_and_denied(self, app, client):
        self._assert_denied_for_registry_url(
            app, client, f"ssh://git@github.com/{_OWNER}/{_REPO}.git"
        )

    def test_scp_style_registry_url_is_matched_and_denied(self, app, client):
        self._assert_denied_for_registry_url(
            app, client, f"git@github.com:{_OWNER}/{_REPO}.git"
        )

    def test_https_registry_url_is_matched_and_denied(self, app, client):
        self._assert_denied_for_registry_url(app, client, _GITHUB_REPO_URL)


class TestSharedHelperFailsClosedOnRegistryLoadFailure:
    """A golden-repo registry load failure denies access before any
    credential is resolved."""

    def test_registry_load_failure_denies_access(self, app, client):
        _override_user(app, _make_user())
        with (
            patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                side_effect=RuntimeError("registry unavailable"),
            ),
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token"
            ) as mock_token,
            patch(
                "code_indexer.server.mcp.handlers._get_personal_credential_for_host"
            ) as mock_personal_cred,
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as mock_client_cls,
        ):
            response = client.get(f"/api/cicd/github/{_OWNER}/{_REPO}/runs")

        data = _parse_mcp_response(response.json())
        assert data["success"] is False
        mock_token.assert_not_called()
        mock_personal_cred.assert_not_called()
        mock_client_cls.assert_not_called()


class TestResolveCicdWriteTokenMissingTokenKey:
    """A personal-credential record that exists but lacks a 'token' field
    must be treated as 'no credential configured' (an error tuple), never
    as silent success with token=None handed to the outbound client."""

    def test_credential_dict_without_token_key_is_treated_as_missing(self):
        user = _make_user()
        with patch(
            "code_indexer.server.mcp.handlers._get_personal_credential_for_host",
            return_value={"username": "bob"},  # credential exists, no "token" key
        ):
            token, error = _resolve_cicd_write_token("github", user, "github.com")

        assert token is None
        assert error is not None
        assert "credential" in error.lower()


class TestForgeHostNeverClientControlledForGitHubMutations:
    """retry_run and cancel_run must derive forge_host as "github.com"
    unconditionally -- GitHubActionsClient is hardwired to api.github.com,
    so honoring a caller-supplied args["base_url"] would look up (or
    store) the caller's personal PAT under a host other than the one the
    outbound call actually targets."""

    def _allowed_access_and_client_patches(self):
        access_svc = MagicMock()
        access_svc.get_accessible_repos.return_value = {_REPO}
        return (
            patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=_registry_with_repo(_GOLDEN_ALIAS, _GITHUB_REPO_URL),
            ),
            patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_svc,
            ),
        )

    @pytest.mark.asyncio
    async def test_retry_run_ignores_client_supplied_base_url(self):
        user = _make_user()
        args = {
            "repository": f"{_OWNER}/{_REPO}",
            "run_id": _RUN_ID,
            "base_url": "https://other-host.example.com",
        }
        p1, p2 = self._allowed_access_and_client_patches()
        with (
            p1,
            p2,
            patch(
                "code_indexer.server.mcp.handlers._get_personal_credential_for_host",
                return_value={"token": "personal_pat_xyz"},
            ) as mock_get_cred,
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as mock_client_cls,
        ):
            mock_instance = MagicMock()
            mock_instance.retry_run = AsyncMock(return_value={"status": "queued"})
            mock_instance.last_rate_limit = None
            mock_client_cls.return_value = mock_instance

            await handle_gh_actions_retry_run(args, user)

        mock_get_cred.assert_called_once_with("bob", "github.com")

    @pytest.mark.asyncio
    async def test_cancel_run_ignores_client_supplied_base_url(self):
        user = _make_user()
        args = {
            "repository": f"{_OWNER}/{_REPO}",
            "run_id": _RUN_ID,
            "base_url": "https://other-host.example.com",
        }
        p1, p2 = self._allowed_access_and_client_patches()
        with (
            p1,
            p2,
            patch(
                "code_indexer.server.mcp.handlers._get_personal_credential_for_host",
                return_value={"token": "personal_pat_xyz"},
            ) as mock_get_cred,
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as mock_client_cls,
        ):
            mock_instance = MagicMock()
            mock_instance.cancel_run = AsyncMock(return_value={"status": "cancelled"})
            mock_instance.last_rate_limit = None
            mock_client_cls.return_value = mock_instance

            await handle_gh_actions_cancel_run(args, user)

        mock_get_cred.assert_called_once_with("bob", "github.com")
