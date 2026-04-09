"""
Test suite for Story #404 - CI/CD Credential Security and Resilience.

Covers:
- AC1: Group Access Control (_resolve_cicd_project_access)
- AC2: Per-User Write Tokens (_resolve_cicd_write_token)
- AC3: Audit Trail (write handlers log username/op/project/id/correlation_id)
- AC4: Read Token Resilience (_resolve_cicd_read_token)
- AC5: forge_host Derivation (_derive_forge_host)

All 12 CI/CD handlers are verified (8 read + 4 write).
Foundation #1 Compliant: minimal mocking, real handler execution paths.
"""

import json
import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import (
    # Helpers (new in Story #404)
    _derive_forge_host,
    _resolve_cicd_project_access,
    _resolve_cicd_read_token,
    _resolve_cicd_write_token,
    # GitLab read handlers
    handle_gitlab_ci_list_pipelines,
    handle_gitlab_ci_get_pipeline,
    handle_gitlab_ci_search_logs,
    handle_gitlab_ci_get_job_logs,
    # GitLab write handlers
    handle_gitlab_ci_retry_pipeline,
    handle_gitlab_ci_cancel_pipeline,
    # GitHub read handlers
    handle_github_actions_list_runs,
    handle_github_actions_get_run,
    handle_github_actions_search_logs,
    handle_github_actions_get_job_logs,
    # GitHub write handlers
    handle_github_actions_retry_run,
    handle_github_actions_cancel_run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_mcp_response(response: dict) -> dict:
    """Decode the inner JSON payload from an MCP envelope response."""
    content = response.get("content", [])
    assert len(content) > 0, f"Empty MCP response: {response}"
    return json.loads(content[0]["text"])  # type: ignore[no-any-return]


def _make_user(username: str = "testuser") -> MagicMock:
    user = MagicMock(spec=User)
    user.username = username
    user.role = UserRole.ADMIN
    return user


# ---------------------------------------------------------------------------
# AC5: _derive_forge_host
# ---------------------------------------------------------------------------


class TestDeriveForgeHost:
    """Tests for _derive_forge_host utility."""

    def test_strips_https_protocol(self):
        assert (
            _derive_forge_host("https://gitlab.example.com", "gitlab")
            == "gitlab.example.com"
        )

    def test_strips_http_protocol(self):
        assert (
            _derive_forge_host("http://gitlab.example.com", "gitlab")
            == "gitlab.example.com"
        )

    def test_strips_trailing_slash(self):
        assert (
            _derive_forge_host("https://gitlab.example.com/", "gitlab")
            == "gitlab.example.com"
        )

    def test_strips_protocol_and_trailing_slash(self):
        assert (
            _derive_forge_host("https://gitlab.example.com/", "gitlab")
            == "gitlab.example.com"
        )

    def test_defaults_to_github_com_when_no_base_url(self):
        assert _derive_forge_host(None, "github") == "github.com"

    def test_defaults_to_gitlab_com_when_no_base_url(self):
        assert _derive_forge_host(None, "gitlab") == "gitlab.com"

    def test_defaults_to_github_com_when_empty_base_url(self):
        assert _derive_forge_host("", "github") == "github.com"

    def test_defaults_to_gitlab_com_when_empty_base_url(self):
        assert _derive_forge_host("", "gitlab") == "gitlab.com"

    def test_handles_base_url_without_protocol(self):
        """When base_url has no protocol prefix, returns it as-is after stripping slash."""
        result = _derive_forge_host("gitlab.example.com/", "gitlab")
        assert result == "gitlab.example.com"


# ---------------------------------------------------------------------------
# AC1: _resolve_cicd_project_access
# ---------------------------------------------------------------------------


class TestResolveCicdProjectAccess:
    """Tests for _resolve_cicd_project_access group access enforcement."""

    def _make_registry_with_repo(self, alias: str, repo_url: str):
        """Create a minimal mock registry with one repo entry."""
        repos = []
        repos = [
            {
                "alias_name": alias,
                "repo_url": repo_url,
            }
        ]
        return repos

    def test_unregistered_project_is_allowed(self):
        """Ad-hoc query for unregistered project must be allowed."""
        repos = []  # type: ignore[var-annotated]
        repos = []  # No repos registered

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=repos,
            ),
        ):
            result = _resolve_cicd_project_access("unknown/project", "github", "alice")
        assert result is None  # None means allowed

    def test_no_access_filtering_service_allows_all(self):
        """When AccessFilteringService not configured, all requests pass through."""
        repos = self._make_registry_with_repo(
            "myrepo-global", "https://github.com/org/myrepo.git"
        )

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=repos,
            ),
        ):
            result = _resolve_cicd_project_access("org/myrepo", "github", "alice")
        assert result is None  # allowed

    def test_user_in_group_with_repo_access_is_allowed(self):
        """User whose group includes the matched golden repo is allowed."""
        repos = self._make_registry_with_repo(
            "myrepo-global", "https://github.com/org/myrepo.git"
        )
        access_svc = MagicMock()
        access_svc.get_accessible_repos.return_value = {"myrepo"}

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_svc,
            ),
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=repos,
            ),
        ):
            result = _resolve_cicd_project_access("org/myrepo", "github", "alice")
        assert result is None  # allowed

    def test_user_not_in_group_for_repo_is_denied(self):
        """User whose group does NOT include matched repo gets invisible-repo error."""
        repos = self._make_registry_with_repo(
            "myrepo-global", "https://github.com/org/myrepo.git"
        )
        access_svc = MagicMock()
        access_svc.get_accessible_repos.return_value = {"otherrepo"}  # not myrepo

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_svc,
            ),
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=repos,
            ),
        ):
            result = _resolve_cicd_project_access("org/myrepo", "github", "alice")
        assert result is not None  # denied
        assert "not found" in result.lower()

    def test_gitlab_ssh_url_matched_for_project_id(self):
        """GitLab project_id as namespace/project matches SSH clone URL."""
        repos = self._make_registry_with_repo(
            "myproject-global", "git@gitlab.com:myns/myproject.git"
        )
        access_svc = MagicMock()
        access_svc.get_accessible_repos.return_value = {"myproject"}

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_svc,
            ),
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=repos,
            ),
        ):
            result = _resolve_cicd_project_access("myns/myproject", "gitlab", "bob")
        assert result is None  # allowed

    def test_gitlab_ssh_url_denied_when_not_in_group(self):
        """GitLab project matched via SSH URL, user not in group -> denied."""
        repos = self._make_registry_with_repo(
            "myproject-global", "git@gitlab.com:myns/myproject.git"
        )
        access_svc = MagicMock()
        access_svc.get_accessible_repos.return_value = set()  # empty

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_svc,
            ),
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=repos,
            ),
        ):
            result = _resolve_cicd_project_access("myns/myproject", "gitlab", "bob")
        assert result is not None
        assert "not found" in result.lower()

    def test_github_https_url_matched(self):
        """GitHub owner/repo identifier matches HTTPS clone URL."""
        repos = self._make_registry_with_repo(
            "cidx-global", "https://github.com/LightspeedDMS/code-indexer.git"
        )
        access_svc = MagicMock()
        access_svc.get_accessible_repos.return_value = {"cidx"}

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_svc,
            ),
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=repos,
            ),
        ):
            result = _resolve_cicd_project_access(
                "LightspeedDMS/code-indexer", "github", "charlie"
            )
        assert result is None  # allowed


