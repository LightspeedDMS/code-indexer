"""Story #2108: completed regex searches are recorded in search_event_log.

Each test drives a real front door (the MCP regex_search entry point, run on
a worker thread exactly as the dispatcher runs it, or POST /api/regex/search)
with REAL ripgrep over a small repository, then reads the row the real
SearchEventLogWriter wrote to a REAL store (SQLite and PostgreSQL).

Recording rules: one row per searched repository (an omni search fans out
one search per repository); query_text is the pattern; result_count is the
number of matches returned; a failed search writes no row.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator
from unittest.mock import patch

import pytest

from tests.unit.server.services.search_event_real_stores import (
    PG_AVAILABLE,
    PG_SKIP_REASON,
)
from tests.unit.server.services.search_event_recording_harness import (
    Recorder,
    make_repo,
    make_user,
    recorder,
)

_KINDS = [
    "sqlite",
    pytest.param(
        "postgres", marks=pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON)
    ),
]


class _ActivatedRepos:
    """ActivatedRepoManager stand-in: every alias is activated at `repo`."""

    def __init__(self, repo: Path) -> None:
        self._repo = repo

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        return True

    def get_activated_repo_path(self, username: str, alias: str) -> str:
        return str(self._repo)


class RegexEnv(SimpleNamespace):
    rec: Recorder
    repo: Path
    state: SimpleNamespace


@pytest.fixture(params=_KINDS)
def env(request, tmp_path) -> Iterator[RegexEnv]:
    repo = make_repo(tmp_path, files=3)
    with recorder(request.param, tmp_path) as rec:
        state = SimpleNamespace(
            search_event_log_writer=rec.writer,
            golden_repos_dir=str(tmp_path / "golden-repos"),
            payload_cache=None,
            access_filtering_service=SimpleNamespace(is_admin_user=lambda u: True),
        )
        app_module = SimpleNamespace(
            app=SimpleNamespace(state=state),
            activated_repo_manager=_ActivatedRepos(repo),
            golden_repo_manager=None,
        )
        with patch("code_indexer.server.mcp.handlers._utils.app_module", app_module):
            yield RegexEnv(rec=rec, repo=repo, state=state)


def _mcp_regex(args: Dict[str, Any], username: str) -> Dict[str, Any]:
    from code_indexer.server.mcp.handlers.search.regex_search import (
        handle_regex_search_sync,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(handle_regex_search_sync, args, make_user(username))
        return dict(json.loads(response.result()["content"][0]["text"]))


class TestMcpRegexSearch:
    def test_mcp_regex_search_records_one_regex_row(self, env):
        body = _mcp_regex(
            {"repository_alias": "example-repo", "pattern": r"def example_"}, "alice"
        )
        assert body["success"] is True, body
        assert len(body["matches"]) == 3

        [row] = env.rec.rows(search_type="regex")
        assert (row["username"], row["repo_alias"], row["result_count"]) == (
            "alice",
            "example-repo",
            3,
        )
        assert row["query_text"] == r"def example_"
        assert row["voyage_cache_hit"] is None
        assert row["cohere_cache_hit"] is None

    def test_mcp_omni_regex_records_one_row_per_repository(self, env):
        body = _mcp_regex(
            {
                "repository_alias": ["first-repo", "second-repo"],
                "pattern": r"def example_",
            },
            "bob",
        )
        assert body["repos_searched"] == 2, body

        rows = env.rec.rows(search_type="regex")
        assert sorted((r["repo_alias"], r["result_count"]) for r in rows) == [
            ("first-repo", 3),
            ("second-repo", 3),
        ]

    def test_mcp_failed_regex_search_records_no_row(self, env, monkeypatch):
        monkeypatch.setattr(
            _ActivatedRepos, "get_activated_repo_path", lambda self, u, a: None
        )

        body = _mcp_regex(
            {"repository_alias": "missing-repo", "pattern": r"def example_"}, "bob"
        )

        assert body["success"] is False
        assert env.rec.rows(search_type="regex") == []


def _register_aliases(env: RegexEnv, *aliases: str) -> None:
    """Register real golden-repo aliases pointing at the test repository."""
    from code_indexer.global_repos.alias_manager import AliasManager

    manager = AliasManager(str(Path(env.state.golden_repos_dir) / "aliases"))
    for alias in aliases:
        manager.create_alias(alias, str(env.repo))


def _rest_regex(env: RegexEnv, payload: Dict[str, Any], username: str) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from code_indexer.server.auth.dependencies import get_current_user
    from code_indexer.server.routes.regex_routes import router

    app = FastAPI()
    app.include_router(router)
    app.state.search_event_log_writer = env.state.search_event_log_writer
    app.state.access_filtering_service = env.state.access_filtering_service
    app.dependency_overrides[get_current_user] = lambda: make_user(username)
    return TestClient(app).post("/api/regex/search", json=payload)


class TestRestRegexSearch:
    def test_rest_regex_search_records_one_regex_row(self, env):
        _register_aliases(env, "example-repo-global")

        response = _rest_regex(
            env,
            {"repository_alias": "example-repo-global", "pattern": r"def example_"},
            "carol",
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["matches"]) == 3

        [row] = env.rec.rows(search_type="regex")
        assert (row["username"], row["repo_alias"], row["result_count"]) == (
            "carol",
            "example-repo-global",
            3,
        )
        assert row["query_text"] == r"def example_"

    def test_rest_omni_regex_records_one_row_per_repository(self, env):
        _register_aliases(env, "first-repo-global", "second-repo-global")

        response = _rest_regex(
            env,
            {
                "repository_alias": [
                    "first-repo-global",
                    "second-repo-global",
                    "unknown-repo-global",
                ],
                "pattern": r"def example_",
            },
            "dave",
        )
        assert response.status_code == 200, response.text
        assert response.json()["repos_searched"] == 2

        rows = env.rec.rows(search_type="regex")
        assert sorted((r["repo_alias"], r["result_count"]) for r in rows) == [
            ("first-repo-global", 3),
            ("second-repo-global", 3),
        ]
