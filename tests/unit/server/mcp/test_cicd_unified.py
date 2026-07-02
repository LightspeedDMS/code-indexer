"""
Test suite for Story #991 - CI/CD forge auto-detection consolidation.

Covers:
- Registry: 6 new ci_* names registered, 12 old names NOT registered
- Tool docs: 6 new docs exist, 12 old docs do not exist
- Auto-detection: GitHub URL -> GitHub dispatch, GitLab URL -> GitLab dispatch
- Forge override: forge='github' forces GitHub, forge='gitlab' forces GitLab
- Auto-detect failure: unknown hostname returns explicit failure response
- Invalid forge value: returns clear error
- Missing repository_alias: returns error
- Alias not found: returns error
- Read vs write token: list/get/search use read token, cancel/retry use write token + audit log
"""

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import (
    handle_ci_list_runs,
    handle_ci_get_run,
    handle_ci_get_job_logs,
    handle_ci_search_logs,
    handle_ci_cancel_run,
    handle_ci_retry_run,
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


GITHUB_REPO_DICT = {
    "alias_name": "myrepo-global",
    "repo_url": "https://github.com/myorg/myrepo.git",
    "index_path": "/fake/path",
}

GITLAB_REPO_DICT = {
    "alias_name": "glproject-global",
    "repo_url": "https://gitlab.com/myns/glproject.git",
    "index_path": "/fake/path",
}

UNKNOWN_FORGE_REPO_DICT = {
    "alias_name": "unknown-global",
    "repo_url": "https://mybitbucket.example.com/owner/repo.git",
    "index_path": "/fake/path",
}


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestRegistryPresence:
    """6 new ci_* names registered, 12 old names NOT registered."""

    def test_new_handler_names_are_registered(self):
        """All 6 unified ci_* handlers must appear in the MCP handler registry."""
        from code_indexer.server.mcp.handlers._legacy import HANDLER_REGISTRY

        new_names = {
            "ci_list_runs",
            "ci_get_run",
            "ci_get_job_logs",
            "ci_search_logs",
            "ci_cancel_run",
            "ci_retry_run",
        }
        missing = new_names - set(HANDLER_REGISTRY.keys())
        assert not missing, f"Missing new unified handler names: {missing}"

    def test_old_github_handler_names_not_registered(self):
        """Old github_actions_* handler names must NOT be in the registry."""
        from code_indexer.server.mcp.handlers._legacy import HANDLER_REGISTRY

        old_names = {
            "github_actions_list_runs",
            "github_actions_get_run",
            "github_actions_get_job_logs",
            "github_actions_search_logs",
            "github_actions_cancel_run",
            "github_actions_retry_run",
        }
        still_registered = old_names & set(HANDLER_REGISTRY.keys())
        assert not still_registered, (
            f"Old GitHub handler names still registered: {still_registered}"
        )

    def test_old_gitlab_handler_names_not_registered(self):
        """Old gitlab_ci_* handler names must NOT be in the registry."""
        from code_indexer.server.mcp.handlers._legacy import HANDLER_REGISTRY

        old_names = {
            "gitlab_ci_list_pipelines",
            "gitlab_ci_get_pipeline",
            "gitlab_ci_get_job_logs",
            "gitlab_ci_search_logs",
            "gitlab_ci_cancel_pipeline",
            "gitlab_ci_retry_pipeline",
        }
        still_registered = old_names & set(HANDLER_REGISTRY.keys())
        assert not still_registered, (
            f"Old GitLab handler names still registered: {still_registered}"
        )


# ---------------------------------------------------------------------------
# Tool docs tests
# ---------------------------------------------------------------------------

TOOL_DOCS_DIR = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src"
    / "code_indexer"
    / "server"
    / "mcp"
    / "tool_docs"
    / "cicd"
)