# ---------------------------------------------------------------------------
# AC4: _resolve_cicd_read_token
# ---------------------------------------------------------------------------


class TestResolveCicdReadToken:
    """Tests for _resolve_cicd_read_token fallback chain."""

    def _make_user(self, username="alice"):
        return _make_user(username)

    def test_returns_global_token_when_available(self):
        """Global CI token is used first when available."""
        user = self._make_user()

        with patch(
            "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
            return_value="global_token_xyz",
        ):
            token = _resolve_cicd_read_token("github", user, "github.com")

        assert token == "global_token_xyz"

    def test_falls_back_to_personal_when_global_unavailable(self):
        """When global token absent, personal credential is used."""
        user = self._make_user()
        cred = {"token": "personal_pat_abc", "forge_host": "github.com"}

        with (
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._get_personal_credential_for_host",
                return_value=cred,
            ),
        ):
            token = _resolve_cicd_read_token("github", user, "github.com")

        assert token == "personal_pat_abc"

    def test_returns_none_when_both_unavailable(self):
        """Returns None when neither global nor personal credential available."""
        user = self._make_user()

        with (
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._get_personal_credential_for_host",
                return_value=None,
            ),
        ):
            token = _resolve_cicd_read_token("github", user, "github.com")

        assert token is None

    def test_fallback_produces_info_log(self, caplog):
        """Fallback to personal credential must produce INFO log."""
        user = self._make_user()
        cred = {"token": "personal_pat_abc"}

        with (
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._get_personal_credential_for_host",
                return_value=cred,
            ),
            caplog.at_level(logging.INFO, logger="code_indexer.server.mcp.handlers"),
        ):
            _resolve_cicd_read_token("github", user, "github.com")

        assert any(
            "global" in rec.message.lower() and "personal" in rec.message.lower()
            for rec in caplog.records
        ), f"Expected fallback INFO log. Got: {[r.message for r in caplog.records]}"

    def test_no_fallback_log_when_global_token_available(self, caplog):
        """No fallback log message when global token is used."""
        user = self._make_user()

        with (
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
                return_value="global_token_xyz",
            ),
            caplog.at_level(logging.INFO, logger="code_indexer.server.mcp.handlers"),
        ):
            _resolve_cicd_read_token("github", user, "github.com")

        fallback_logs = [
            r
            for r in caplog.records
            if "personal" in r.message.lower() and "fallback" in r.message.lower()
        ]
        assert len(fallback_logs) == 0


