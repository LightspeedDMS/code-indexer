"""Bug #2111 / Story #2117 through the MCP search_code front door.

- A bare alias promoted to its -global form (global-alias fallback) is
  recorded as the RESOLVED alias.
- A search over several repositories (list, JSON-array string, wildcard)
  writes one row per repository it searched, with that repository's own
  result count; a repository that failed gets a failure row; the rows share
  the call's correlation id.
- A search that raises records a failure row (the exception's class).
- Every row carries tool "search_code" and door "mcp".

The REAL search_code and _omni_search_code run; only the multi-repository
service, wildcard expansion and the single-repository search behind the
door are stand-ins (they need the whole server and indexed repositories).
Real SearchEventLogWriter over the real stores (SQLite and PostgreSQL).
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List
from unittest.mock import patch

import pytest

from tests.unit.server.services.search_event_real_stores import (
    PG_AVAILABLE,
    PG_SKIP_REASON,
)
from tests.unit.server.services.search_event_recording_harness import (
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

_HANDLERS = "code_indexer.server.mcp.handlers"


@pytest.fixture(params=_KINDS)
def rec(request, tmp_path: Path) -> Iterator[Recorder]:
    with recorder(request.param, tmp_path) as real:
        yield real


class _Repos:
    """ActivatedRepoManager / GoldenRepoManager stand-in: the user has no
    activated repository; every golden repository is globally active."""

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        return False

    def is_globally_active(self, alias: str) -> bool:
        return True


def _app_module(rec: Recorder) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                search_event_log_writer=rec.writer,
                access_filtering_service=None,
                payload_cache=None,
            )
        ),
        activated_repo_manager=_Repos(),
        golden_repo_manager=_Repos(),
    )


def _mcp(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _search(rec: Recorder, params: Dict[str, Any], **patches: Any) -> Dict[str, Any]:
    from code_indexer.server.mcp.handlers.search import search_code

    with patch(f"{_HANDLERS}._utils.app_module", _app_module(rec)):
        managers = [patch(target, value) for target, value in patches.items()]
        for manager in managers:
            manager.start()
        try:
            response = search_code(params, make_user("alice"))
        finally:
            for manager in reversed(managers):
                manager.stop()
    return dict(json.loads(response["content"][0]["text"]))


def _rows(rec: Recorder) -> List[Dict[str, Any]]:
    return rec.rows()


class TestSingleRepository:
    def test_promoted_bare_alias_is_recorded_as_global(self, rec):
        seen: List[str] = []

        def _global(params: Any, user: Any, alias: str) -> Dict[str, Any]:
            seen.append(alias)
            return _mcp({"success": True, "results": [{"file_path": "a.py"}] * 2})

        body = _search(
            rec,
            {
                "repository_alias": "example-repo",
                "query_text": "q",
                "search_mode": "fts",
            },
            **{f"{_HANDLERS}.search.code_search._search_global_repo": _global},
        )

        assert body["success"] is True
        assert seen == ["example-repo-global"]
        [row] = _rows(rec)
        assert (row["repo_alias"], row["result_count"]) == ("example-repo-global", 2)
        assert (row["tool"], row["door"], row["outcome"]) == (
            "search_code",
            "mcp",
            "success",
        )

    def test_search_that_raises_records_a_failure(self, rec):
        def _boom(params: Any, user: Any, alias: str) -> Dict[str, Any]:
            raise RuntimeError("index unreadable at /srv/example")

        body = _search(
            rec,
            {"repository_alias": "example-repo-global", "query_text": "q"},
            **{f"{_HANDLERS}.search.code_search._search_global_repo": _boom},
        )

        assert body["success"] is False
        [row] = _rows(rec)
        assert (row["repo_alias"], row["outcome"], row["error_class"]) == (
            "example-repo-global",
            "failure",
            "RuntimeError",
        )


class _FakeMultiSearch:
    """MultiSearchService stand-in: first repo 2 results, second 0, the
    third resolved and failed. Like the real service, it notes the
    repositories it resolved on the request's search event context."""

    def search(self, request: Any) -> Any:
        from code_indexer.server.multi.models import (
            MultiSearchMetadata,
            MultiSearchResponse,
        )
        from code_indexer.server.services.search_event_context import (
            note_current_repositories,
        )

        repos = list(request.repositories)
        note_current_repositories(searched=repos[:2], failed=[repos[2]])
        results: Dict[str, List[Dict[str, Any]]] = {
            repos[0]: [
                {"file_path": "a.py", "score": 0.9},
                {"file_path": "b.py", "score": 0.8},
            ],
            repos[1]: [],
        }
        return MultiSearchResponse(
            results=results,
            metadata=MultiSearchMetadata(
                total_results=2, total_repos_searched=2, execution_time_ms=1
            ),
            errors={repos[2]: "Search timed out"},
        )


