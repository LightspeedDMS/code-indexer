"""
Unit tests for Story #1032 AC4 — MCP list_repositories handler:
deactivation_job field included per activated repo.

AC4: The field is null when no in-flight deactivation exists for the repo,
and populated with {job_id, status} when a deactivate_repository job is
PENDING or RUNNING for that (username, user_alias) pair.

The tests call list_repositories() directly with mocked app module state,
following the same pattern used in test_handlers_field_stripping.py.
"""

import json
import pytest
from unittest.mock import Mock, patch

from code_indexer.server.mcp.handlers import list_repositories
from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def regular_user():
    user = Mock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


def _make_activated_repo(user_alias: str, username: str = "testuser") -> dict:
    return {
        "user_alias": user_alias,
        "golden_repo_alias": f"golden-{user_alias}",
        "current_branch": "main",
        "activated_at": "2026-01-01T00:00:00+00:00",
        "last_accessed": "2026-01-01T00:00:00+00:00",
        "username": username,
    }


def _make_deact_job(job_id: str, repo_alias: str, username: str, status: str) -> dict:
    return {
        "job_id": job_id,
        "operation_type": "deactivate_repository",
        "status": status,
        "repo_alias": repo_alias,
        "username": username,
    }


def _call_list_repositories(
    activated_repos: list,
    active_jobs: list,
    user: User,
) -> dict:
    """Call list_repositories with mocked app state and return parsed JSON."""
    mock_bjm = Mock()
    mock_bjm.list_jobs.return_value = {
        "jobs": active_jobs,
        "total": len(active_jobs),
        "limit": 500,
        "offset": 0,
    }

    mock_category_service = Mock()
    mock_category_service.get_repo_category_map = Mock(return_value={})

    with (
        patch("code_indexer.server.app.activated_repo_manager") as mock_arm,
        patch("code_indexer.server.mcp.handlers._get_golden_repos_dir"),
        patch("code_indexer.server.mcp.handlers._list_global_repos", return_value=[]),
        patch("code_indexer.server.app.golden_repo_manager") as mock_grm,
        patch("code_indexer.server.app.background_job_manager", mock_bjm),
    ):
        mock_arm.list_activated_repositories = Mock(return_value=activated_repos)
        mock_grm._repo_category_service = mock_category_service

        result = list_repositories({}, user)

    return json.loads(result["content"][0]["text"])  # type: ignore[no-any-return]


class TestListReposDeactivationJobFieldNull:
    """AC4: deactivation_job is null when no active deactivation exists."""

    def test_deactivation_job_null_when_no_jobs(self, regular_user):
        """deactivation_job field is null when background_job_manager has no active jobs."""
        repos = [_make_activated_repo("myrepo")]
        data = _call_list_repositories(repos, [], regular_user)

        assert data["success"] is True
        assert len(data["repositories"]) == 1
        repo = data["repositories"][0]
        assert "deactivation_job" in repo, (
            "deactivation_job field must be present in MCP list_repositories response"
        )
        assert repo["deactivation_job"] is None

    def test_deactivation_job_null_when_no_matching_job(self, regular_user):
        """deactivation_job is null when active job belongs to a different repo."""
        repos = [_make_activated_repo("myrepo")]
        jobs = [_make_deact_job("job-999", "otherrepo", "testuser", "running")]
        data = _call_list_repositories(repos, jobs, regular_user)

        assert data["success"] is True
        repo = data["repositories"][0]
        assert repo["deactivation_job"] is None


class TestListReposDeactivationJobFieldPopulated:
    """AC4: deactivation_job is populated when an in-flight job matches the repo."""

    def test_deactivation_job_populated_for_running_job(self, regular_user):
        """deactivation_job contains job_id and status for a running deactivation."""
        repos = [_make_activated_repo("myrepo")]
        jobs = [_make_deact_job("deact-abc-123", "myrepo", "testuser", "running")]
        data = _call_list_repositories(repos, jobs, regular_user)

        assert data["success"] is True
        repo = data["repositories"][0]
        assert repo["deactivation_job"] is not None
        assert repo["deactivation_job"]["job_id"] == "deact-abc-123"
        assert repo["deactivation_job"]["status"] == "running"

    def test_deactivation_job_populated_for_pending_job(self, regular_user):
        """deactivation_job contains job_id and status for a pending deactivation."""
        repos = [_make_activated_repo("myrepo")]
        jobs = [_make_deact_job("deact-xyz-456", "myrepo", "testuser", "pending")]
        data = _call_list_repositories(repos, jobs, regular_user)

        assert data["success"] is True
        repo = data["repositories"][0]
        assert repo["deactivation_job"] is not None
        assert repo["deactivation_job"]["job_id"] == "deact-xyz-456"
        assert repo["deactivation_job"]["status"] == "pending"

    def test_multiple_repos_correct_job_assigned(self, regular_user):
        """Each repo receives its own deactivation_job, not another repo's job."""
        repos = [
            _make_activated_repo("repo-a"),
            _make_activated_repo("repo-b"),
        ]
        jobs = [_make_deact_job("job-for-b", "repo-b", "testuser", "running")]
        data = _call_list_repositories(repos, jobs, regular_user)

        assert data["success"] is True
        assert len(data["repositories"]) == 2

        repo_a = next(r for r in data["repositories"] if r["user_alias"] == "repo-a")
        repo_b = next(r for r in data["repositories"] if r["user_alias"] == "repo-b")

        assert repo_a["deactivation_job"] is None
        assert repo_b["deactivation_job"] is not None
        assert repo_b["deactivation_job"]["job_id"] == "job-for-b"
