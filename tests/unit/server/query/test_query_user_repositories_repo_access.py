"""Query searches only repositories the caller can access.

SemanticQueryManager.query_user_repositories builds the set of repositories
to search from the caller's activated repos plus the global repos. For a
non-admin caller, the global repos are narrowed to those the caller's group
can access BEFORE any search runs, so every consumer of the manager's output
-- the synchronous REST and MCP paths, and the background query job whose
result is returned verbatim by GET /api/jobs/{job_id} -- sees only rows,
counts and repository names from accessible repositories.

Real services throughout (see query_repo_access_env); only the external
embedding + HNSW search boundary is replaced.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

import pytest

from code_indexer.server.query.semantic_query_manager import SemanticQueryError
from code_indexer.server.services.repo_access_guard import (
    AccessFilteringServiceUnavailableError,
)
from tests.unit.server.query.query_repo_access_env import (
    ADMIN,
    ALL_REPOS,
    ALL_ROWS_LIMIT,
    GRANTED_REPO,
    GRANTED_REPO_2,
    GRANTED_REPOS,
    OWN_ACTIVATION,
    ROWS_PER_REPO,
    UNGRANTED_REPO,
    USER,
    QueryAccessEnv,
    build_server_db_template,
    global_alias,
    result_repos,
)

GRANTED_ALIASES = {global_alias(GRANTED_REPO), global_alias(GRANTED_REPO_2)}
ALL_ALIASES = GRANTED_ALIASES | {global_alias(UNGRANTED_REPO)}


@pytest.fixture(scope="module")
def server_db_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_server_db_template(tmp_path_factory.mktemp("server_db_template"))


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_db_template: Path
) -> Iterator[QueryAccessEnv]:
    # One embedding provider configured -> deterministic primary-only
    # routing, including for submit_query_job (which takes no strategy).
    monkeypatch.delenv("CO_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    e = QueryAccessEnv(tmp_path, server_db_template)
    try:
        yield e
    finally:
        e.close()


def _query(env: QueryAccessEnv, username: str, access_service, **kwargs):
    with env.installed(access_service):
        return env.query_manager.query_user_repositories(
            username=username,
            query_text="find main",
            query_strategy="primary_only",
            **kwargs,
        )


class TestNonAdminDefaultQuery:
    def test_rows_and_repositories_searched_cover_only_granted_repos(self, env):
        response = _query(env, USER, env.access_service, limit=ALL_ROWS_LIMIT)

        assert result_repos(response["results"]) == GRANTED_ALIASES
        assert response["query_metadata"]["repositories_searched"] == len(GRANTED_REPOS)
        assert env.searched_repos() == {GRANTED_REPO, GRANTED_REPO_2}

    def test_degraded_repos_never_names_an_ungranted_repo(self, env):
        env.failing_repos = {UNGRANTED_REPO}

        response = _query(env, USER, env.access_service, limit=ALL_ROWS_LIMIT)

        assert "degraded_repos" not in response["query_metadata"]
        assert result_repos(response["results"]) == GRANTED_ALIASES

    def test_granted_user_gets_a_full_limit_of_granted_rows(self, env):
        limit = ROWS_PER_REPO

        response = _query(env, USER, env.access_service, limit=limit)

        assert len(response["results"]) == limit
        assert result_repos(response["results"]) == {global_alias(GRANTED_REPO)}


class TestAsyncQueryJob:
    def test_job_result_contains_no_rows_from_ungranted_repos(self, env):
        with env.installed(env.access_service):
            job_id = env.query_manager.submit_query_job(
                username=USER, query_text="find main", limit=ALL_ROWS_LIMIT
            )
            status = env.wait_for_job(job_id, USER)

        assert status["status"] == "completed", status
        result = status["result"]
        assert result_repos(result["results"]) == GRANTED_ALIASES
        assert all(UNGRANTED_REPO not in r["code_snippet"] for r in result["results"])
        assert result["query_metadata"]["repositories_searched"] == len(GRANTED_REPOS)


class TestExplicitAlias:
    def test_ungranted_alias_is_refused_like_an_unknown_alias(self, env):
        with pytest.raises(SemanticQueryError) as ungranted:
            _query(
                env,
                USER,
                env.access_service,
                limit=10,
                repository_alias=global_alias(UNGRANTED_REPO),
            )
        with pytest.raises(SemanticQueryError) as unknown:
            _query(
                env,
                USER,
                env.access_service,
                limit=10,
                repository_alias="no-such-repo-global",
            )

        assert str(ungranted.value) == str(unknown.value).replace(
            "no-such-repo-global", global_alias(UNGRANTED_REPO)
        )
        assert "not found" in str(ungranted.value)
        assert env.searched_paths == []

    def test_granted_alias_is_searched(self, env):
        response = _query(
            env,
            USER,
            env.access_service,
            limit=10,
            repository_alias=global_alias(GRANTED_REPO),
        )

        assert result_repos(response["results"]) == {global_alias(GRANTED_REPO)}


class TestAdminAndMissingService:
    def test_admin_searches_every_global_repo_including_ungrouped_ones(self, env):
        response = _query(env, ADMIN, env.access_service, limit=ALL_ROWS_LIMIT)

        assert result_repos(response["results"]) == ALL_ALIASES
        assert response["query_metadata"]["repositories_searched"] == len(ALL_REPOS)

    def test_admin_explicit_alias_of_ungrouped_repo_is_searched(self, env):
        response = _query(
            env,
            ADMIN,
            env.access_service,
            limit=10,
            repository_alias=global_alias(UNGRANTED_REPO),
        )

        assert result_repos(response["results"]) == {global_alias(UNGRANTED_REPO)}


class TestMissingAccessService:
    """No access service (e.g. group access failed to initialize at startup):
    a query that would include any global repo is refused for every caller,
    admins included; one scoped to the caller's own activation still runs."""

    @pytest.mark.parametrize("username", [USER, ADMIN])
    def test_default_query_is_refused_and_logged(self, env, username, caplog):
        with caplog.at_level(logging.ERROR):
            with pytest.raises(AccessFilteringServiceUnavailableError):
                _query(env, username, None, limit=ALL_ROWS_LIMIT)

        assert env.searched_paths == []
        assert any(
            r.levelno == logging.ERROR and "QUERY-MIGRATE-014" in r.getMessage()
            for r in caplog.records
        )

    def test_explicit_global_alias_is_refused(self, env):
        with pytest.raises(AccessFilteringServiceUnavailableError):
            _query(
                env,
                USER,
                None,
                limit=10,
                repository_alias=global_alias(GRANTED_REPO),
            )

        assert env.searched_paths == []

    def test_own_activated_alias_still_runs(self, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)

        response = _query(
            env, USER, None, limit=ROWS_PER_REPO, repository_alias=OWN_ACTIVATION
        )

        assert result_repos(response["results"]) == {OWN_ACTIVATION}
        assert len(response["results"]) == ROWS_PER_REPO
        assert response["query_metadata"]["repositories_searched"] == 1

    def test_async_job_fails_without_storing_rows(self, env):
        with env.installed(None):
            job_id = env.query_manager.submit_query_job(
                username=USER, query_text="find main", limit=ALL_ROWS_LIMIT
            )
            status = env.wait_for_job(job_id, USER)

        assert status["status"] == "failed", status
        assert status["result"] is None
        assert "access control" in status["error"]
        assert env.searched_paths == []