_THREE = ["first-repo-global", "second-repo-global", "broken-repo-global"]


def _omni_patches(expanded: List[str]) -> Dict[str, Any]:
    return {
        f"{_HANDLERS}.search.omni._expand_wildcard_patterns": (
            lambda patterns, user, search_mode="semantic": list(expanded)
        ),
        "code_indexer.server.multi.multi_search_service.MultiSearchService.get_instance": (
            lambda *a, **k: _FakeMultiSearch()
        ),
    }


class TestMultiRepository:
    @pytest.mark.parametrize(
        "repository_alias",
        [_THREE, json.dumps(_THREE), "*-repo-global"],
        ids=["list", "json-array-string", "wildcard"],
    )
    def test_one_row_per_searched_repository(self, rec, repository_alias):
        body = _search(
            rec,
            {
                "repository_alias": repository_alias,
                "query_text": "token refresh",
                "search_mode": "fts",
            },
            **_omni_patches(_THREE),
        )

        assert body["success"] is True, body
        rows = _rows(rec)
        by_repo = {
            r["repo_alias"]: (r["result_count"], r["outcome"], r["error_class"])
            for r in rows
        }
        assert by_repo == {
            "first-repo-global": (2, "success", None),
            "second-repo-global": (0, "success", None),
            "broken-repo-global": (0, "failure", "repository_failed"),
        }
        assert len({r["correlation_id"] for r in rows}) == 1
        assert {(r["tool"], r["door"]) for r in rows} == {("search_code", "mcp")}

    def test_every_repository_failed_writes_repository_failed_rows(self, rec, tmp_path):
        """Every resolved repository failed: one repository_failed row each;
        the alias that never resolved writes none. The REAL MultiSearchService
        runs; only its path lookup and per-repository engine are replaced."""
        from code_indexer.server.multi.multi_search_config import MultiSearchConfig
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        def _get_repository_path(self: Any, repo_id: str) -> str:
            if repo_id == "nope":
                from code_indexer.server.multi.models import (
                    UnresolvedRepositoryError,
                )

                raise UnresolvedRepositoryError(f"Repository '{repo_id}' not found")
            return str(tmp_path)

        def _failing_search(self: Any, repo_id: str, request: Any) -> Any:
            self._get_repository_path(repo_id)
            raise RuntimeError("index unreadable")

        aliases = ["broken-a", "broken-b", "nope"]
        service = MultiSearchService(MultiSearchConfig())
        try:
            with patch.object(
                MultiSearchService, "_get_repository_path", _get_repository_path
            ):
                with patch.object(
                    MultiSearchService, "_search_single_repo_sync", _failing_search
                ):
                    _search(
                        rec,
                        {
                            "repository_alias": aliases,
                            "query_text": "q",
                            "search_mode": "fts",
                        },
                        **{
                            f"{_HANDLERS}.search.omni._expand_wildcard_patterns": (
                                lambda patterns, user, search_mode="semantic": list(
                                    aliases
                                )
                            ),
                            "code_indexer.server.multi.multi_search_service."
                            "MultiSearchService.get_instance": (
                                lambda *a, **k: service
                            ),
                        },
                    )
        finally:
            service.thread_executor.shutdown(wait=True)

        by_repo = {
            r["repo_alias"]: (r["result_count"], r["outcome"], r["error_class"])
            for r in _rows(rec)
        }
        assert by_repo == {
            "broken-a": (0, "failure", "repository_failed"),
            "broken-b": (0, "failure", "repository_failed"),
        }