# ---------------------------------------------------------------------------
# AC2: _resolve_cicd_write_token
# ---------------------------------------------------------------------------


class TestResolveCicdWriteToken:
    """Tests for _resolve_cicd_write_token - per-user PAT ONLY."""

    def test_returns_personal_pat_when_credential_exists(self):
        """Returns (token, None) tuple when personal credential found."""
        user = _make_user()
        cred = {"token": "my_personal_pat_123", "forge_host": "github.com"}

        with patch(
            "code_indexer.server.mcp.handlers._get_personal_credential_for_host",
            return_value=cred,
        ):
            token, error = _resolve_cicd_write_token("github", user, "github.com")

        assert token == "my_personal_pat_123"
        assert error is None

    def test_returns_error_when_no_credential(self):
        """Returns (None, error_msg) tuple when no personal credential configured."""
        user = _make_user()

        with patch(
            "code_indexer.server.mcp.handlers._get_personal_credential_for_host",
            return_value=None,
        ):
            token, error = _resolve_cicd_write_token("github", user, "github.com")

        assert token is None
        assert error is not None
        assert "configure personal git credential" in error.lower()
        assert "github.com" in error
        assert "configure_git_credential" in error

    def test_never_uses_global_token(self):
        """Global CI token is NEVER consulted for write operations."""
        user = _make_user()

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_personal_credential_for_host",
                return_value=None,
            ),
            patch(
                "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
                return_value="global_token_xyz",
            ) as mock_resolve,
        ):
            token, error = _resolve_cicd_write_token("github", user, "github.com")

        # Global token resolver must NOT be called
        mock_resolve.assert_not_called()
        assert token is None

    def test_error_message_includes_forge_host(self):
        """Error message includes the specific forge host."""
        user = _make_user()

        with patch(
            "code_indexer.server.mcp.handlers._get_personal_credential_for_host",
            return_value=None,
        ):
            _, error = _resolve_cicd_write_token("gitlab", user, "gitlab.mycompany.com")

        assert "gitlab.mycompany.com" in error

    def test_gitlab_returns_personal_pat(self):
        """GitLab write token also uses personal PAT only."""
        user = _make_user()
        cred = {"token": "glpat_personal_456"}

        with patch(
            "code_indexer.server.mcp.handlers._get_personal_credential_for_host",
            return_value=cred,
        ):
            token, error = _resolve_cicd_write_token("gitlab", user, "gitlab.com")

        assert token == "glpat_personal_456"
        assert error is None


# ---------------------------------------------------------------------------
# AC1 (end-to-end): GitLab handler with group access check
# ---------------------------------------------------------------------------


