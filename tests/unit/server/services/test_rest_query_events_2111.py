"""Bug #2111 / Story #2117 through REST POST /api/query.

- A query without repository_alias searches every repository the caller
  can search: one row per repository it searched (that repository's own
  result count); a repository that failed (degraded) gets a failure row.
- A query that fails records a failure row (the exception's class); an
  asynchronous job that raises does too.
- A request the door rejects itself before searching records nothing.
- Every row carries tool "POST /api/query" and door "rest".

The REAL route and the REAL SemanticQueryManager.query_user_repositories
run; only `_perform_search` (provider-backed) is a stand-in. Real writer over
the real stores (SQLite and PostgreSQL).
"""

from pathlib import Path
from typing import Any, Dict, Iterator, List
from unittest.mock import patch

import pytest

from code_indexer.server.query.semantic_query_manager import (
    QueryResult,
    SemanticQueryManager,
)
from tests.unit.server.services.search_event_real_stores import (
    PG_AVAILABLE,
    PG_SKIP_REASON,
)
from tests.unit.server.services.search_event_recording_harness import (
    FakeBackgroundJobManager,
    Recorder,
    make_user,
    recorder,
)

_KINDS = [
    "sqlite",
    pytest.param(
        "postgres", marks=pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON)
    ),
]

_ALIASES = ["repo-a", "repo-b", "repo-c"]


class _ActivatedRepos:
    """ActivatedRepoManager stand-in with three activated repositories."""

    def __init__(self, directory: Path) -> None:
        self.activated_repos_dir = str(directory)

    def list_activated_repositories(self, username: str) -> List[Dict[str, Any]]:
        return [
            {"user_alias": a, "username": username, "golden_repo_alias": a}
            for a in _ALIASES
        ]

    def get_activated_repo_path(self, username: str, alias: str) -> str:
        return self.activated_repos_dir

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        return alias in _ALIASES

    def get_activation_id(self, username: str, alias: str) -> str:
        return f"activation-{alias}"


def _result(alias: str) -> QueryResult:
    return QueryResult(
        file_path="src/m.py",
        line_number=1,
        code_snippet="def m(): pass",
        similarity_score=0.9,
        repository_alias=alias,
    )


def _three_repo_search(*args: Any, **kwargs: Any) -> Any:
    """repo-a 2 results, repo-b 1, repo-c hard-failed (degraded). Like the
    real per-repository loop, it notes each repository it resolves and
    searches (searching_resolved_repository)."""
    from code_indexer.server.services.search_event_context import (
        searching_resolved_repository,
    )

    for alias in _ALIASES:
        with searching_resolved_repository(alias):
            pass
    kwargs["_degraded_repos_out"].append("repo-c")
    return [_result("repo-a"), _result("repo-a"), _result("repo-b")], "primary_only"


class _Failed(Exception):
    pass


def _failing_search(*args: Any, **kwargs: Any) -> Any:
    # The real query_user_repositories wraps any other exception of the
    # search as SearchFailedError -- the class the row records.
    raise _Failed("storage unreadable at /srv/example")


@pytest.fixture(params=_KINDS)
def rec(request, tmp_path: Path) -> Iterator[Recorder]:
    with recorder(request.param, tmp_path) as real:
        yield real