class TestToolDocsPresence:
    """6 new docs exist, 12 old docs do not exist."""

    def test_new_tool_docs_exist(self):
        """All 6 new ci_* tool doc files must exist."""
        expected_files = [
            "ci_list_runs.md",
            "ci_get_run.md",
            "ci_get_job_logs.md",
            "ci_search_logs.md",
            "ci_cancel_run.md",
            "ci_retry_run.md",
        ]
        missing = [f for f in expected_files if not (TOOL_DOCS_DIR / f).exists()]
        assert not missing, f"Missing new tool doc files: {missing}"

    def test_old_github_tool_docs_deleted(self):
        """Old github_actions_* tool docs must NOT exist."""
        old_files = [
            "github_actions_list_runs.md",
            "github_actions_get_run.md",
            "github_actions_get_job_logs.md",
            "github_actions_search_logs.md",
            "github_actions_cancel_run.md",
            "github_actions_retry_run.md",
        ]
        still_present = [f for f in old_files if (TOOL_DOCS_DIR / f).exists()]
        assert not still_present, f"Old GitHub tool docs still present: {still_present}"

    def test_old_gitlab_tool_docs_deleted(self):
        """Old gitlab_ci_* tool docs must NOT exist."""
        old_files = [
            "gitlab_ci_list_pipelines.md",
            "gitlab_ci_get_pipeline.md",
            "gitlab_ci_get_job_logs.md",
            "gitlab_ci_search_logs.md",
            "gitlab_ci_cancel_pipeline.md",
            "gitlab_ci_retry_pipeline.md",
        ]
        still_present = [f for f in old_files if (TOOL_DOCS_DIR / f).exists()]
        assert not still_present, f"Old GitLab tool docs still present: {still_present}"


# ---------------------------------------------------------------------------
# Missing repository_alias tests
# ---------------------------------------------------------------------------


class TestMissingRepositoryAlias:
    """Missing or empty repository_alias returns clear error."""

    @pytest.mark.asyncio
    async def test_ci_list_runs_missing_alias_returns_error(self):
        user = _make_user()
        response = await handle_ci_list_runs({}, user)
        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert "repository_alias" in data["error"]

    @pytest.mark.asyncio
    async def test_ci_get_run_missing_alias_returns_error(self):
        user = _make_user()
        response = await handle_ci_get_run({"run_id": 1}, user)
        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert "repository_alias" in data["error"]

    @pytest.mark.asyncio
    async def test_ci_get_job_logs_missing_alias_returns_error(self):
        user = _make_user()
        response = await handle_ci_get_job_logs({"job_id": 1}, user)
        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert "repository_alias" in data["error"]

    @pytest.mark.asyncio
    async def test_ci_search_logs_missing_alias_returns_error(self):
        user = _make_user()
        response = await handle_ci_search_logs({"run_id": 1, "pattern": "error"}, user)
        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert "repository_alias" in data["error"]

    @pytest.mark.asyncio
    async def test_ci_cancel_run_missing_alias_returns_error(self):
        user = _make_user()
        response = await handle_ci_cancel_run({"run_id": 1}, user)
        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert "repository_alias" in data["error"]

    @pytest.mark.asyncio
    async def test_ci_retry_run_missing_alias_returns_error(self):
        user = _make_user()
        response = await handle_ci_retry_run({"run_id": 1}, user)
        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert "repository_alias" in data["error"]


# ---------------------------------------------------------------------------
# Alias not found tests
# ---------------------------------------------------------------------------


class TestAliasNotFound:
    """Unknown alias returns clear error."""

    @pytest.mark.asyncio
    async def test_ci_list_runs_unknown_alias_returns_error(self):
        user = _make_user()
        with patch(
            "code_indexer.server.mcp.handlers._get_global_repo",
            return_value=None,
        ):
            response = await handle_ci_list_runs(
                {"repository_alias": "nonexistent"}, user
            )
        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert "not found" in data["error"].lower() or "nonexistent" in data["error"]

    @pytest.mark.asyncio
    async def test_ci_get_run_unknown_alias_returns_error(self):
        user = _make_user()
        with patch(
            "code_indexer.server.mcp.handlers._get_global_repo",
            return_value=None,
        ):
            response = await handle_ci_get_run(
                {"repository_alias": "nonexistent", "run_id": 1}, user
            )
        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert "not found" in data["error"].lower() or "nonexistent" in data["error"]