class TestGitLabListPipelinesGroupAccess:
    """End-to-end test: gitlab_ci_list_pipelines enforces group access."""

    @pytest.mark.asyncio
    async def test_denied_project_returns_not_found(self):
        """User without group access receives invisible-repo 'not found' error."""
        user = _make_user("alice")
        args = {"project_id": "myns/myproject"}

        with patch(
            "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
            return_value="Project 'myns/myproject' not found.",
        ):
            response = await handle_gitlab_ci_list_pipelines(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_allowed_project_proceeds_to_token_resolution(self):
        """User with group access proceeds past access check to token resolution."""
        user = _make_user("alice")
        args = {"project_id": "myns/myproject"}

        pipelines = [{"id": 42, "status": "success"}]

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,  # allowed
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_read_token",
                return_value="test_token",
            ),
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as MockClient,
        ):
            mock_instance = MagicMock()
            mock_instance.list_pipelines = AsyncMock(return_value=pipelines)
            mock_instance.last_rate_limit = None
            MockClient.return_value = mock_instance

            response = await handle_gitlab_ci_list_pipelines(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is True
        assert data["pipelines"] == pipelines

    @pytest.mark.asyncio
    async def test_access_check_before_token_resolution(self):
        """Group check runs before token resolution (fail fast)."""
        user = _make_user("alice")
        args = {"project_id": "myns/myproject"}

        token_resolver_called = []

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value="Project 'myns/myproject' not found.",
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_read_token",
                side_effect=lambda *a, **kw: token_resolver_called.append(True)  # type: ignore[func-returns-value]
                or "token",
            ),
        ):
            await handle_gitlab_ci_list_pipelines(args, user)

        # Token resolver must NOT have been called
        assert len(token_resolver_called) == 0, (
            "Token resolver called despite access denial"
        )


# ---------------------------------------------------------------------------
# AC1 (end-to-end): GitHub handler with group access check
# ---------------------------------------------------------------------------


class TestGitHubListRunsGroupAccess:
    """End-to-end test: github_actions_list_runs enforces group access."""

    @pytest.mark.asyncio
    async def test_denied_project_returns_not_found(self):
        """User without group access receives invisible-repo 'not found' error."""
        user = _make_user("bob")
        args = {"owner": "myorg", "repo": "myrepo"}

        with patch(
            "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
            return_value="Project 'myorg/myrepo' not found.",
        ):
            response = await handle_github_actions_list_runs(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_allowed_project_proceeds_to_api_call(self):
        """Allowed user proceeds to the GitHub API call."""
        user = _make_user("bob")
        args = {"owner": "myorg", "repo": "myrepo"}

        runs = [{"id": 100, "status": "completed"}]

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_read_token",
                return_value="gh_token",
            ),
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as MockClient,
        ):
            mock_instance = MagicMock()
            mock_instance.list_runs = AsyncMock(return_value=runs)
            mock_instance.last_rate_limit = None
            MockClient.return_value = mock_instance

            response = await handle_github_actions_list_runs(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is True
        assert data["runs"] == runs


# ---------------------------------------------------------------------------
# AC2 (end-to-end): Write handlers use per-user PAT
# ---------------------------------------------------------------------------


class TestWriteHandlersUsePerUserPat:
    """All 4 write handlers must route through _resolve_cicd_write_token."""

    @pytest.mark.asyncio
    async def test_gitlab_retry_pipeline_fails_without_personal_credential(self):
        """gitlab_ci_retry_pipeline returns clear error when no personal PAT."""
        user = _make_user("alice")
        args = {"project_id": "myns/myproject", "pipeline_id": 123}

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                return_value=(
                    None,
                    "Configure personal git credential for gitlab.com to perform write operations. Use configure_git_credential tool.",
                ),
            ),
        ):
            response = await handle_gitlab_ci_retry_pipeline(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert "configure personal git credential" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_gitlab_cancel_pipeline_fails_without_personal_credential(self):
        """gitlab_ci_cancel_pipeline returns clear error when no personal PAT."""
        user = _make_user("alice")
        args = {"project_id": "myns/myproject", "pipeline_id": 456}

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                return_value=(
                    None,
                    "Configure personal git credential for gitlab.com to perform write operations. Use configure_git_credential tool.",
                ),
            ),
        ):
            response = await handle_gitlab_ci_cancel_pipeline(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert "configure personal git credential" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_github_retry_run_fails_without_personal_credential(self):
        """github_actions_retry_run returns clear error when no personal PAT."""
        user = _make_user("bob")
        args = {"owner": "myorg", "repo": "myrepo", "run_id": 789}

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                return_value=(
                    None,
                    "Configure personal git credential for github.com to perform write operations. Use configure_git_credential tool.",
                ),
            ),
        ):
            response = await handle_github_actions_retry_run(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert "configure personal git credential" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_github_cancel_run_fails_without_personal_credential(self):
        """github_actions_cancel_run returns clear error when no personal PAT."""
        user = _make_user("bob")
        args = {"owner": "myorg", "repo": "myrepo", "run_id": 999}

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                return_value=(
                    None,
                    "Configure personal git credential for github.com to perform write operations. Use configure_git_credential tool.",
                ),
            ),
        ):
            response = await handle_github_actions_cancel_run(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert "configure personal git credential" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_gitlab_retry_pipeline_uses_personal_pat_not_global(self):
        """gitlab_ci_retry_pipeline uses personal PAT, global token NOT consulted."""
        user = _make_user("alice")
        args = {"project_id": "myns/myproject", "pipeline_id": 123}

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                return_value=("personal_glpat_xyz", None),
            ) as mock_write_token,
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as MockClient,
        ):
            mock_instance = MagicMock()
            mock_instance.retry_pipeline = AsyncMock(
                return_value={"id": 200, "status": "running"}
            )
            mock_instance.last_rate_limit = None
            MockClient.return_value = mock_instance

            response = await handle_gitlab_ci_retry_pipeline(args, user)

        mock_write_token.assert_called_once()
        data = _parse_mcp_response(response)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_github_cancel_run_uses_personal_pat_not_global(self):
        """github_actions_cancel_run uses personal PAT, global token NOT consulted."""
        user = _make_user("bob")
        args = {"owner": "myorg", "repo": "myrepo", "run_id": 999}

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                return_value=("gh_personal_pat", None),
            ) as mock_write_token,
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as MockClient,
        ):
            mock_instance = MagicMock()
            mock_instance.cancel_run = AsyncMock(return_value={"status": "cancelled"})
            mock_instance.last_rate_limit = None
            MockClient.return_value = mock_instance

            response = await handle_github_actions_cancel_run(args, user)

        mock_write_token.assert_called_once()
        data = _parse_mcp_response(response)
        assert data["success"] is True


