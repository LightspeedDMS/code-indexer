"""#2047 (S21): one ``file_extensions`` rule for semantic, FTS and hybrid
search, through both server front doors (REST ``/api/query`` and MCP
``search_code``).

Rule: values are case-insensitive, the leading dot is optional, several
values are OR-ed, an extensionless file matches no extension, and the result
is intersected with ``language`` when both are given. Each mode's top-k AFTER
filtering must equal the brute-force answer over every chunk (see
extension_filter_env_2047): the filter may never leave the answer short while
matching chunks exist, nor let a non-matching chunk through.

Real route / handler, real SemanticQueryManager, SemanticSearchService,
FilesystemVectorStore and Tantivy index over a real git repo. Replaced: the
embedding provider (external service), authentication, and the
activated-repo listing (which repos the caller has).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole
from tests.unit.server._isolated_app import isolated_app
from tests.unit.server.query.extension_filter_env_2047 import (
    LIMIT,
    QUERY,
    REPO_ALIAS,
    build_corpus_repo,
    expected_fts,
    expected_hybrid,
    expected_semantic,
    passes,
    paths,
    ranked_store_search,
)

ADMIN = User(
    username="admin",
    password_hash="$2b$12$hash",
    role=UserRole.ADMIN,
    created_at=datetime.now(timezone.utc),
)
NEVER_A_SUFFIX = "never be a file extension"

# (case id, file_extensions, language)
CASES: List[Tuple[str, List[str], Optional[str]]] = [
    ("bare-lowercase", ["md"], None),
    ("dotted-uppercase", [".PY"], None),
    ("several-values-ored", ["py", ".MD"], None),
    ("txt-excludes-extensionless", ["txt"], None),
    ("intersected-with-language", ["PY", "md"], "python"),
]
CASE_PARAMS = [pytest.param(v, lang, id=cid) for cid, v, lang in CASES]


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Tuple[Any, Path]]:
    root = tmp_path_factory.mktemp("ext-filter-2047")
    repo = build_corpus_repo(root)
    with pytest.MonkeyPatch.context() as mp:
        # One provider configured -> deterministic primary-only routing.
        mp.delenv("CO_API_KEY", raising=False)
        mp.delenv("VOYAGE_API_KEY", raising=False)
        with isolated_app(root / "app") as app:
            yield app, repo


def _user_repos(repo: Path) -> List[Dict[str, Any]]:
    return [{"user_alias": REPO_ALIAS, "repo_path": str(repo)}]


# --------------------------------------------------------------------- REST


@pytest.fixture
def rest(env: Tuple[Any, Path]) -> Iterator[Tuple[TestClient, Path]]:
    from code_indexer.server.auth.dependencies import get_current_user

    app, repo = env
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    arm = app.state.semantic_query_manager.activated_repo_manager
    try:
        with (
            ranked_store_search(),
            patch.object(
                arm, "list_activated_repositories", return_value=_user_repos(repo)
            ),
            patch.object(arm, "get_activated_repo_path", return_value=str(repo)),
        ):
            yield TestClient(app), repo
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _rest_post(
    client: TestClient, mode: str, values: List[str], language: Optional[str]
) -> Any:
    body: Dict[str, Any] = {
        "query_text": QUERY,
        "repository_alias": REPO_ALIAS,
        "limit": LIMIT,
        "search_mode": mode,
        "file_extensions": values,
    }
    if language is not None:
        body["language"] = language
    return client.post("/api/query", json=body)


def _rest_query(
    client: TestClient, mode: str, values: List[str], language: Optional[str]
) -> Dict[str, Any]:
    response = _rest_post(client, mode, values, language)
    assert response.status_code == 200, response.text
    data: Dict[str, Any] = response.json()
    return data


@pytest.mark.parametrize("values, language", CASE_PARAMS)
def test_rest_semantic_filtered_top_k(rest, values, language) -> None:
    client, _ = rest
    data = _rest_query(client, "semantic", values, language)
    assert paths(data["results"]) == expected_semantic(values, language)


@pytest.mark.parametrize("values, language", CASE_PARAMS)
def test_rest_fts_filtered_top_k(rest, values, language) -> None:
    client, repo = rest
    data = _rest_query(client, "fts", values, language)
    assert paths(data["fts_results"], "path") == expected_fts(repo, values, language)


@pytest.mark.parametrize("values, language", CASE_PARAMS)
def test_rest_hybrid_filtered_top_k(rest, values, language) -> None:
    client, repo = rest
    data = _rest_query(client, "hybrid", values, language)
    assert data["search_mode"] == "hybrid"
    assert paths(data["fts_results"], "path") == expected_fts(repo, values, language)
    assert paths(data["semantic_results"]) == expected_semantic(values, language)


def test_rest_accepts_symbol_extension(rest) -> None:
    client, _ = rest
    assert _rest_query(client, "semantic", ["c++"], None)["results"] == []


def test_rest_rejects_value_that_is_never_a_suffix(rest) -> None:
    client, _ = rest
    response = _rest_post(client, "semantic", ["py", "tar.gz"], None)
    assert response.status_code == 422, response.text
    assert NEVER_A_SUFFIX in response.text


# ---------------------------------------------------------------------- MCP


def _mcp_body(
    repo: Path,
    tmp_path: Path,
    mode: str,
    values: List[str],
    language: Optional[str],
    **extra: Any,
) -> Dict[str, Any]:
    from code_indexer.server.mcp.handlers import search_code
    from code_indexer.server.query.semantic_query_manager import (
        SemanticQueryManager,
    )

    repo_listing = MagicMock()
    repo_listing.list_activated_repositories.return_value = _user_repos(repo)
    repo_listing.user_has_activated_repo.return_value = True
    manager = SemanticQueryManager(
        data_dir=str(tmp_path / "server-data"),
        activated_repo_manager=repo_listing,
        background_job_manager=MagicMock(),
    )
    app_stand_in = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace()),
        semantic_query_manager=manager,
        activated_repo_manager=repo_listing,
        golden_repo_manager=None,
    )
    params: Dict[str, Any] = {
        "query_text": QUERY,
        "repository_alias": REPO_ALIAS,
        "limit": LIMIT,
        "min_score": 0.0,
        "search_mode": mode,
        "file_extensions": values,
    }
    if language is not None:
        params["language"] = language
    params.update(extra)
    with (
        ranked_store_search(),
        patch("code_indexer.server.mcp.handlers._utils.app_module", app_stand_in),
    ):
        response = search_code(params, ADMIN)
    body: Dict[str, Any] = json.loads(response["content"][0]["text"])
    return body


def _mcp_search(
    repo: Path,
    tmp_path: Path,
    mode: str,
    values: List[str],
    language: Optional[str],
    **extra: Any,
) -> List[str]:
    body = _mcp_body(repo, tmp_path, mode, values, language, **extra)
    assert body["success"] is True, body
    return paths(body["results"]["results"])


@pytest.mark.parametrize("values, language", CASE_PARAMS)
def test_mcp_semantic_filtered_top_k(env, tmp_path, values, language) -> None:
    _, repo = env
    got = _mcp_search(repo, tmp_path, "semantic", values, language)
    assert got == expected_semantic(values, language)


@pytest.mark.parametrize(
    "strategy",
    [
        pytest.param(
            {"query_strategy": "specific", "preferred_provider": "voyage-ai"},
            id="specific",
        ),
        pytest.param({"query_strategy": "failover"}, id="failover"),
        pytest.param({"query_strategy": "parallel"}, id="parallel"),
    ],
)
@pytest.mark.parametrize("values", [["md"], [".PY"]], ids=["md", "dot-PY"])
def test_mcp_semantic_strategies_filtered_top_k(
    env, tmp_path, strategy, values
) -> None:
    _, repo = env
    got = _mcp_search(repo, tmp_path, "semantic", values, None, **strategy)
    assert got == expected_semantic(values, None)


@pytest.mark.parametrize("values, language", CASE_PARAMS)
def test_mcp_fts_filtered_top_k(env, tmp_path, values, language) -> None:
    _, repo = env
    got = _mcp_search(repo, tmp_path, "fts", values, language)
    assert got == expected_fts(repo, values, language)


@pytest.mark.parametrize("values, language", CASE_PARAMS)
def test_mcp_hybrid_filtered_top_k(env, tmp_path, values, language) -> None:
    _, repo = env
    got = _mcp_search(repo, tmp_path, "hybrid", values, language)
    fts = expected_fts(repo, values, language)
    semantic = expected_semantic(values, language)
    expected, unambiguous = expected_hybrid(fts, semantic)

    assert all(passes(p, values, language) for p in got), got
    assert len(got) == len(expected) == LIMIT
    if unambiguous:
        assert set(got) == expected
    else:
        assert set(got) <= set(fts) | set(semantic)


def _query_embedding_spy() -> Any:
    """Pass-through class-level spy counting embeddings of the query text."""
    from tests.unit.server.query.extension_filter_env_2047 import (
        RankedEmbeddingProvider,
    )

    return patch.object(
        RankedEmbeddingProvider,
        "_vector_for",
        autospec=True,
        side_effect=RankedEmbeddingProvider._vector_for,
    )


def _query_embeddings(spy: Any) -> int:
    return sum(1 for call in spy.call_args_list if call.args[1] == QUERY)


@pytest.mark.parametrize(
    "strategy",
    [
        pytest.param({}, id="primary"),
        pytest.param(
            {"query_strategy": "specific", "preferred_provider": "voyage-ai"},
            id="specific",
        ),
    ],
)
def test_mcp_semantic_embeds_query_once_across_rounds(env, tmp_path, strategy) -> None:
    _, repo = env
    with _query_embedding_spy() as spy:
        got = _mcp_search(repo, tmp_path, "semantic", ["md"], None, **strategy)
    assert got == expected_semantic(["md"], None)
    assert _query_embeddings(spy) == 1


def test_rest_semantic_embeds_query_once_across_rounds(rest) -> None:
    client, _ = rest
    with _query_embedding_spy() as spy:
        data = _rest_query(client, "semantic", ["md"], None)
    assert paths(data["results"]) == expected_semantic(["md"], None)
    assert _query_embeddings(spy) == 1


def test_semantic_extension_search_is_one_store_query(env, tmp_path) -> None:
    """The store filters over a widened candidate window, so one query finds
    the .md files ranked below every non-matching file -- no over-fetch
    rounds."""
    from code_indexer.server.services.search_service import SemanticSearchService

    _, repo = env
    with patch.object(
        SemanticSearchService,
        "search_repository_path",
        autospec=True,
        side_effect=SemanticSearchService.search_repository_path,
    ) as searches:
        got = _mcp_search(repo, tmp_path, "semantic", ["md"], None)
    assert searches.call_count == 1
    assert got == expected_semantic(["md"], None)


def test_multimodal_repo_fans_out_once(env, tmp_path) -> None:
    from code_indexer.server.services.search_service import SemanticSearchService
    from code_indexer.services.multi_index_query_service import (
        MultiIndexQueryService,
    )
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
    from tests.unit.server.query.content_unavailable_env_1991 import (
        VECTOR_DIM,
        FakeEmbeddingProvider,
    )

    (tmp_path / "mm").mkdir()
    repo = build_corpus_repo(tmp_path / "mm")
    FilesystemVectorStore(
        base_path=repo / ".code-indexer" / "index", project_root=repo
    ).create_collection("voyage-multimodal-3", vector_size=VECTOR_DIM)

    with (
        patch.object(
            SemanticSearchService,
            "search_repository_path",
            autospec=True,
            side_effect=SemanticSearchService.search_repository_path,
        ) as rounds,
        patch.object(
            MultiIndexQueryService,
            "query_with_separate_kwargs",
            autospec=True,
            side_effect=MultiIndexQueryService.query_with_separate_kwargs,
        ) as fan_outs,
        patch.object(
            MultiIndexQueryService,
            "_get_multimodal_provider",
            return_value=FakeEmbeddingProvider(),
        ),
    ):
        got = _mcp_search(repo, tmp_path, "semantic", ["md"], None)

    assert got == expected_semantic(["md"], None)
    # One store query: one code+multimodal fan-out, embedded once.
    assert rounds.call_count == 1
    assert fan_outs.call_count == 1


def _two_repo_manager(first_repo: Path, tmp_path: Path) -> Any:
    from code_indexer.server.query.semantic_query_manager import (
        SemanticQueryManager,
    )

    (tmp_path / "second").mkdir()
    second_repo = build_corpus_repo(tmp_path / "second")
    listing = MagicMock()
    listing.list_activated_repositories.return_value = [
        {"user_alias": "repo-a", "repo_path": str(first_repo)},
        {"user_alias": "repo-b", "repo_path": str(second_repo)},
    ]
    return SemanticQueryManager(
        data_dir=str(tmp_path / "server-data"),
        activated_repo_manager=listing,
        background_job_manager=MagicMock(),
    )


def test_one_request_deadline_shared_across_repositories(env, tmp_path) -> None:
    import time

    from code_indexer.services.tantivy_index_manager import TantivyIndexManager

    manager = _two_repo_manager(env[1], tmp_path)
    deadline = time.monotonic() + 3600
    with patch.object(
        TantivyIndexManager,
        "search",
        autospec=True,
        side_effect=TantivyIndexManager.search,
    ) as fts_searches:
        response = manager.query_user_repositories(
            username="admin",
            query_text=QUERY,
            limit=LIMIT,
            file_extensions=["c++"],  # cannot be pushed down: every hit inspected
            search_mode="fts",
            extension_deadline=deadline,
        )

    assert response["results"] == []
    deadlines = [c.kwargs["deadline"] for c in fts_searches.call_args_list]
    assert deadlines == [deadline, deadline]


def test_expired_deadline_starts_no_further_repository_search(
    env, tmp_path, caplog
) -> None:
    """No repository search starts once the request's budget has passed:
    the index is never queried, although .md files would match."""
    import logging
    import time

    from code_indexer.services.tantivy_index_manager import TantivyIndexManager

    manager = _two_repo_manager(env[1], tmp_path)
    with (
        patch.object(
            TantivyIndexManager,
            "search",
            autospec=True,
            side_effect=TantivyIndexManager.search,
        ) as fts_searches,
        caplog.at_level(logging.INFO, logger=SQM_LOGGER),
    ):
        response = manager.query_user_repositories(
            username="admin",
            query_text=QUERY,
            limit=LIMIT,
            file_extensions=["md"],
            search_mode="fts",
            extension_deadline=time.monotonic() - 1,
        )

    assert fts_searches.call_count == 0
    assert response["results"] == []
    assert any("not searched" in r.getMessage() for r in caplog.records), caplog.records


SQM_LOGGER = "code_indexer.server.query.semantic_query_manager"


TANTIVY_LOGGER = "code_indexer.services.tantivy_index_manager"


def test_rest_hybrid_halves_share_one_deadline(rest) -> None:
    from code_indexer.server.query.semantic_query_manager import (
        SemanticQueryManager,
    )
    from code_indexer.services.tantivy_index_manager import TantivyIndexManager

    client, _ = rest
    with (
        patch.object(
            TantivyIndexManager,
            "search",
            autospec=True,
            side_effect=TantivyIndexManager.search,
        ) as fts_searches,
        patch.object(
            SemanticQueryManager,
            "_search_single_repository",
            autospec=True,
            side_effect=SemanticQueryManager._search_single_repository,
        ) as repo_searches,
    ):
        _rest_query(client, "hybrid", ["md"], None)

    fts_deadline = fts_searches.call_args.kwargs["deadline"]
    assert fts_deadline is not None
    assert repo_searches.call_args.kwargs["extension_deadline"] == fts_deadline


@pytest.mark.parametrize(
    "strategy",
    [
        pytest.param({}, id="primary"),
        pytest.param({"query_strategy": "parallel"}, id="parallel"),
    ],
)
@pytest.mark.parametrize(
    "values, message",
    [
        pytest.param(["py", "tar.gz"], NEVER_A_SUFFIX, id="never-a-suffix"),
        pytest.param("py", "must be a list", id="bare-string"),
    ],
)
def test_mcp_rejects_invalid_file_extensions_before_searching(
    env, tmp_path, strategy, values, message
) -> None:
    """Validated once, before any repository is searched: an invalid value
    is a clear error, never an empty success (the parallel strategy used to
    swallow it per provider)."""
    from code_indexer.server.services.search_service import SemanticSearchService

    _, repo = env
    with (
        patch.object(SemanticSearchService, "search_repository_path") as primary,
        patch.object(
            SemanticSearchService, "search_repository_path_with_provider"
        ) as per_provider,
    ):
        body = _mcp_body(repo, tmp_path, "semantic", values, None, **strategy)
    assert body["success"] is False, body
    assert message in json.dumps(body)
    assert primary.call_count == 0 and per_provider.call_count == 0


@pytest.mark.parametrize("mode", ["semantic", "fts"])
def test_mcp_rejects_value_that_is_never_a_suffix(env, tmp_path, mode) -> None:
    _, repo = env
    body = _mcp_body(repo, tmp_path, mode, ["py", "tar.gz"], None)
    assert body["success"] is False, body
    assert NEVER_A_SUFFIX in json.dumps(body)