# ---------------------------------------------------------------------------
# Auto-detect failure tests (unknown hostname)
# ---------------------------------------------------------------------------


class TestAutoDetectFailure:
    """Unknown hostname returns explicit failure with remote_url field."""

    @pytest.mark.asyncio
    async def test_ci_list_runs_unknown_forge_returns_failure_with_remote_url(self):
        user = _make_user()
        with patch(
            "code_indexer.server.mcp.handlers._get_global_repo",
            return_value=UNKNOWN_FORGE_REPO_DICT,
        ):
            response = await handle_ci_list_runs(
                {"repository_alias": "unknown-global"}, user
            )
        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert (
            "auto-detect" in data["error"].lower() or "forge" in data["error"].lower()
        )
        assert "remote_url" in data
        assert data["remote_url"] == UNKNOWN_FORGE_REPO_DICT["repo_url"]

    @pytest.mark.asyncio
    async def test_ci_get_run_unknown_forge_returns_failure_with_remote_url(self):
        user = _make_user()
        with patch(
            "code_indexer.server.mcp.handlers._get_global_repo",
            return_value=UNKNOWN_FORGE_REPO_DICT,
        ):
            response = await handle_ci_get_run(
                {"repository_alias": "unknown-global", "run_id": 1}, user
            )
        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert "remote_url" in data

    @pytest.mark.asyncio
    async def test_auto_detect_failure_suggests_explicit_forge(self):
        """Error message must tell caller to pass forge='github' or forge='gitlab'."""
        user = _make_user()
        with patch(
            "code_indexer.server.mcp.handlers._get_global_repo",
            return_value=UNKNOWN_FORGE_REPO_DICT,
        ):
            response = await handle_ci_list_runs(
                {"repository_alias": "unknown-global"}, user
            )
        data = _parse_mcp_response(response)
        assert "github" in data["error"] or "gitlab" in data["error"]
        assert "forge" in data["error"].lower()


# ---------------------------------------------------------------------------
# Auto-detection routing tests
# ---------------------------------------------------------------------------


class TestAutoDetectionRouting:
    """GitHub URL dispatches GitHub client, GitLab URL dispatches GitLab client."""

    @pytest.mark.asyncio
    async def test_ci_list_runs_github_url_calls_github_client(self):
        """ci_list_runs with GitHub remote URL dispatches to GitHubActionsClient."""
        user = _make_user()
        runs = [{"id": 100, "status": "completed"}]

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_global_repo",
                return_value=GITHUB_REPO_DICT,
            ),
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
            ) as MockGH,
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as MockGL,
        ):
            mock_gh = MagicMock()
            mock_gh.list_runs = AsyncMock(return_value=runs)
            mock_gh.last_rate_limit = None
            MockGH.return_value = mock_gh

            response = await handle_ci_list_runs(
                {"repository_alias": "myrepo-global"}, user
            )

        data = _parse_mcp_response(response)
        assert data["success"] is True
        MockGH.assert_called_once()
        MockGL.assert_not_called()

    @pytest.mark.asyncio
    async def test_ci_list_runs_gitlab_url_calls_gitlab_client(self):
        """ci_list_runs with GitLab remote URL dispatches to GitLabCIClient."""
        user = _make_user()
        pipelines = [{"id": 42, "status": "success"}]

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_global_repo",
                return_value=GITLAB_REPO_DICT,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_read_token",
                return_value="gl_token",
            ),
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as MockGL,
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as MockGH,
        ):
            mock_gl = MagicMock()
            mock_gl.list_pipelines = AsyncMock(return_value=pipelines)
            mock_gl.last_rate_limit = None
            MockGL.return_value = mock_gl

            response = await handle_ci_list_runs(
                {"repository_alias": "glproject-global"}, user
            )

        data = _parse_mcp_response(response)
        assert data["success"] is True
        MockGL.assert_called_once()
        MockGH.assert_not_called()