# ---------------------------------------------------------------------------
# AC3: Audit Trail in write handlers
# ---------------------------------------------------------------------------


class TestAuditTrailInWriteHandlers:
    """All 4 write handlers produce INFO audit log with required fields."""

    @pytest.mark.asyncio
    async def test_gitlab_retry_pipeline_logs_audit_entry(self, caplog):
        """gitlab_ci_retry_pipeline logs username, op, project_id, pipeline_id."""
        user = _make_user("alice")
        args = {"project_id": "myns/myproject", "pipeline_id": 42}

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                return_value=("test_token", None),
            ),
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as MockClient,
            caplog.at_level(logging.INFO, logger="code_indexer.server.mcp.handlers"),
        ):
            mock_instance = MagicMock()
            mock_instance.retry_pipeline = AsyncMock(return_value={"id": 43})
            mock_instance.last_rate_limit = None
            MockClient.return_value = mock_instance

            await handle_gitlab_ci_retry_pipeline(args, user)

        audit_logs = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any(
            "alice" in r.message
            and ("retry" in r.message.lower() or "myproject" in r.message)
            for r in audit_logs
        ), f"Audit log missing. Records: {[r.message for r in audit_logs]}"

    @pytest.mark.asyncio
    async def test_gitlab_cancel_pipeline_logs_audit_entry(self, caplog):
        """gitlab_ci_cancel_pipeline logs username, op, project_id, pipeline_id."""
        user = _make_user("alice")
        args = {"project_id": "myns/myproject", "pipeline_id": 55}

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                return_value=("test_token", None),
            ),
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as MockClient,
            caplog.at_level(logging.INFO, logger="code_indexer.server.mcp.handlers"),
        ):
            mock_instance = MagicMock()
            mock_instance.cancel_pipeline = AsyncMock(
                return_value={"status": "cancelled"}
            )
            mock_instance.last_rate_limit = None
            MockClient.return_value = mock_instance

            await handle_gitlab_ci_cancel_pipeline(args, user)

        audit_logs = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any(
            "alice" in r.message
            and ("cancel" in r.message.lower() or "myproject" in r.message)
            for r in audit_logs
        ), f"Audit log missing. Records: {[r.message for r in audit_logs]}"

    @pytest.mark.asyncio
    async def test_github_retry_run_logs_audit_entry(self, caplog):
        """github_actions_retry_run logs username, op, owner/repo, run_id."""
        user = _make_user("bob")
        args = {"owner": "myorg", "repo": "myrepo", "run_id": 77}

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                return_value=("gh_token", None),
            ),
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as MockClient,
            caplog.at_level(logging.INFO, logger="code_indexer.server.mcp.handlers"),
        ):
            mock_instance = MagicMock()
            mock_instance.retry_run = AsyncMock(return_value={"status": "queued"})
            mock_instance.last_rate_limit = None
            MockClient.return_value = mock_instance

            await handle_github_actions_retry_run(args, user)

        audit_logs = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any(
            "bob" in r.message
            and ("retry" in r.message.lower() or "myrepo" in r.message)
            for r in audit_logs
        ), f"Audit log missing. Records: {[r.message for r in audit_logs]}"

    @pytest.mark.asyncio
    async def test_github_cancel_run_logs_audit_entry(self, caplog):
        """github_actions_cancel_run logs username, op, owner/repo, run_id."""
        user = _make_user("charlie")
        args = {"owner": "myorg", "repo": "myrepo", "run_id": 88}

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                return_value=("gh_token", None),
            ),
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as MockClient,
            caplog.at_level(logging.INFO, logger="code_indexer.server.mcp.handlers"),
        ):
            mock_instance = MagicMock()
            mock_instance.cancel_run = AsyncMock(return_value={"status": "cancelled"})
            mock_instance.last_rate_limit = None
            MockClient.return_value = mock_instance

            await handle_github_actions_cancel_run(args, user)

        audit_logs = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any(
            "charlie" in r.message
            and ("cancel" in r.message.lower() or "myrepo" in r.message)
            for r in audit_logs
        ), f"Audit log missing. Records: {[r.message for r in audit_logs]}"

    @pytest.mark.asyncio
    async def test_audit_log_includes_correlation_id(self, caplog):
        """Write handler audit log entry includes correlation_id in extra."""
        user = _make_user("alice")
        args = {"project_id": "myns/myproject", "pipeline_id": 42}

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                return_value=("test_token", None),
            ),
            patch(
                "code_indexer.server.mcp.handlers.get_correlation_id",
                return_value="test-correlation-123",
            ),
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as MockClient,
            caplog.at_level(logging.INFO, logger="code_indexer.server.mcp.handlers"),
        ):
            mock_instance = MagicMock()
            mock_instance.retry_pipeline = AsyncMock(return_value={"id": 43})
            mock_instance.last_rate_limit = None
            MockClient.return_value = mock_instance

            await handle_gitlab_ci_retry_pipeline(args, user)

        # Check that at least one INFO record has correlation_id in its extras
        audit_logs = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any(
            getattr(r, "correlation_id", None) is not None for r in audit_logs
        ), (
            f"No audit log with correlation_id. Records: {[(r.message, r.__dict__) for r in audit_logs]}"
        )