class TestRejections:
    """Story #2117: a request rejected before its repository resolved writes
    no row."""

    def test_unknown_global_alias_records_nothing(self, rec, tmp_path):
        repo_search = f"{_HANDLERS}.search.repo_search"
        body = _search(
            rec,
            {"repository_alias": "nope-global", "query_text": "q"},
            **{
                f"{repo_search}._list_global_repos": lambda: [],
                f"{repo_search}._get_golden_repos_dir": lambda: str(tmp_path),
                f"{repo_search}._get_available_repos": lambda user: [],
            },
        )

        assert body["success"] is False
        assert body["error_code"] == "REPOSITORY_NOT_FOUND"
        assert _rows(rec) == []

    def test_providers_unavailable_after_resolution_records_a_failure(self, rec):
        """Item 5: the search ran on a resolved repository and every
        provider was unavailable (Bug #1804 payload) -- a failure row."""

        def _unavailable(params: Any, user: Any, alias: str) -> Dict[str, Any]:
            return _mcp(
                {
                    "success": True,
                    "results": {
                        "results": [],
                        "query_metadata": {"completeness": "providers_unavailable"},
                    },
                }
            )

        _search(
            rec,
            {"repository_alias": "example-repo-global", "query_text": "q"},
            **{f"{_HANDLERS}.search.code_search._search_global_repo": _unavailable},
        )

        [row] = _rows(rec)
        assert (row["repo_alias"], row["outcome"], row["error_class"]) == (
            "example-repo-global",
            "failure",
            "providers_unavailable",
        )

    def test_unknown_bare_alias_records_nothing(self, rec):
        from code_indexer.server.query.semantic_query_manager import (
            SearchRepositoryNotFoundError,
        )

        def _unknown(params: Any, user: Any) -> Dict[str, Any]:
            raise SearchRepositoryNotFoundError(params["repository_alias"])

        with patch.object(_Repos, "is_globally_active", lambda self, alias: False):
            body = _search(
                rec,
                {"repository_alias": "nope", "query_text": "q"},
                **{f"{_HANDLERS}.search.code_search._search_activated_repo": _unknown},
            )

        assert body["success"] is False
        assert _rows(rec) == []


class _ThreeActivated(_Repos):
    """ActivatedRepoManager stand-in with three activated repositories; each
    record names its golden source, as every stored activation does."""

    _ALIASES = ["repo-a", "repo-b", "repo-c"]

    def __init__(self, directory: Path) -> None:
        self.activated_repos_dir = str(directory)

    def list_activated_repositories(self, username: str) -> List[Dict[str, Any]]:
        return [
            {"user_alias": a, "username": username, "golden_repo_alias": a}
            for a in self._ALIASES
        ]

    def get_activated_repo_path(self, username: str, alias: str) -> str:
        return self.activated_repos_dir

    def get_activation_id(self, username: str, alias: str) -> str:
        return f"activation-{alias}"


def _repo_a_then_timeout(
    repo_path: str, repository_alias: str, *args: Any, **kwargs: Any
) -> List[Any]:
    """Per-repository engine: repo-a is searched, then repo-b times out,
    which fails the whole call (the real loop re-raises a timeout); the loop
    never reaches repo-c."""
    if repository_alias == "repo-b":
        raise TimeoutError("search exceeded its deadline")
    return []


class TestAliasFreeFailureAfterResolution:
    def test_whole_call_failure_writes_a_failure_row_per_resolved_repository(
        self, rec, tmp_path
    ):
        """Finding 4: a search_code call without an alias whose whole call
        fails after resolving repositories writes one failure row per
        resolved repository (ruling b), none for the one never reached."""
        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryManager,
        )

        repos = _ThreeActivated(tmp_path)
        manager = SemanticQueryManager(
            data_dir=str(tmp_path / "data"),
            activated_repo_manager=repos,  # type: ignore[arg-type]
        )
        manager._search_single_repository = _repo_a_then_timeout  # type: ignore[method-assign]
        module = _app_module(rec)
        module.activated_repo_manager = repos
        module.semantic_query_manager = manager

        with patch(f"{_HANDLERS}._utils.app_module", module):
            from code_indexer.server.mcp.handlers.search import search_code

            response = search_code(
                {"query_text": "q", "search_mode": "fts"}, make_user("alice")
            )

        assert json.loads(response["content"][0]["text"])["success"] is False
        by_repo = {
            r["repo_alias"]: (r["result_count"], r["outcome"], r["error_class"])
            for r in _rows(rec)
        }
        assert sorted(by_repo) == ["repo-a", "repo-b"], by_repo
        assert {(count, outcome) for count, outcome, _ in by_repo.values()} == {
            (0, "failure")
        }
        [error_class] = {error for _, _, error in by_repo.values()}
        assert error_class
        assert {(r["tool"], r["door"]) for r in _rows(rec)} == {("search_code", "mcp")}


