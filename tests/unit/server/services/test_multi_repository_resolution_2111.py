"""Bug #2111 / Story #2117: multi-repository doors and repository resolution.

- One row per repository the search resolved, with its own count.
- `repository_failed` only for a repository that resolved and then failed.
- An alias that never resolves is a rejection: no row.
- A whole-call failure after resolution: one failure row per resolved
  repository, carrying the exception's class. Before any repository resolved:
  no row.

The REAL MultiSearchService (POST /api/query/multi) and SCIPMultiService
(POST /api/scip/multi/definition) run, including their per-repository
fan-out and error collection; only the repository path lookup (needs the
whole server registry) and, for the text search, the per-repository engine
are replaced. The SCIP engine is real over a real SCIP fixture index. Real
writer over the real stores (SQLite and PostgreSQL).
"""

import shutil
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Tuple
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

_SCIP_FIXTURE = Path(__file__).parents[3] / "scip/fixtures/comprehensive_index.scip.db"
_REPOS = ["first-repo", "second-repo", "broken-repo", "nope"]


@pytest.fixture(params=_KINDS)
def rec(request, tmp_path: Path) -> Iterator[Recorder]:
    with recorder(request.param, tmp_path) as real:
        yield real


def _rows(rec: Recorder) -> List[Tuple[Any, ...]]:
    return sorted(
        (r["repo_alias"], r["result_count"], r["outcome"], r["error_class"])
        for r in rec.rows()
    )


def _resolver(tmp_path: Path) -> Any:
    """The repository path lookup: every alias but `nope` resolves."""

    def _get_repository_path(self: Any, repo_id: str) -> str:
        if repo_id == "nope":
            from code_indexer.server.multi.models import UnresolvedRepositoryError

            raise UnresolvedRepositoryError(
                f"Repository '{repo_id}' not found in global repositories"
            )
        path = tmp_path / repo_id
        path.mkdir(exist_ok=True)
        return str(path)

    return _get_repository_path


def _text_search(self: Any, repo_id: str, request: Any) -> List[Dict[str, Any]]:
    """Per-repository engine stand-in: resolves the repository first, as the
    real per-repository searches do; broken-repo fails after resolving."""
    self._get_repository_path(repo_id)
    if repo_id == "broken-repo":
        raise RuntimeError("index unreadable")
    hit = {"file_path": "src/a.py", "score": 0.9, "content": "def a(): pass"}
    return [hit, hit] if repo_id == "first-repo" else []


def _client(rec: Recorder, router: Any) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from code_indexer.server.auth.dependencies import get_current_user

    app = FastAPI()
    app.include_router(router)
    app.state.search_event_log_writer = rec.writer
    app.state.access_filtering_service = SimpleNamespace(is_admin_user=lambda u: True)
    app.dependency_overrides[get_current_user] = lambda: make_user("gina")
    return TestClient(app)


def _raising(*_: Any, **__: Any) -> Any:
    raise RuntimeError("whole call failed")


def _multi_query(
    rec: Recorder,
    tmp_path: Path,
    failing_step: str = "",
    search: Any = _text_search,
    repositories: Any = None,
) -> Any:
    from code_indexer.server.multi.multi_search_config import MultiSearchConfig
    from code_indexer.server.multi.multi_search_service import MultiSearchService
    from code_indexer.server.routes import multi_query_routes

    service = MultiSearchService(MultiSearchConfig())
    try:
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    MultiSearchService, "_get_repository_path", _resolver(tmp_path)
                )
            )
            stack.enter_context(
                patch.object(MultiSearchService, "_search_single_repo_sync", search)
            )
            stack.enter_context(
                patch.object(
                    multi_query_routes, "get_multi_search_service", lambda: service
                )
            )
            if failing_step:
                stack.enter_context(patch(failing_step, _raising))
            return _client(rec, multi_query_routes.router).post(
                "/api/query/multi",
                json={
                    "repositories": (
                        _REPOS if repositories is None else list(repositories)
                    ),
                    "query": "token refresh",
                    "search_type": "semantic",
                },
            )
    finally:
        service.thread_executor.shutdown(wait=True)