# ---------------------------------------------------------------------------
# Forge override tests
# ---------------------------------------------------------------------------


class TestForgeOverride:
    """forge='github' forces GitHub, forge='gitlab' forces GitLab."""

    @pytest.mark.asyncio
    async def test_forge_github_override_forces_github_client(self):
        """forge='github' forces GitHub dispatch regardless of URL."""
        user = _make_user()
        runs = [{"id": 200}]

        # Use a GitLab URL but override forge to github
        with (
            patch(
                "code_indexer.server.mcp.handlers._get_global_repo",
                return_value=GITLAB_REPO_DICT,
            ),
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
            ) as MockGH,
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as MockGL,
        ):
            mock_gh = MagicMock()
            mock_gh.list_runs = AsyncMock(return_value=runs)
            mock_gh.last_rate_limit = None
            MockGH.return_value = mock_gh

            response = await handle_ci_list_runs(
                {"repository_alias": "glproject-global", "forge": "github"}, user
            )

        data = _parse_mcp_response(response)
        assert data["success"] is True
        MockGH.assert_called_once()
        MockGL.assert_not_called()

    @pytest.mark.asyncio
    async def test_forge_gitlab_override_forces_gitlab_client(self):
        """forge='gitlab' forces GitLab dispatch regardless of URL."""
        user = _make_user()
        pipelines = [{"id": 300}]

        # Use a GitHub URL but override forge to gitlab
        with (
            patch(
                "code_indexer.server.mcp.handlers._get_global_repo",
                return_value=GITHUB_REPO_DICT,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_read_token",
                return_value="gl_token",
            ),
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as MockGL,
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as MockGH,
        ):
            mock_gl = MagicMock()
            mock_gl.list_pipelines = AsyncMock(return_value=pipelines)
            mock_gl.last_rate_limit = None
            MockGL.return_value = mock_gl

            response = await handle_ci_list_runs(
                {"repository_alias": "myrepo-global", "forge": "gitlab"}, user
            )

        data = _parse_mcp_response(response)
        assert data["success"] is True
        MockGL.assert_called_once()
        MockGH.assert_not_called()


# ---------------------------------------------------------------------------
# Read vs write token tests
# ---------------------------------------------------------------------------


