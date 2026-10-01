"""
inline_query.py's FTS-
availability check must resolve a non-global activated repo's on-disk path
via ActivatedRepoManager.get_activated_repo_path(username, user_alias) --
the shared realpath-containment helper every other (username, user_alias)
join in this codebase already routes through -- instead of a hand-built
`PathLib(activated_repo_manager.activated_repos_dir) / username / alias`
join that bypasses it entirely.

This is a REGRESSION test for the specific call site at
inline_query.py's search_mode in ["fts", "hybrid"] branch: against the
OLD hand-built-join code, get_activated_repo_path is never called at all.
"""

from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers.inline_query import register_query_routes


def _make_user(username: str = "alice") -> User:
    user = MagicMock(spec=User)
    user.username = username
    user.role = UserRole.NORMAL_USER
    return user


class TestFtsAvailabilityCheckUsesSafePathHelper:
    def test_get_activated_repo_path_called_with_username_and_alias(
        self, tmp_path: Path, monkeypatch
    ):
        activated_repos_dir = tmp_path / "activated-repos"
        repo_dir = activated_repos_dir / "alice" / "myrepo"
        (repo_dir / ".code-indexer" / "tantivy_index").mkdir(parents=True)

        fast_app = FastAPI()
        fast_app.state.payload_cache = None
        fast_app.state.access_filtering_service = None
        fast_app.state.search_event_log_writer = None

        mock_activated_repo_manager = MagicMock()
        mock_activated_repo_manager.activated_repos_dir = str(activated_repos_dir)
        mock_activated_repo_manager.list_activated_repositories.return_value = [
            {"user_alias": "myrepo", "username": "alice", "is_global": False}
        ]
        mock_activated_repo_manager.get_activated_repo_path.side_effect = (
            lambda username, user_alias: str(
                activated_repos_dir / username / user_alias
            )
        )

        mock_semantic_query_manager = MagicMock()

        register_query_routes(
            fast_app,
            semantic_query_manager=mock_semantic_query_manager,
            activated_repo_manager=mock_activated_repo_manager,
        )

        from code_indexer.server.auth import dependencies as auth_deps

        fast_app.dependency_overrides[auth_deps.get_current_user] = lambda: (
            _make_user("alice")
        )

        cfg_mock = MagicMock()
        cfg_mock.get_config.return_value.node_id = "test-node"
        monkeypatch.setattr(
            "code_indexer.server.routers.inline_query.get_config_service",
            lambda: cfg_mock,
            raising=False,
        )

        class _StubTantivyManager:
            def __init__(self, index_dir):
                self._index_dir = index_dir

            def open_for_search(self):
                pass

            def search(self, **kwargs):
                return []

        monkeypatch.setattr(
            "code_indexer.services.tantivy_index_manager.TantivyIndexManager",
            _StubTantivyManager,
        )

        client = TestClient(fast_app, raise_server_exceptions=False)
        response = client.post(
            "/api/query",
            json={
                "query_text": "find auth",
                "repository_alias": "myrepo",
                "search_mode": "fts",
            },
        )

        assert response.status_code == 200, response.text
        mock_activated_repo_manager.get_activated_repo_path.assert_called_once_with(
            "alice", "myrepo"
        )