class _ListedRepos(_ThreeActivated):
    """Activated repositories `entries`, rooted under tmp_path/activated so
    the golden alias directory the loop opens for a global entry is
    tmp_path/golden-repos."""

    def __init__(self, tmp_path: Path, entries: List[Dict[str, Any]]) -> None:
        directory = tmp_path / "activated"
        directory.mkdir(exist_ok=True)
        super().__init__(directory)
        self._entries = entries

    def list_activated_repositories(self, username: str) -> List[Dict[str, Any]]:
        return [dict(entry, username=username) for entry in self._entries]


def _alias_free_search(rec: Recorder, tmp_path: Path, repos: Any, engine: Any) -> Any:
    """search_code without an alias through the REAL _search_activated_repo
    and SemanticQueryManager loop; only the per-repository engine is a
    stand-in."""
    from code_indexer.server.query.semantic_query_manager import (
        SemanticQueryManager,
    )

    manager = SemanticQueryManager(
        data_dir=str(tmp_path / "data"),
        activated_repo_manager=repos,  # type: ignore[arg-type]
    )
    manager._search_single_repository = engine  # type: ignore[method-assign]
    module = _app_module(rec)
    module.activated_repo_manager = repos
    module.semantic_query_manager = manager
    with patch(f"{_HANDLERS}._utils.app_module", module):
        from code_indexer.server.mcp.handlers.search import search_code

        response = search_code(
            {"query_text": "q", "search_mode": "fts"}, make_user("alice")
        )
    return json.loads(response["content"][0]["text"])


def _one_result(repo_path: str, repository_alias: str, *a: Any, **k: Any) -> Any:
    from code_indexer.server.query.semantic_query_manager import QueryResult

    return [
        QueryResult(
            file_path="src/m.py",
            line_number=1,
            code_snippet="def m(): pass",
            similarity_score=0.9,
            repository_alias=repository_alias,
        )
    ]


class TestOnlySearchedRepositoriesGetRows:
    """Round-3 ruling: an alias-free search writes rows only for repositories
    that resolved AND were searched."""

    def _rows_by_repo(self, rec: Recorder) -> Dict[Any, Any]:
        return {
            r["repo_alias"]: (r["result_count"], r["outcome"], r["error_class"])
            for r in _rows(rec)
        }

    def test_unresolvable_global_alias_writes_no_row(self, rec, tmp_path):
        repos = _ListedRepos(
            tmp_path,
            [
                {"user_alias": "repo-a", "golden_repo_alias": "repo-a"},
                {"user_alias": "repo-b-global", "is_global": True},
            ],
        )
        body = _alias_free_search(rec, tmp_path, repos, _one_result)

        assert body["success"] is True, body
        assert self._rows_by_repo(rec) == {"repo-a": (1, "success", None)}

    def test_repository_the_budget_never_reached_writes_no_row(self, rec, tmp_path):
        repos = _ListedRepos(
            tmp_path,
            [
                {"user_alias": "repo-a", "golden_repo_alias": "repo-a"},
                {"user_alias": "repo-b", "golden_repo_alias": "repo-b"},
                {"user_alias": "repo-c", "golden_repo_alias": "repo-c"},
            ],
        )
        searched: List[str] = []

        def _search(repo_path: str, repository_alias: str, *a: Any, **k: Any) -> Any:
            searched.append(repository_alias)
            return _one_result(repo_path, repository_alias)

        with patch(
            "code_indexer.server.query.filtered_search.extension_budget_spent",
            lambda deadline: bool(searched),
        ):
            body = _alias_free_search(rec, tmp_path, repos, _search)

        assert body["success"] is True, body
        assert searched == ["repo-a"]
        assert self._rows_by_repo(rec) == {"repo-a": (1, "success", None)}