class TestMultiQuery:
    def test_resolved_repositories_get_rows_and_an_unresolved_alias_none(
        self, rec, tmp_path
    ):
        response = _multi_query(rec, tmp_path)
        assert response.status_code == 200, response.text

        assert _rows(rec) == [
            ("broken-repo", 0, "failure", "repository_failed"),
            ("first-repo", 2, "success", None),
            ("second-repo", 0, "success", None),
        ]

    def test_whole_call_failure_is_a_failure_row_per_resolved_repository(
        self, rec, tmp_path
    ):
        response = _multi_query(
            rec,
            tmp_path,
            "code_indexer.server.multi.multi_search_service."
            "MultiResultAggregator.aggregate",
        )
        assert response.status_code == 500, response.text

        assert _rows(rec) == [
            ("broken-repo", 0, "failure", "RuntimeError"),
            ("first-repo", 0, "failure", "RuntimeError"),
            ("second-repo", 0, "failure", "RuntimeError"),
        ]

    def test_whole_call_failure_before_any_repository_resolved_writes_nothing(
        self, rec, tmp_path
    ):
        # The request's time budget is computed before the fan-out starts.
        response = _multi_query(
            rec,
            tmp_path,
            "code_indexer.server.query.filtered_search.extension_overfetch_deadline",
        )
        assert response.status_code == 500, response.text

        assert _rows(rec) == []


def _cache_search(self: Any, repo_id: str, request: Any) -> List[Dict[str, Any]]:
    """Per-repository engine stand-in that embeds the query through the
    real call-site helper: first-repo's lookup hits the embedding cache,
    second-repo's misses."""
    from code_indexer.storage.filesystem_vector_store import (
        _write_embed_meta_to_event_ctx,
    )

    self._get_repository_path(repo_id)
    _write_embed_meta_to_event_ctx(
        SimpleNamespace(
            search_event_cache_hit=repo_id == "first-repo",
            search_event_cache_mode="shortcut",
            provider_latency_ms=7,
            # Unclassified: the durable embed-event emission is a no-op.
            role=None,
            outcome=None,
        ),
        "voyage-ai",
    )
    return [{"file_path": "src/a.py", "score": 0.9, "content": "def a(): pass"}]


class TestPerRepositoryCacheFields:
    def test_each_repository_row_carries_its_own_cache_lookup(self, rec, tmp_path):
        response = _multi_query(
            rec,
            tmp_path,
            search=_cache_search,
            repositories=["first-repo", "second-repo"],
        )
        assert response.status_code == 200, response.text

        hits = {r["repo_alias"]: r["voyage_cache_hit"] for r in rec.rows()}
        assert {alias: bool(hit) for alias, hit in hits.items()} == {
            "first-repo": True,
            "second-repo": False,
        }
        assert None not in hits.values()


def _scip_repos(tmp_path: Path) -> None:
    """first-repo and second-repo hold the real SCIP fixture index;
    broken-repo holds an unreadable one."""
    for name in ("first-repo", "second-repo", "broken-repo"):
        scip_dir = tmp_path / name / ".code-indexer" / "scip"
        scip_dir.mkdir(parents=True)
        if name == "broken-repo":
            (scip_dir / "index.scip.db").write_bytes(b"not a database")
        else:
            shutil.copy(_SCIP_FIXTURE, scip_dir / "index.scip.db")


def _multi_scip(rec: Recorder, tmp_path: Path, failing_step: str = "") -> Any:
    from code_indexer.server.multi.scip_multi_service import SCIPMultiService
    from code_indexer.server.routes import scip_multi_routes

    _scip_repos(tmp_path)
    service = SCIPMultiService(max_workers=2)
    try:
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    SCIPMultiService, "_get_repository_path", _resolver(tmp_path)
                )
            )
            stack.enter_context(
                patch.object(
                    scip_multi_routes, "get_scip_multi_service", lambda: service
                )
            )
            if failing_step:
                stack.enter_context(patch(failing_step, _raising))
            return _client(rec, scip_multi_routes.router).post(
                "/api/scip/multi/definition",
                json={"repositories": _REPOS, "symbol": "Calculator"},
            )
    finally:
        service.thread_executor.shutdown(wait=True)


class TestMultiScip:
    def test_resolved_repositories_get_rows_and_an_unresolved_alias_none(
        self, rec, tmp_path
    ):
        response = _multi_scip(rec, tmp_path)
        assert response.status_code == 200, response.text
        results = response.json()["results"]

        by_repo = {r[0]: r[1:] for r in _rows(rec)}
        assert sorted(by_repo) == ["broken-repo", "first-repo", "second-repo"]
        assert by_repo["broken-repo"] == (0, "failure", "repository_failed")
        for name in ("first-repo", "second-repo"):
            assert by_repo[name] == (len(results[name]), "success", None)
        assert by_repo["first-repo"][0] > 0

    def test_whole_call_failure_is_a_failure_row_per_resolved_repository(
        self, rec, tmp_path
    ):
        response = _multi_scip(
            rec,
            tmp_path,
            "code_indexer.server.multi.scip_multi_service.SCIPMultiMetadata",
        )
        assert response.status_code == 500, response.text

        assert _rows(rec) == [
            ("broken-repo", 0, "failure", "RuntimeError"),
            ("first-repo", 0, "failure", "RuntimeError"),
            ("second-repo", 0, "failure", "RuntimeError"),
        ]