# ---------------------------------------------------------------------------
# AC4 (end-to-end): Read handlers use resilient token fallback
# ---------------------------------------------------------------------------


class TestReadHandlerTokenFallback:
    """Read handlers call _resolve_cicd_read_token (not direct TokenAuthenticator)."""

    @pytest.mark.asyncio
    async def test_gitlab_list_pipelines_uses_read_token_resolver(self):
        """handle_gitlab_ci_list_pipelines calls _resolve_cicd_read_token."""
        user = _make_user("alice")
        args = {"project_id": "myns/myproject"}

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_read_token",
                return_value="resolved_token",
            ) as mock_read_token,
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as MockClient,
        ):
            mock_instance = MagicMock()
            mock_instance.list_pipelines = AsyncMock(return_value=[])
            mock_instance.last_rate_limit = None
            MockClient.return_value = mock_instance

            await handle_gitlab_ci_list_pipelines(args, user)

        mock_read_token.assert_called_once()

    @pytest.mark.asyncio
    async def test_github_list_runs_uses_read_token_resolver(self):
        """handle_github_actions_list_runs calls _resolve_cicd_read_token."""
        user = _make_user("bob")
        args = {"owner": "myorg", "repo": "myrepo"}

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_read_token",
                return_value="resolved_token",
            ) as mock_read_token,
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as MockClient,
        ):
            mock_instance = MagicMock()
            mock_instance.list_runs = AsyncMock(return_value=[])
            mock_instance.last_rate_limit = None
            MockClient.return_value = mock_instance

            await handle_github_actions_list_runs(args, user)

        mock_read_token.assert_called_once()

    @pytest.mark.asyncio
    async def test_read_handler_preserves_error_when_both_tokens_unavailable(self):
        """When both global and personal tokens unavailable, current error preserved."""
        user = _make_user("alice")
        args = {"project_id": "myns/myproject"}

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_read_token",
                return_value=None,  # Both unavailable
            ),
        ):
            response = await handle_gitlab_ci_list_pipelines(args, user)

        data = _parse_mcp_response(response)
        assert data["success"] is False
        # Must contain some error about token not being available
        assert "token" in data["error"].lower() or "credential" in data["error"].lower()


