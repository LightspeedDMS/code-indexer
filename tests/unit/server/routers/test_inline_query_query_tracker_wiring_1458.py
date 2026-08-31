"""Codex Finding #7 (HIGH, Story #1458 review round): REST POST /api/query
must wire the SAME activated-repo QueryTracker refcount protection MCP
search.py already has -- previously deactivation's bounded drain
(wait_for_activated_repo_query_drain) had nothing real to observe for
queries arriving via this front door, meaning a deactivation could read
zero in-flight queries and purge the activated clone's chunks.db mid-read.

Real QueryTracker (not mocked) -- a side_effect on the mocked
semantic_query_manager.query_user_repositories asserts the refcount is
genuinely held DURING the call, proving the REAL route body wires
track_activated_repo_query, not merely that the helper function works in
isolation.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers.inline_query import register_query_routes


def _make_user(username: str = "alice") -> User:
    user = MagicMock(spec=User)
    user.username = username
    user.role = UserRole.NORMAL_USER
    return user


def _qm_result():
    return {
        "results": [],
        "total_results": 0,
        "query_metadata": {},
        "warning": None,
    }


@pytest.fixture
def real_query_tracker() -> QueryTracker:
    return QueryTracker()


@pytest.fixture
def app_with_real_tracker(real_query_tracker, monkeypatch):
    """FastAPI app with query routes registered, a REAL QueryTracker on
    app.state, and an activated_repo_manager whose activated_repos_dir is
    a real string (required for the refcount key construction)."""
    fast_app = FastAPI()
    fast_app.state.payload_cache = None
    fast_app.state.access_filtering_service = None
    fast_app.state.search_event_log_writer = None
    fast_app.state.query_tracker = real_query_tracker

    mock_activated_repo_manager = MagicMock()
    mock_activated_repo_manager.get_activated_repos.return_value = []
    mock_activated_repo_manager.activated_repos_dir = "/data/activated-repos"

    mock_semantic_query_manager = MagicMock()
    mock_semantic_query_manager.query_user_repositories.return_value = _qm_result()

    register_query_routes(
        fast_app,
        semantic_query_manager=mock_semantic_query_manager,
        activated_repo_manager=mock_activated_repo_manager,
    )

    from code_indexer.server.auth import dependencies as auth_deps

    fast_app.dependency_overrides[auth_deps.get_current_user] = lambda: _make_user(
        "alice"
    )

    cfg_mock = MagicMock()
    cfg_mock.get_config.return_value.node_id = "test-node"
    monkeypatch.setattr(
        "code_indexer.server.routers.inline_query.get_config_service",
        lambda: cfg_mock,
        raising=False,
    )

    return fast_app, mock_semantic_query_manager, mock_activated_repo_manager


class TestRestQueryTrackerWiring:
    def test_refcount_is_held_during_semantic_mode_query_and_released_after(
        self, app_with_real_tracker, real_query_tracker
    ) -> None:
        fast_app, mock_qm, mock_arm = app_with_real_tracker
        expected_key = "/data/activated-repos/alice/myrepo"
        observed = {"refcount_during_call": None}

        def _side_effect(**kwargs):
            observed["refcount_during_call"] = real_query_tracker.get_ref_count(
                expected_key
            )
            return _qm_result()

        mock_qm.query_user_repositories.side_effect = _side_effect

        client = TestClient(fast_app, raise_server_exceptions=False)
        response = client.post(
            "/api/query",
            json={"query_text": "find auth", "repository_alias": "myrepo"},
        )

        assert response.status_code == 200
        assert observed["refcount_during_call"] == 1, (
            "Bug: POST /api/query did not hold a QueryTracker refcount "
            "during the query -- deactivation's drain would observe zero "
            "in-flight queries and could purge chunks.db mid-read."
        )
        assert real_query_tracker.get_ref_count(expected_key) == 0

    def test_noop_for_global_alias_no_refcount_key_ever_created(
        self, app_with_real_tracker, real_query_tracker
    ) -> None:
        """A -global (golden-repo) alias must NOT be tracked by this
        activated-repo-specific helper -- that is a separate, already-
        covered concern (MCP's _search_global_repo)."""
        fast_app, mock_qm, mock_arm = app_with_real_tracker

        client = TestClient(fast_app, raise_server_exceptions=False)
        response = client.post(
            "/api/query",
            json={"query_text": "find auth", "repository_alias": "myrepo-global"},
        )

        assert response.status_code == 200
        assert real_query_tracker.get_all_paths() == set()


class TestRestQueryTrackerWiringFtsMode:
    """Codex HIGH finding (round 2): the pure search_mode='fts' REST path
    reads the Tantivy index directly (TantivyIndexManager against
    repo_path/.code-indexer/tantivy_index) -- it never calls
    query_user_repositories() at all, so it was completely invisible to
    the track_activated_repo_query() wrapping already applied to the
    semantic/hybrid branch."""

    def test_refcount_is_held_during_pure_fts_mode_query_and_released_after(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        real_query_tracker = QueryTracker()
        activated_repos_dir = tmp_path / "activated-repos"
        expected_key = str(activated_repos_dir / "alice" / "myrepo")

        repo_dir = activated_repos_dir / "alice" / "myrepo"
        (repo_dir / ".code-indexer" / "tantivy_index").mkdir(parents=True)

        fast_app = FastAPI()
        fast_app.state.payload_cache = None
        fast_app.state.access_filtering_service = None
        fast_app.state.search_event_log_writer = None
        fast_app.state.query_tracker = real_query_tracker

        mock_activated_repo_manager = MagicMock()
        mock_activated_repo_manager.activated_repos_dir = str(activated_repos_dir)
        mock_activated_repo_manager.list_activated_repositories.return_value = [
            {"user_alias": "myrepo", "username": "alice", "is_global": False}
        ]

        mock_semantic_query_manager = MagicMock()

        register_query_routes(
            fast_app,
            semantic_query_manager=mock_semantic_query_manager,
            activated_repo_manager=mock_activated_repo_manager,
        )

        from code_indexer.server.auth import dependencies as auth_deps

        fast_app.dependency_overrides[auth_deps.get_current_user] = lambda: _make_user(
            "alice"
        )

        cfg_mock = MagicMock()
        cfg_mock.get_config.return_value.node_id = "test-node"
        monkeypatch.setattr(
            "code_indexer.server.routers.inline_query.get_config_service",
            lambda: cfg_mock,
            raising=False,
        )

        observed = {"refcount_during_call": None}

        class _StubTantivyManager:
            def __init__(self, index_dir):
                self._index_dir = index_dir

            def open_for_search(self):
                pass

            def search(self, **kwargs):
                observed["refcount_during_call"] = real_query_tracker.get_ref_count(
                    expected_key
                )
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
        assert observed["refcount_during_call"] == 1, (
            "Bug: pure FTS-mode REST query did not hold a QueryTracker "
            "refcount during the Tantivy search -- deactivation's drain "
            "would observe zero in-flight queries and could purge the "
            "activated repo's data mid-read."
        )
        assert real_query_tracker.get_ref_count(expected_key) == 0

    def test_alias_less_fts_response_labels_results_with_the_actually_selected_repo_not_the_first_one(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Codex round-6 LOW finding: the alias-less FTS response
        construction used activated_repos[0]["user_alias"] (the FIRST
        repo in the list) instead of fts_repo_alias (the repo whose FTS
        index was ACTUALLY selected). If the first repo lacks an FTS
        index and a later one is the real winner, results get mislabeled
        with the wrong repo's alias."""
        real_query_tracker = QueryTracker()
        activated_repos_dir = tmp_path / "activated-repos"

        # repo-without-fts is FIRST in the list but has NO tantivy_index
        # -- it must never be selected or used for labeling.
        no_fts_repo_dir = activated_repos_dir / "alice" / "repo-without-fts"
        no_fts_repo_dir.mkdir(parents=True)

        # repo-with-fts is SECOND but is the genuine FTS-available repo.
        fts_repo_dir = activated_repos_dir / "alice" / "repo-with-fts"
        (fts_repo_dir / ".code-indexer" / "tantivy_index").mkdir(parents=True)

        fast_app = FastAPI()
        fast_app.state.payload_cache = None
        fast_app.state.access_filtering_service = None
        fast_app.state.search_event_log_writer = None
        fast_app.state.query_tracker = real_query_tracker

        mock_activated_repo_manager = MagicMock()
        mock_activated_repo_manager.activated_repos_dir = str(activated_repos_dir)
        mock_activated_repo_manager.list_activated_repositories.return_value = [
            {"user_alias": "repo-without-fts", "username": "alice", "is_global": False},
            {"user_alias": "repo-with-fts", "username": "alice", "is_global": False},
        ]

        mock_semantic_query_manager = MagicMock()

        register_query_routes(
            fast_app,
            semantic_query_manager=mock_semantic_query_manager,
            activated_repo_manager=mock_activated_repo_manager,
        )

        from code_indexer.server.auth import dependencies as auth_deps

        fast_app.dependency_overrides[auth_deps.get_current_user] = lambda: _make_user(
            "alice"
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
                return [
                    {
                        "path": "src/a.py",
                        "line_start": 1,
                        "line_end": 2,
                        "snippet": "match",
                        "language": "python",
                    }
                ]

        monkeypatch.setattr(
            "code_indexer.services.tantivy_index_manager.TantivyIndexManager",
            _StubTantivyManager,
        )

        client = TestClient(fast_app, raise_server_exceptions=False)
        response = client.post(
            "/api/query",
            json={
                "query_text": "find auth",
                # Deliberately alias-less -- an omni request.
                "search_mode": "fts",
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        fts_results = body.get("fts_results") or []
        assert len(fts_results) == 1
        assert fts_results[0]["repository_alias"] == "repo-with-fts", (
            "Bug: alias-less FTS response labeled its result with "
            f"{fts_results[0]['repository_alias']!r} instead of the "
            "actually-selected repo 'repo-with-fts' -- it used the "
            "FIRST repo in the list rather than the one whose FTS "
            "index was genuinely read."
        )

    def test_refcount_is_held_during_alias_less_fts_mode_query_using_resolved_repo_alias(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Codex HIGH finding (round 5): an alias-less (omni) FTS request
        has request.repository_alias == None, which is a NO-OP key for
        track_activated_repo_query -- but the handler still resolves and
        reads ONE concrete activated repo's Tantivy index (the first
        with an available FTS index). The refcount must be held under
        THAT repo's own resolved alias, mirroring the per-repository-
        identity tracking pattern semantic_query_manager.py's fan-out
        already established (round 4), never the possibly-absent
        request-level repository_alias."""
        real_query_tracker = QueryTracker()
        activated_repos_dir = tmp_path / "activated-repos"
        expected_key = str(activated_repos_dir / "alice" / "myrepo")

        repo_dir = activated_repos_dir / "alice" / "myrepo"
        (repo_dir / ".code-indexer" / "tantivy_index").mkdir(parents=True)

        fast_app = FastAPI()
        fast_app.state.payload_cache = None
        fast_app.state.access_filtering_service = None
        fast_app.state.search_event_log_writer = None
        fast_app.state.query_tracker = real_query_tracker

        mock_activated_repo_manager = MagicMock()
        mock_activated_repo_manager.activated_repos_dir = str(activated_repos_dir)
        mock_activated_repo_manager.list_activated_repositories.return_value = [
            {"user_alias": "myrepo", "username": "alice", "is_global": False}
        ]

        mock_semantic_query_manager = MagicMock()

        register_query_routes(
            fast_app,
            semantic_query_manager=mock_semantic_query_manager,
            activated_repo_manager=mock_activated_repo_manager,
        )

        from code_indexer.server.auth import dependencies as auth_deps

        fast_app.dependency_overrides[auth_deps.get_current_user] = lambda: _make_user(
            "alice"
        )

        cfg_mock = MagicMock()
        cfg_mock.get_config.return_value.node_id = "test-node"
        monkeypatch.setattr(
            "code_indexer.server.routers.inline_query.get_config_service",
            lambda: cfg_mock,
            raising=False,
        )

        observed = {"refcount_during_call": None}

        class _StubTantivyManager:
            def __init__(self, index_dir):
                self._index_dir = index_dir

            def open_for_search(self):
                pass

            def search(self, **kwargs):
                observed["refcount_during_call"] = real_query_tracker.get_ref_count(
                    expected_key
                )
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
                # Deliberately NO repository_alias -- an alias-less/omni
                # request.
                "search_mode": "fts",
            },
        )

        assert response.status_code == 200, response.text
        assert observed["refcount_during_call"] == 1, (
            "Bug: an alias-less FTS-mode REST query did not hold a "
            "QueryTracker refcount under the resolved repo's OWN alias "
            "during the Tantivy search -- deactivation's drain would "
            "observe zero in-flight queries for this repo and could "
            "purge its data mid-read."
        )
        assert real_query_tracker.get_ref_count(expected_key) == 0