def _client(
    rec: Recorder,
    tmp_path: Path,
    perform_search: Any = None,
    jobs: Any = None,
    single_repo: Any = None,
    repos: Any = None,
):
    """`perform_search` replaces the whole search; None keeps the REAL
    per-repository loop, with `single_repo` replacing only the engine that
    searches one repository. `repos` replaces the activated repositories
    (default: repo-a, repo-b, repo-c)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from code_indexer.server.auth import dependencies
    from code_indexer.server.routers.inline_query import register_query_routes

    repos = repos if repos is not None else _ActivatedRepos(tmp_path)
    manager = SemanticQueryManager(
        data_dir=str(tmp_path / "data"),
        activated_repo_manager=repos,  # type: ignore[arg-type]
        background_job_manager=jobs,  # type: ignore[arg-type]
    )
    if perform_search is not None:
        manager._perform_search = perform_search  # type: ignore[method-assign]
    if single_repo is not None:
        manager._search_single_repository = single_repo  # type: ignore[method-assign]
    app = FastAPI()
    register_query_routes(
        app, semantic_query_manager=manager, activated_repo_manager=repos
    )
    app.state.search_event_log_writer = rec.writer
    app.dependency_overrides[dependencies.get_current_user] = lambda: make_user("alice")
    return TestClient(app, raise_server_exceptions=False)


def _by_repo(rows: List[Dict[str, Any]]) -> Dict[Any, tuple]:
    return {
        r["repo_alias"]: (r["result_count"], r["outcome"], r["error_class"])
        for r in rows
    }


class TestSyncQuery:
    def test_query_over_every_repository_writes_one_row_per_repository(
        self, rec, tmp_path
    ):
        response = _client(rec, tmp_path, _three_repo_search).post(
            "/api/query", json={"query_text": "token refresh"}
        )

        assert response.status_code == 200, response.text
        rows = rec.rows()
        assert _by_repo(rows) == {
            "repo-a": (2, "success", None),
            "repo-b": (1, "success", None),
            "repo-c": (0, "failure", "repository_failed"),
        }
        assert {(r["tool"], r["door"]) for r in rows} == {("POST /api/query", "rest")}
        assert len({r["correlation_id"] for r in rows}) == 1

    def test_single_repository_query_keeps_one_row(self, rec, tmp_path):
        def _one(*args: Any, **kwargs: Any) -> Any:
            return [_result("repo-b")], "primary_only"

        response = _client(rec, tmp_path, _one).post(
            "/api/query", json={"query_text": "q", "repository_alias": "repo-b"}
        )

        assert response.status_code == 200, response.text
        assert _by_repo(rec.rows()) == {"repo-b": (1, "success", None)}

    def test_failed_query_records_a_failure(self, rec, tmp_path):
        response = _client(rec, tmp_path, _failing_search).post(
            "/api/query", json={"query_text": "q", "repository_alias": "repo-a"}
        )

        assert response.status_code == 500, response.text
        [row] = rec.rows()
        assert (row["repo_alias"], row["outcome"], row["error_class"]) == (
            "repo-a",
            "failure",
            "SearchFailedError",
        )

    def test_unknown_repository_records_nothing(self, rec, tmp_path):
        """Story #2117: a request rejected before its repository resolved
        writes no row."""
        response = _client(rec, tmp_path, _three_repo_search).post(
            "/api/query", json={"query_text": "q", "repository_alias": "nope"}
        )

        assert response.status_code == 404, response.text
        assert rec.rows() == []

    def test_rejected_async_mode_records_nothing(self, rec, tmp_path):
        response = _client(rec, tmp_path, _three_repo_search).post(
            "/api/query",
            json={"query_text": "q", "search_mode": "fts", "async_query": True},
        )

        assert response.status_code == 400, response.text
        assert rec.rows() == []

    def test_unknown_fts_repository_rejected_by_the_door_records_nothing(
        self, rec, tmp_path
    ):
        response = _client(rec, tmp_path, _three_repo_search).post(
            "/api/query",
            json={"query_text": "q", "search_mode": "fts", "repository_alias": "nope"},
        )

        assert response.status_code == 404, response.text
        assert rec.rows() == []


def _providers_unavailable(*args: Any, **kwargs: Any) -> Any:
    """What the search raises once the repository resolved and every
    embedding provider turned out to be unavailable (Bug #1804)."""
    from code_indexer.server.query.semantic_query_manager import SemanticQueryError

    raise SemanticQueryError(
        "Semantic search failed for repository 'repo-a' -- every configured "
        "embedding provider is currently unavailable (down/sinbinned): voyage-ai"
    )


class TestProvidersUnavailable:
    """Story #2117: a search that ran after its repository resolved and found
    every provider unavailable is a failure, never a rejection."""

    def test_sync_query_records_a_failure(self, rec, tmp_path):
        response = _client(rec, tmp_path, _providers_unavailable).post(
            "/api/query", json={"query_text": "q", "repository_alias": "repo-a"}
        )

        assert response.status_code >= 500, response.text
        [row] = rec.rows()
        assert (row["repo_alias"], row["outcome"]) == ("repo-a", "failure")
        assert row["error_class"]

    def test_async_job_records_a_failure(self, rec, tmp_path):
        jobs = FakeBackgroundJobManager()
        _client(rec, tmp_path, _providers_unavailable, jobs=jobs).post(
            "/api/query",
            json={"query_text": "q", "repository_alias": "repo-a", "async_query": True},
        )
        with pytest.raises(Exception):
            jobs.run_pending()

        [row] = rec.rows()
        assert (row["repo_alias"], row["outcome"]) == ("repo-a", "failure")
        assert row["error_class"]


class _FtsEngine:
    """TantivyIndexManager stand-in (the FTS engine is not under test): three
    hits from the index it was opened on."""

    def __init__(self, index_dir: Any) -> None:
        self.index_dir = index_dir

    def open_for_search(self) -> None:
        return None

    def search(self, **_: Any) -> List[Dict[str, Any]]:
        return [
            {"path": f"src/m{i}.py", "line_start": 1, "line_end": 2, "snippet": "x"}
            for i in range(3)
        ]


class TestFtsAndHybrid:
    """G: a REST FTS or hybrid request without an alias records the
    repository whose FTS index it searched, its FTS hits counted toward it.
    Every alias resolves to the same activated directory, so the FTS
    repository is the first one listed (repo-a)."""

    def _post(self, rec: Recorder, tmp_path: Path, search_mode: str) -> Any:
        (tmp_path / ".code-indexer" / "tantivy_index").mkdir(parents=True)
        with patch(
            "code_indexer.services.tantivy_index_manager.TantivyIndexManager",
            _FtsEngine,
        ):
            return _client(rec, tmp_path, _three_repo_search).post(
                "/api/query", json={"query_text": "q", "search_mode": search_mode}
            )

    def test_fts_only_records_the_searched_repository(self, rec, tmp_path):
        response = self._post(rec, tmp_path, "fts")

        assert response.status_code == 200, response.text
        assert _by_repo(rec.rows()) == {"repo-a": (3, "success", None)}

    def test_hybrid_counts_fts_hits_toward_their_repository(self, rec, tmp_path):
        response = self._post(rec, tmp_path, "hybrid")

        assert response.status_code == 200, response.text
        assert _by_repo(rec.rows()) == {
            "repo-a": (5, "success", None),
            "repo-b": (1, "success", None),
            "repo-c": (0, "failure", "repository_failed"),
        }


class TestAsyncQuery:
    def test_async_job_that_raises_records_a_failure(self, rec, tmp_path):
        jobs = FakeBackgroundJobManager()
        response = _client(rec, tmp_path, _failing_search, jobs=jobs).post(
            "/api/query",
            json={"query_text": "q", "repository_alias": "repo-a", "async_query": True},
        )
        assert response.status_code == 202, response.text
        assert rec.rows() == []

        with pytest.raises(Exception):
            jobs.run_pending()

        [row] = rec.rows()
        assert (row["tool"], row["outcome"], row["error_class"]) == (
            "POST /api/query",
            "failure",
            "SearchFailedError",
        )

    def test_async_job_for_an_unknown_repository_records_nothing(self, rec, tmp_path):
        jobs = FakeBackgroundJobManager()
        response = _client(rec, tmp_path, _three_repo_search, jobs=jobs).post(
            "/api/query",
            json={"query_text": "q", "repository_alias": "nope", "async_query": True},
        )
        assert response.status_code == 202, response.text

        with pytest.raises(Exception, match="nope"):
            jobs.run_pending()
        assert rec.rows() == []

    def test_async_job_over_every_repository_writes_one_row_each(self, rec, tmp_path):
        jobs = FakeBackgroundJobManager()
        _client(rec, tmp_path, _three_repo_search, jobs=jobs).post(
            "/api/query", json={"query_text": "q", "async_query": True}
        )
        jobs.run_pending()

        assert _by_repo(rec.rows()) == {
            "repo-a": (2, "success", None),
            "repo-b": (1, "success", None),
            "repo-c": (0, "failure", "repository_failed"),
        }


def _embed(hit: bool) -> None:
    """Embed the query through the real call-site helper (a cache hit or a
    miss), as the per-repository engine does."""
    from types import SimpleNamespace

    from code_indexer.storage.filesystem_vector_store import (
        _write_embed_meta_to_event_ctx,
    )

    _write_embed_meta_to_event_ctx(
        SimpleNamespace(
            search_event_cache_hit=hit,
            search_event_cache_mode="shortcut",
            provider_latency_ms=7,
            role=None,
            outcome=None,
        ),
        "voyage-ai",
    )


def _repo_a_then_timeout(
    repo_path: str, repository_alias: str, *args: Any, **kwargs: Any
) -> List[QueryResult]:
    """Per-repository engine: repo-a is searched, then repo-b times out. A
    per-repository timeout fails the WHOLE call (the real loop re-raises
    it), so repo-a's results are never delivered; the loop never reaches
    repo-c."""
    if repository_alias == "repo-b":
        raise TimeoutError("search exceeded its deadline")
    return [_result(repository_alias)]


def _repo_a_hits(
    repo_path: str, repository_alias: str, *args: Any, **kwargs: Any
) -> List[QueryResult]:
    """Per-repository engine: repo-a's query embedding hits the cache, the
    others miss."""
    _embed(repository_alias == "repo-a")
    return [_result(repository_alias)]


class TestAliasFreeFailureAfterResolution:
    """Finding 4: a query without an alias whose whole call fails after it
    resolved some repositories writes one failure row per resolved
    repository (ruling b), all with the call's class; the repository the
    search never reached writes none."""

    def _assert_failure_rows(self, rows: List[Dict[str, Any]]) -> None:
        by_repo = _by_repo(rows)
        assert sorted(by_repo) == ["repo-a", "repo-b"], by_repo
        assert {(count, outcome) for count, outcome, _ in by_repo.values()} == {
            (0, "failure")
        }
        assert len({error for _, _, error in by_repo.values()}) == 1
        assert None not in {error for _, _, error in by_repo.values()}

    def test_sync_query(self, rec, tmp_path):
        response = _client(rec, tmp_path, single_repo=_repo_a_then_timeout).post(
            "/api/query", json={"query_text": "q"}
        )

        assert response.status_code >= 500, response.text
        self._assert_failure_rows(rec.rows())

    def test_async_job(self, rec, tmp_path):
        jobs = FakeBackgroundJobManager()
        _client(rec, tmp_path, jobs=jobs, single_repo=_repo_a_then_timeout).post(
            "/api/query", json={"query_text": "q", "async_query": True}
        )
        with pytest.raises(Exception):
            jobs.run_pending()

        self._assert_failure_rows(rec.rows())

    def test_each_repository_row_carries_its_own_cache_lookup(self, rec, tmp_path):
        """Finding 1 on the sequential loop: repo-a hit, the others missed."""
        response = _client(rec, tmp_path, single_repo=_repo_a_hits).post(
            "/api/query", json={"query_text": "q"}
        )

        assert response.status_code == 200, response.text
        hits = {r["repo_alias"]: r["voyage_cache_hit"] for r in rec.rows()}
        assert None not in hits.values()
        assert {alias: bool(hit) for alias, hit in hits.items()} == {
            "repo-a": True,
            "repo-b": False,
            "repo-c": False,
        }


class _FailingFtsEngine(_FtsEngine):
    def search(self, **_: Any) -> List[Dict[str, Any]]:
        raise RuntimeError("index unreadable")


class TestFtsFailure:
    def test_alias_free_fts_failure_carries_the_fts_repository(self, rec, tmp_path):
        """Finding 4: the FTS repository is noted before its index is read,
        so a failed FTS search records it."""
        (tmp_path / ".code-indexer" / "tantivy_index").mkdir(parents=True)
        with patch(
            "code_indexer.services.tantivy_index_manager.TantivyIndexManager",
            _FailingFtsEngine,
        ):
            response = _client(rec, tmp_path, _three_repo_search).post(
                "/api/query", json={"query_text": "q", "search_mode": "fts"}
            )

        assert response.status_code == 500, response.text
        assert _by_repo(rec.rows()) == {"repo-a": (0, "failure", "RuntimeError")}

    def test_hybrid_semantic_half_failure_marks_its_repositories_failed(
        self, rec, tmp_path
    ):
        """A hybrid search whose semantic half failed on every repository
        (the door answers with the FTS hits): the FTS repository is a
        success with its hits, the others resolved and failed."""

        def _every_repository_fails(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("index unreadable")

        (tmp_path / ".code-indexer" / "tantivy_index").mkdir(parents=True)
        with patch(
            "code_indexer.services.tantivy_index_manager.TantivyIndexManager",
            _FtsEngine,
        ):
            response = _client(rec, tmp_path, single_repo=_every_repository_fails).post(
                "/api/query", json={"query_text": "q", "search_mode": "hybrid"}
            )

        assert response.status_code == 200, response.text
        assert _by_repo(rec.rows()) == {
            "repo-a": (3, "success", None),
            "repo-b": (0, "failure", "repository_failed"),
            "repo-c": (0, "failure", "repository_failed"),
        }


class _Repos(_ActivatedRepos):
    """Activated repositories `entries` (dicts as list_activated_repositories
    returns them), rooted under tmp_path/activated so the golden alias
    directory the loop opens for a global entry is tmp_path/golden-repos."""

    def __init__(self, tmp_path: Path, entries: List[Dict[str, Any]]) -> None:
        directory = tmp_path / "activated"
        directory.mkdir(exist_ok=True)
        super().__init__(directory)
        self._entries = entries

    def list_activated_repositories(self, username: str) -> List[Dict[str, Any]]:
        return [dict(entry, username=username) for entry in self._entries]


def _one_result(
    repo_path: str, repository_alias: str, *args: Any, **kwargs: Any
) -> List[QueryResult]:
    return [_result(repository_alias)]


class TestOnlySearchedRepositoriesGetRows:
    """Round-3 ruling: an alias-free search writes rows only for repositories
    that resolved AND were searched. A global alias that never resolves, and
    a repository the time budget never reached, write nothing; the answer is
    unchanged."""

    def test_unresolvable_global_alias_writes_no_row(self, rec, tmp_path):
        repos = _Repos(
            tmp_path,
            [
                {"user_alias": "repo-a", "golden_repo_alias": "repo-a"},
                {"user_alias": "repo-b-global", "is_global": True},
            ],
        )
        response = _client(rec, tmp_path, single_repo=_one_result, repos=repos).post(
            "/api/query", json={"query_text": "q"}
        )

        assert response.status_code == 200, response.text
        assert [r["repository_alias"] for r in response.json()["results"]] == ["repo-a"]
        assert _by_repo(rec.rows()) == {"repo-a": (1, "success", None)}

    def test_repository_the_budget_never_reached_writes_no_row(self, rec, tmp_path):
        repos = _Repos(
            tmp_path,
            [
                {"user_alias": alias, "golden_repo_alias": alias}
                for alias in ("repo-a", "repo-b", "repo-c")
            ],
        )
        searched: List[str] = []

        def _search(repo_path: str, repository_alias: str, *a: Any, **k: Any) -> Any:
            searched.append(repository_alias)
            return [_result(repository_alias)]

        with patch(
            "code_indexer.server.query.filtered_search.extension_budget_spent",
            lambda deadline: bool(searched),
        ):
            response = _client(rec, tmp_path, single_repo=_search, repos=repos).post(
                "/api/query", json={"query_text": "q"}
            )

        assert response.status_code == 200, response.text
        assert searched == ["repo-a"]
        assert _by_repo(rec.rows()) == {"repo-a": (1, "success", None)}