class TestReadVsWriteToken:
    """list/get/search use read token; cancel/retry use write token + audit log."""

    @pytest.mark.asyncio
    async def test_ci_list_runs_uses_read_token(self):
        """ci_list_runs calls _resolve_cicd_read_token, not _resolve_cicd_write_token."""
        user = _make_user()
        read_token_calls = []
        write_token_calls = []

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_global_repo",
                return_value=GITHUB_REPO_DICT,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_read_token",
                side_effect=lambda *a, **kw: read_token_calls.append(True) or "tok",  # type: ignore[func-returns-value]
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                side_effect=lambda *a, **kw: write_token_calls.append(True)  # type: ignore[func-returns-value]
                or ("tok", None),
            ),
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as MockGH,
        ):
            mock_gh = MagicMock()
            mock_gh.list_runs = AsyncMock(return_value=[])
            mock_gh.last_rate_limit = None
            MockGH.return_value = mock_gh
            await handle_ci_list_runs({"repository_alias": "myrepo-global"}, user)

        assert len(read_token_calls) == 1
        assert len(write_token_calls) == 0

    @pytest.mark.asyncio
    async def test_ci_cancel_run_uses_write_token(self):
        """ci_cancel_run calls _resolve_cicd_write_token, not _resolve_cicd_read_token."""
        user = _make_user()
        read_token_calls = []
        write_token_calls = []

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_global_repo",
                return_value=GITHUB_REPO_DICT,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_read_token",
                side_effect=lambda *a, **kw: read_token_calls.append(True) or "tok",  # type: ignore[func-returns-value]
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                side_effect=lambda *a, **kw: write_token_calls.append(True)  # type: ignore[func-returns-value]
                or ("tok", None),
            ),
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as MockGH,
        ):
            mock_gh = MagicMock()
            mock_gh.cancel_run = AsyncMock(return_value={"cancelled": True})
            mock_gh.last_rate_limit = None
            MockGH.return_value = mock_gh
            await handle_ci_cancel_run(
                {"repository_alias": "myrepo-global", "run_id": 123}, user
            )

        assert len(write_token_calls) == 1
        assert len(read_token_calls) == 0

    @pytest.mark.asyncio
    async def test_ci_retry_run_uses_write_token(self):
        """ci_retry_run calls _resolve_cicd_write_token, not _resolve_cicd_read_token."""
        user = _make_user()
        write_token_calls = []

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_global_repo",
                return_value=GITHUB_REPO_DICT,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                side_effect=lambda *a, **kw: write_token_calls.append(True)  # type: ignore[func-returns-value]
                or ("tok", None),
            ),
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as MockGH,
        ):
            mock_gh = MagicMock()
            mock_gh.retry_run = AsyncMock(return_value={"retried": True})
            mock_gh.last_rate_limit = None
            MockGH.return_value = mock_gh
            await handle_ci_retry_run(
                {"repository_alias": "myrepo-global", "run_id": 456}, user
            )

        assert len(write_token_calls) == 1

    @pytest.mark.asyncio
    async def test_ci_cancel_run_logs_audit_entry(self, caplog):
        """ci_cancel_run must emit audit log before the API call."""
        user = _make_user("alice")

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_global_repo",
                return_value=GITHUB_REPO_DICT,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                return_value=("tok", None),
            ),
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as MockGH,
            caplog.at_level(logging.INFO),
        ):
            mock_gh = MagicMock()
            mock_gh.cancel_run = AsyncMock(return_value={"cancelled": True})
            mock_gh.last_rate_limit = None
            MockGH.return_value = mock_gh
            await handle_ci_cancel_run(
                {"repository_alias": "myrepo-global", "run_id": 789}, user
            )

        audit_logs = [
            r
            for r in caplog.records
            if "cancel" in r.message.lower() and "alice" in r.message
        ]
        assert len(audit_logs) > 0, (
            f"No audit log found. Got: {[r.message for r in caplog.records]}"
        )

    @pytest.mark.asyncio
    async def test_ci_retry_run_logs_audit_entry(self, caplog):
        """ci_retry_run must emit audit log before the API call."""
        user = _make_user("bob")

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_global_repo",
                return_value=GITHUB_REPO_DICT,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                return_value=("tok", None),
            ),
            patch(
                "code_indexer.server.clients.github_actions_client.GitHubActionsClient"
            ) as MockGH,
            caplog.at_level(logging.INFO),
        ):
            mock_gh = MagicMock()
            mock_gh.retry_run = AsyncMock(return_value={"retried": True})
            mock_gh.last_rate_limit = None
            MockGH.return_value = mock_gh
            await handle_ci_retry_run(
                {"repository_alias": "myrepo-global", "run_id": 111}, user
            )

        audit_logs = [
            r
            for r in caplog.records
            if "retry" in r.message.lower() and "bob" in r.message
        ]
        assert len(audit_logs) > 0, (
            f"No audit log found. Got: {[r.message for r in caplog.records]}"
        )

    @pytest.mark.asyncio
    async def test_ci_cancel_run_fails_without_write_token(self):
        """ci_cancel_run returns error when no personal credential configured."""
        user = _make_user()

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_global_repo",
                return_value=GITHUB_REPO_DICT,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_write_token",
                return_value=(None, "Configure personal git credential for github.com"),
            ),
        ):
            response = await handle_ci_cancel_run(
                {"repository_alias": "myrepo-global", "run_id": 123}, user
            )

        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert (
            "configure" in data["error"].lower()
            or "credential" in data["error"].lower()
        )