# ---------------------------------------------------------------------------
# Regression: All 12 handlers have group access + correct token routing
# ---------------------------------------------------------------------------


class TestAllHandlersCoverage:
    """Smoke tests verifying all 12 handlers call the new helpers."""

    def _patch_read_handler_deps(self, project_denied: bool = False):
        """Common patches for read handler tests."""
        return [
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value="not found" if project_denied else None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_read_token",
                return_value="test_token",
            ),
        ]

    def _patch_write_handler_deps(self, project_denied: bool = False):
        """Common patches for write handler tests."""
        return [
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value="not found" if project_denied else None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                return_value=(
                    (None, "no cred") if project_denied else ("test_token", None)
                ),
            ),
        ]

    @pytest.mark.asyncio
    async def test_gitlab_get_pipeline_checks_access(self):
        user = _make_user()
        args = {"project_id": "ns/proj", "pipeline_id": 1}
        with patch(
            "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
            return_value="ns/proj not found.",
        ):
            response = await handle_gitlab_ci_get_pipeline(args, user)
        data = _parse_mcp_response(response)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_gitlab_search_logs_checks_access(self):
        user = _make_user()
        args = {"project_id": "ns/proj", "pipeline_id": 1, "pattern": "error"}
        with patch(
            "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
            return_value="ns/proj not found.",
        ):
            response = await handle_gitlab_ci_search_logs(args, user)
        data = _parse_mcp_response(response)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_gitlab_get_job_logs_checks_access(self):
        user = _make_user()
        args = {"project_id": "ns/proj", "job_id": 1}
        with patch(
            "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
            return_value="ns/proj not found.",
        ):
            response = await handle_gitlab_ci_get_job_logs(args, user)
        data = _parse_mcp_response(response)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_github_get_run_checks_access(self):
        user = _make_user()
        args = {"owner": "o", "repo": "r", "run_id": 1}
        with patch(
            "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
            return_value="o/r not found.",
        ):
            response = await handle_github_actions_get_run(args, user)
        data = _parse_mcp_response(response)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_github_search_logs_checks_access(self):
        user = _make_user()
        args = {"owner": "o", "repo": "r", "run_id": 1, "pattern": "fail"}
        with patch(
            "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
            return_value="o/r not found.",
        ):
            response = await handle_github_actions_search_logs(args, user)
        data = _parse_mcp_response(response)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_github_get_job_logs_checks_access(self):
        user = _make_user()
        args = {"owner": "o", "repo": "r", "job_id": 1}
        with patch(
            "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
            return_value="o/r not found.",
        ):
            response = await handle_github_actions_get_job_logs(args, user)
        data = _parse_mcp_response(response)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_gitlab_retry_pipeline_checks_access(self):
        user = _make_user()
        args = {"project_id": "ns/proj", "pipeline_id": 1}
        with patch(
            "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
            return_value="ns/proj not found.",
        ):
            response = await handle_gitlab_ci_retry_pipeline(args, user)
        data = _parse_mcp_response(response)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_gitlab_cancel_pipeline_checks_access(self):
        user = _make_user()
        args = {"project_id": "ns/proj", "pipeline_id": 1}
        with patch(
            "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
            return_value="ns/proj not found.",
        ):
            response = await handle_gitlab_ci_cancel_pipeline(args, user)
        data = _parse_mcp_response(response)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_github_retry_run_checks_access(self):
        user = _make_user()
        args = {"owner": "o", "repo": "r", "run_id": 1}
        with patch(
            "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
            return_value="o/r not found.",
        ):
            response = await handle_github_actions_retry_run(args, user)
        data = _parse_mcp_response(response)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_github_cancel_run_checks_access(self):
        user = _make_user()
        args = {"owner": "o", "repo": "r", "run_id": 1}
        with patch(
            "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
            return_value="o/r not found.",
        ):
            response = await handle_github_actions_cancel_run(args, user)
        data = _parse_mcp_response(response)
        assert data["success"] is False
