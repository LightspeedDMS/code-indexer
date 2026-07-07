"""Bug #1314: `refresh_golden_repo` MCP handler has its OWN inline stale
in-memory dict check, bypassing GoldenRepoManager entirely.

`refresh_golden_repo` (src/code_indexer/server/mcp/handlers/repos.py) checks
`alias not in golden_repo_manager.golden_repos` directly, instead of calling
a manager method. Even after `GoldenRepoManager.golden_repo_exists()` /
`_resolve_golden_repo()` are fixed to reload from the shared backend on a
cache miss, this handler's raw dict membership check would still return the
stale "not found" result for a repo registered by a DIFFERENT worker/node,
because it never calls into the manager's resolver at all.

Uses a REAL `GoldenRepoManager` (real SQLite backend, no mocked DB layer --
memory: feedback_faithful_db_mocks) with a repo inserted directly into the
backend to simulate a cross-worker registration, exactly like the
GoldenRepoManager-level reproduction in
test_golden_repo_manager_cross_worker_staleness_bug1314.py.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers.repos import refresh_golden_repo
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


@pytest.fixture
def admin_user() -> User:
    user = Mock(spec=User)
    user.username = "admin"
    user.role = UserRole.ADMIN
    user.has_permission = Mock(return_value=True)
    return user


@pytest.fixture
def manager(tmp_path) -> GoldenRepoManager:
    return GoldenRepoManager(data_dir=str(tmp_path))


def _register_repo_directly(manager: GoldenRepoManager, alias: str, tmp_path) -> None:
    """Simulate a DIFFERENT worker registering a golden repo: write straight
    to the shared SQLite backend WITHOUT touching this manager's in-memory
    `golden_repos` cache (Bug #1314 cross-worker staleness)."""
    clone_path = tmp_path / "golden-repos" / alias
    clone_path.mkdir(parents=True, exist_ok=True)
    manager._sqlite_backend.add_repo(
        alias=alias,
        repo_url="https://example.com/cross-worker-refresh.git",
        default_branch="main",
        clone_path=str(clone_path),
        created_at="2026-01-01T00:00:00+00:00",
        enable_temporal=False,
        temporal_options=None,
    )


class TestRefreshGoldenRepoHandlerCrossWorkerStaleness:
    def test_refresh_golden_repo_resolves_repo_registered_by_another_worker(
        self, manager: GoldenRepoManager, admin_user: User, tmp_path
    ) -> None:
        alias = "cross-worker-refresh-handler"
        _register_repo_directly(manager, alias, tmp_path)

        assert alias not in manager.golden_repos, (
            "test setup invalid: alias must be absent from this worker's "
            "in-memory cache to reproduce Bug #1314"
        )

        mock_scheduler = Mock()
        mock_scheduler.trigger_refresh_for_repo = Mock(return_value="job-refresh-1")

        with (
            patch("code_indexer.server.app.golden_repo_manager", manager),
            patch(
                "code_indexer.server.mcp.handlers.repos._get_app_refresh_scheduler",
                return_value=mock_scheduler,
            ),
        ):
            result = refresh_golden_repo({"alias": alias}, admin_user)

        import json

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["success"] is True, response_data
        assert response_data["job_id"] == "job-refresh-1"