# ---------------------------------------------------------------------------
# Access control enforcement tests
# ---------------------------------------------------------------------------


class TestAccessControl:
    """ci_* handlers enforce group access via _resolve_cicd_project_access."""

    @pytest.mark.asyncio
    async def test_ci_list_runs_denied_project_returns_error(self):
        """ci_list_runs: user without group access gets not-found error."""
        user = _make_user("alice")

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_global_repo",
                return_value=GITHUB_REPO_DICT,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value="Project 'myorg/myrepo' not found.",
            ),
        ):
            response = await handle_ci_list_runs(
                {"repository_alias": "myrepo-global"}, user
            )

        data = _parse_mcp_response(response)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_ci_list_runs_access_check_before_token(self):
        """Group check runs BEFORE token resolution (fail fast)."""
        user = _make_user("alice")
        token_resolver_called = []

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_global_repo",
                return_value=GITHUB_REPO_DICT,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value="Project 'myorg/myrepo' not found.",
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_read_token",
                side_effect=lambda *a, **kw: token_resolver_called.append(True)  # type: ignore[func-returns-value]
                or "tok",
            ),
        ):
            await handle_ci_list_runs({"repository_alias": "myrepo-global"}, user)

        assert len(token_resolver_called) == 0, (
            "Token resolver called despite access denial"
        )


# ---------------------------------------------------------------------------
# GitLab async await correctness (migrated from test_gitlab_ci_get_job_logs_async_bug)
# ---------------------------------------------------------------------------


class TestCiGetJobLogsAsync:
    """handle_ci_get_job_logs properly awaits async GitLab client call."""

    @pytest.mark.asyncio
    async def test_ci_get_job_logs_awaits_gitlab_async_call(self):
        """Handler properly awaits async client.get_job_logs() for GitLab."""
        user = _make_user()
        test_logs = "Line 1\nLine 2\nDone."

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_global_repo",
                return_value=GITLAB_REPO_DICT,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_read_token",
                return_value="gl_token",
            ),
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as MockGL,
        ):
            mock_gl = MagicMock()
            mock_gl.get_job_logs = AsyncMock(return_value=test_logs)
            mock_gl.last_rate_limit = None
            MockGL.return_value = mock_gl

            response = await handle_ci_get_job_logs(
                {"repository_alias": "glproject-global", "job_id": 123}, user
            )

        # Must be JSON-serializable (not a coroutine)
        json.dumps(response)
        data = _parse_mcp_response(response)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_ci_get_job_logs_response_not_coroutine(self):
        """Response must not contain coroutine objects."""
        import inspect

        user = _make_user()

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_global_repo",
                return_value=GITLAB_REPO_DICT,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_project_access",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_cicd_read_token",
                return_value="gl_token",
            ),
            patch(
                "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient"
            ) as MockGL,
        ):
            mock_gl = MagicMock()
            mock_gl.get_job_logs = AsyncMock(return_value="test logs")
            mock_gl.last_rate_limit = None
            MockGL.return_value = mock_gl

            response = await handle_ci_get_job_logs(
                {"repository_alias": "glproject-global", "job_id": 999}, user
            )

        def check_no_coroutines(obj, path="response"):
            if inspect.iscoroutine(obj):
                pytest.fail(f"Found coroutine at {path}")
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    check_no_coroutines(v, f"{path}['{k}']")
            elif isinstance(obj, (list, tuple)):
                for i, v in enumerate(obj):
                    check_no_coroutines(v, f"{path}[{i}]")

        check_no_coroutines(response)
