"""#2047 (S21): the bounds on an extension-filtered search -- the time
budget, one HNSW index load per repository, the per-repository deadline
check and the repository-count cap -- and the store push-down of the
``any_ext`` condition on the server's semantic path.

Real SemanticQueryManager, SemanticSearchService, MultiSearchService,
FilesystemVectorStore and Tantivy index over a real git repo. Replaced: the
embedding provider (external service), authentication and the
activated-repo listing.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import PurePosixPath
from typing import Any, Iterator, List, Tuple
from unittest.mock import patch

import pytest

from code_indexer.services.language_mapper import LanguageMapper
from tests.unit.server.query import test_file_extensions_front_doors_2047 as _doors
from tests.unit.server.query.extension_filter_env_2047 import (
    CORPUS,
    QUERY,
    REPO_ALIAS,
    expected_semantic,
    passes,
    paths,
)

# The front-door module's corpus fixtures and helpers, shared (not copied).
env = _doors.env
rest = _doors.rest
_mcp_search = _doors._mcp_search
_rest_query = _doors._rest_query


def _set_handler_timeouts(
    monkeypatch: pytest.MonkeyPatch, mcp_seconds: int, rest_seconds: int
) -> None:
    from code_indexer.server.services.config_service import get_config_service

    timeouts = get_config_service().get_config().search_timeouts_config
    monkeypatch.setattr(timeouts, "search_code_handler_timeout_seconds", mcp_seconds)
    monkeypatch.setattr(timeouts, "rest_query_handler_timeout_seconds", rest_seconds)


def test_overfetch_budget_leaves_a_margin_inside_the_handler_timeout(
    monkeypatch,
) -> None:
    """The over-fetch rounds stop at 80% of the smaller handler timeout, so
    the client gets the short, logged answer instead of a handler timeout."""
    from code_indexer.server.query.filtered_search import (
        extension_overfetch_deadline,
    )

    _set_handler_timeouts(monkeypatch, mcp_seconds=100, rest_seconds=50)
    assert extension_overfetch_deadline(None) is None
    assert extension_overfetch_deadline([]) is None

    before = time.monotonic()
    deadline = extension_overfetch_deadline(["py"])
    after = time.monotonic()

    assert deadline is not None
    assert before + 40.0 <= deadline <= after + 40.0


# ------------------------------------------------- store push-down (P2-1)


@contextmanager
def _store_calls() -> Iterator[List[Tuple[Any, List[str]]]]:
    """Pass-through spy on the real FilesystemVectorStore.search: records
    each call's filter_conditions and the paths the store returned."""
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    real_search = FilesystemVectorStore.search
    calls: List[Tuple[Any, List[str]]] = []

    def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        out = real_search(self, *args, **kwargs)
        rows = out[0] if isinstance(out, tuple) else out
        calls.append(
            (kwargs.get("filter_conditions"), [r["payload"]["path"] for r in rows])
        )
        return out

    with patch.object(FilesystemVectorStore, "search", spy):
        yield calls


def _any_ext(filter_conditions: Any) -> List[Any]:
    must = (filter_conditions or {}).get("must", [])
    return [c for c in must if "any_ext" in c.get("match", {})]


def _assert_pushed_down(calls: List[Tuple[Any, List[str]]], values: List[str]) -> None:
    assert calls, "the store was never queried"
    wanted = sorted({v.strip().lower().lstrip(".") for v in values})
    for filter_conditions, returned in calls:
        assert _any_ext(filter_conditions) == [
            {"key": "path", "match": {"any_ext": wanted}}
        ], filter_conditions
        # Store-side filtering: no non-matching candidate leaves the store.
        assert all(passes(path, values, None) for path in returned), returned


def test_rest_semantic_pushes_any_ext_into_the_store(rest) -> None:
    client, _ = rest
    with _store_calls() as calls:
        data = _rest_query(client, "semantic", [".PY"], None)
    assert paths(data["results"]) == expected_semantic([".PY"], None)
    _assert_pushed_down(calls, [".PY"])


@pytest.mark.parametrize(
    "strategy",
    [
        pytest.param({}, id="primary"),
        pytest.param(
            {"query_strategy": "specific", "preferred_provider": "voyage-ai"},
            id="specific",
        ),
        pytest.param({"query_strategy": "failover"}, id="failover"),
        pytest.param({"query_strategy": "parallel"}, id="parallel"),
    ],
)
def test_mcp_semantic_pushes_any_ext_into_the_store(env, tmp_path, strategy) -> None:
    _, repo = env
    with _store_calls() as calls:
        got = _mcp_search(repo, tmp_path, "semantic", ["py", "MD"], None, **strategy)
    assert got == expected_semantic(["py", "MD"], None)
    _assert_pushed_down(calls, ["py", "MD"])


def test_pushdown_composes_with_language(rest) -> None:
    client, _ = rest
    with _store_calls() as calls:
        data = _rest_query(client, "semantic", ["PY", "md"], "python")
    assert paths(data["results"]) == expected_semantic(["PY", "md"], "python")
    for filter_conditions, _ in calls:
        must = filter_conditions["must"]
        assert len(_any_ext(filter_conditions)) == 1
        assert {"key": "language", "match": {"value": "py"}} in must[0]["should"]


def test_rest_semantic_language_without_extensions(rest) -> None:
    """P3: REST semantic forwards ``language`` on its own; with no
    file_extensions there is no any_ext condition and exactly one store
    query (no over-fetch rounds). The limit makes the store's HNSW window
    (2 x limit) cover the whole corpus, so every Python file is a candidate;
    unforwarded, the top results would hold .js and .txt files."""
    full_window_limit = (len(CORPUS) + 1) // 2
    # The language filter keeps its own (case-preserving) value match.
    lowercase_py = {p for p, _ in CORPUS if PurePosixPath(p).suffix == ".py"}
    client, _ = rest
    with _store_calls() as calls:
        response = client.post(
            "/api/query",
            json={
                "query_text": QUERY,
                "repository_alias": REPO_ALIAS,
                "limit": full_window_limit,
                "search_mode": "semantic",
                "language": "python",
            },
        )
    assert response.status_code == 200, response.text
    got = paths(response.json()["results"])
    assert all(PurePosixPath(p).suffix.lower() == ".py" for p in got), got
    assert lowercase_py <= set(got), got
    assert len(calls) == 1
    filter_conditions, _ = calls[0]
    assert _any_ext(filter_conditions) == []
    assert filter_conditions["must"] == [
        LanguageMapper().build_language_filter("python")
    ]


@pytest.mark.parametrize("door", ["rest", "mcp"])
def test_language_only_filter_fills_the_limit(env, rest, tmp_path, door) -> None:
    """Any store-side filter searches the widened candidate window: the
    Python files rank below 19 others, so a 2 x limit window held none."""
    limit = _doors.LIMIT
    # The language filter keeps its own (case-preserving) value match.
    python_by_rank = [
        p for p, _ in sorted(CORPUS, key=lambda e: e[1]) if p.endswith(".py")
    ]
    if door == "rest":
        client, _ = rest
        got = paths(_rest_query(client, "semantic", [], "python")["results"])
    else:
        _, repo = env
        got = _mcp_search(repo, tmp_path, "semantic", [], "python")
    assert got == python_by_rank[:limit]
