"""Memory retrieval attached to search_code honours repository visibility.

Invariant: a memory is returned in ``query_metadata.relevant_memories`` only
when the caller may see it under the AccessFilteringService memory rule
(scope ``global`` always; scope ``repo``/``file`` only when its
``referenced_repo`` is granted; admins see every memory). The check runs
before any memory body is hydrated, and a caller whose candidates were all
hidden receives exactly the same empty-state nudge as a caller with no match.

Real objects: GroupAccessManager (SQLite in tmp), AccessFilteringService
and memory files written with memory_io in tmp dirs.
Boundaries replaced: the HNSW candidate lookup (MemoryCandidateRetriever
.retrieve) and the Voyage query embedding (external service).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.access_filtering_service import AccessFilteringService
from code_indexer.server.services.group_access_manager import GroupAccessManager
from code_indexer.server.services.memory_candidate_retriever import (
    MemoryCandidate,
    MemoryCandidateRetriever,
)
from code_indexer.server.services.memory_io import atomic_write_memory_file
from code_indexer.server.utils.config_manager import MemoryRetrievalConfig

GLOBAL_ID = "1" * 32
REPO_A_ID = "a" * 32
REPO_B_ID = "b" * 32
REPO_B_GLOBAL_ALIAS_ID = "c" * 32
NO_SCOPE_ID = "d" * 32
REPO_SCOPE_NO_REPO_ID = "e" * 32

BODIES = {
    GLOBAL_ID: "global memory body",
    REPO_A_ID: "repo-a memory body",
    REPO_B_ID: "repo-b memory body",
    REPO_B_GLOBAL_ALIAS_ID: "repo-b global alias memory body",
    NO_SCOPE_ID: "memory without scope body",
    REPO_SCOPE_NO_REPO_ID: "repo scope without repository body",
}

_APP_MODULE_PATCH = "code_indexer.server.mcp.handlers._utils.app_module"
_REPO_SEARCH_CONFIG_PATCH = (
    "code_indexer.server.mcp.handlers.search.repo_search.get_config_service"
)
_SHARED_VECTOR_PATCH = (
    "code_indexer.server.mcp.handlers.search.repo_search._compute_shared_query_vector"
)
_QUERY_VECTOR = [0.1, 0.2, 0.3, 0.4]
_ACCESS_MODULE = "code_indexer.server.services.access_filtering_service"
_PIPELINE_MODULE = "code_indexer.server.mcp.memory_retrieval_pipeline"


def _write_memory(
    memories_dir: Path, memory_id: str, frontmatter: Dict[str, Any]
) -> None:
    fm: Dict[str, Any] = {
        "id": memory_id,
        "type": "gotcha",
        "summary": "example summary",
        "tags": [],
        "created_by": "example-author",
        "created_at": "2024-01-01T00:00:00+00:00",
        "edited_by": None,
        "edited_at": None,
        **frontmatter,
    }
    atomic_write_memory_file(memories_dir / f"{memory_id}.md", fm, BODIES[memory_id])


@pytest.fixture
def golden_dir(tmp_path: Path) -> Path:
    golden = tmp_path / "golden-repos"
    memories = golden / "cidx-meta" / "memories"
    memories.mkdir(parents=True)
    _write_memory(memories, GLOBAL_ID, {"scope": "global"})
    _write_memory(memories, REPO_A_ID, {"scope": "repo", "referenced_repo": "repo-a"})
    _write_memory(memories, REPO_B_ID, {"scope": "file", "referenced_repo": "repo-b"})
    _write_memory(
        memories,
        REPO_B_GLOBAL_ALIAS_ID,
        {"scope": "repo", "referenced_repo": "repo-b-global"},
    )
    _write_memory(memories, NO_SCOPE_ID, {})
    _write_memory(memories, REPO_SCOPE_NO_REPO_ID, {"scope": "repo"})
    return golden


def _group_id(manager: GroupAccessManager, name: str) -> int:
    group = manager.get_group_by_name(name)
    assert group is not None, name
    return group.id


@pytest.fixture
def access_service(tmp_path: Path, golden_dir: Path) -> AccessFilteringService:
    manager = GroupAccessManager(tmp_path / "groups.db")
    powerusers = _group_id(manager, "powerusers")
    users = _group_id(manager, "users")
    manager.grant_repo_access("repo-a", powerusers, "system:test")
    manager.grant_repo_access("repo-b", users, "system:test")
    manager.assign_user_to_group("user-a", powerusers, "admin")
    manager.assign_user_to_group("user-b", users, "admin")
    manager.assign_user_to_group("admin-user", _group_id(manager, "admins"), "admin")
    memories_dir = golden_dir / "cidx-meta" / "memories"
    return AccessFilteringService(manager, memories_dir=memories_dir)


def _user(username: str) -> User:
    return User(
        username=username,
        password_hash="unused",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )


class _ConfigService:
    """Config service stand-in exposing a real MemoryRetrievalConfig."""

    def __init__(self, cohere_min_score: float = 0.4) -> None:
        self._config = SimpleNamespace(
            memory_retrieval_config=MemoryRetrievalConfig(
                memory_cohere_min_score=cohere_min_score
            )
        )

    def get_config(self) -> Any:
        return self._config


def _candidates(ids: List[str], golden: Path) -> List[MemoryCandidate]:
    memories = golden / "cidx-meta" / "memories"
    return [
        MemoryCandidate(
            memory_id=mid, hnsw_score=0.9 - i * 0.01, memory_path=str(memories / mid)
        )
        for i, mid in enumerate(ids)
    ]


@pytest.fixture
def hnsw_returns() -> Iterator[Dict[str, List[MemoryCandidate]]]:
    holder: Dict[str, List[MemoryCandidate]] = {"candidates": []}
    with patch.object(
        MemoryCandidateRetriever,
        "retrieve",
        side_effect=lambda **_kw: list(holder["candidates"]),
    ):
        yield holder


def _app_module(golden: Path, service: Optional[AccessFilteringService]) -> Any:
    state = SimpleNamespace(
        golden_repos_dir=str(golden),
        access_filtering_service=service,
        payload_cache=None,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _run(
    username: str,
    golden: Path,
    service: Optional[AccessFilteringService],
    reranker_status: str = "disabled",
    cohere_min_score: float = 0.4,
) -> List[Dict[str, Any]]:
    from code_indexer.server.mcp.handlers.search.memory_retrieval import (
        _run_memory_retrieval,
    )

    with patch(_APP_MODULE_PATCH, _app_module(golden, service)):
        result = _run_memory_retrieval(
            {"query_text": "example query", "search_mode": "semantic", "limit": 5},
            _user(username),
            _ConfigService(cohere_min_score=cohere_min_score),
            reranker_status,
            query_vector=_QUERY_VECTOR,
        )
    assert result is not None
    return result


def _ids(memories: List[Dict[str, Any]]) -> List[str]:
    return [m["memory_id"] for m in memories]


ALL_SCOPED = [GLOBAL_ID, REPO_A_ID, REPO_B_ID, REPO_B_GLOBAL_ALIAS_ID]


class TestRunMemoryRetrievalVisibility:
    # Reranker "success" with a zero post-rerank floor lets memories reach the
    # response on the rerank path, so the visibility check is proven there too.
    @pytest.mark.parametrize(
        "reranker_status,cohere_min",
        [("disabled", 0.4), ("success", 0.0), ("skipped", 0.0)],
    )
    def test_repo_a_user_gets_global_and_repo_a_but_never_repo_b(
        self, golden_dir, access_service, hnsw_returns, reranker_status, cohere_min
    ):
        hnsw_returns["candidates"] = _candidates(ALL_SCOPED, golden_dir)
        result = _run("user-a", golden_dir, access_service, reranker_status, cohere_min)
        assert sorted(_ids(result)) == sorted([GLOBAL_ID, REPO_A_ID])
        bodies = {m["memory_id"]: m["body"] for m in result}
        assert bodies[GLOBAL_ID] == BODIES[GLOBAL_ID]
        assert BODIES[REPO_B_ID] not in json.dumps(result)

    def test_repo_b_user_gets_repo_b_memories(
        self, golden_dir, access_service, hnsw_returns
    ):
        hnsw_returns["candidates"] = _candidates(ALL_SCOPED, golden_dir)
        result = _run("user-b", golden_dir, access_service)
        assert sorted(_ids(result)) == sorted(
            [GLOBAL_ID, REPO_B_ID, REPO_B_GLOBAL_ALIAS_ID]
        )

    def test_admin_gets_every_memory(self, golden_dir, access_service, hnsw_returns):
        hnsw_returns["candidates"] = _candidates(ALL_SCOPED, golden_dir)
        result = _run("admin-user", golden_dir, access_service)
        assert sorted(_ids(result)) == sorted(ALL_SCOPED)

    def test_memory_without_readable_scope_is_dropped(
        self, golden_dir, access_service, hnsw_returns
    ):
        hnsw_returns["candidates"] = _candidates(
            [GLOBAL_ID, NO_SCOPE_ID, REPO_SCOPE_NO_REPO_ID], golden_dir
        )
        result = _run("user-a", golden_dir, access_service)
        assert _ids(result) == [GLOBAL_ID]

    def test_all_hidden_is_identical_to_no_match(
        self, golden_dir, access_service, hnsw_returns
    ):
        hnsw_returns["candidates"] = []
        no_match = _run("user-a", golden_dir, access_service)
        hnsw_returns["candidates"] = _candidates(
            [REPO_B_ID, REPO_B_GLOBAL_ALIAS_ID], golden_dir
        )
        all_hidden = _run("user-a", golden_dir, access_service)
        assert all_hidden == no_match
        assert _ids(all_hidden) == ["__empty_nudge__"]

    def test_hidden_candidate_file_is_never_fully_read(
        self, golden_dir, access_service, hnsw_returns
    ):
        from code_indexer.server.services import memory_io

        full_reads: List[str] = []
        real_read = memory_io.read_memory_file

        def _recording_read(path: Path) -> Any:
            full_reads.append(path.name)
            return real_read(path)

        hnsw_returns["candidates"] = _candidates([GLOBAL_ID, REPO_B_ID], golden_dir)
        with (
            patch(f"{_PIPELINE_MODULE}.read_memory_file", _recording_read),
        ):
            result = _run("user-a", golden_dir, access_service)

        assert _ids(result) == [GLOBAL_ID]
        assert f"{GLOBAL_ID}.md" in full_reads
        assert f"{REPO_B_ID}.md" not in full_reads

    def test_without_access_service_no_memory_is_returned(
        self, golden_dir, hnsw_returns
    ):
        hnsw_returns["candidates"] = _candidates(ALL_SCOPED, golden_dir)
        result = _run("admin-user", golden_dir, None)
        assert _ids(result) == ["__empty_nudge__"]


def _search_code(
    golden: Path,
    service: AccessFilteringService,
    username: str,
    rerank_query: Optional[str],
) -> List[Dict[str, Any]]:
    app_module = MagicMock()
    app_module.app.state = _app_module(golden, service).app.state
    app_module.golden_repo_manager._repo_category_service.get_repo_category_map.return_value = {}
    app_module.semantic_query_manager.query_user_repositories.return_value = {
        "results": [
            {
                "file_path": "src/example.py",
                "content": "def example(): pass",
                "score": 0.9,
                "repository_alias": "repo-a",
            }
        ],
        "total_results": 1,
        "query_metadata": {"query_text": "example query"},
    }
    params: Dict[str, Any] = {
        "query_text": "example query",
        "search_mode": "semantic",
        "limit": 5,
        "repository_alias": "repo-a",
    }
    if rerank_query is not None:
        params["rerank_query"] = rerank_query
    with (
        patch(_APP_MODULE_PATCH, app_module),
        patch(_REPO_SEARCH_CONFIG_PATCH, return_value=_ConfigService(0.0)),
        patch(_SHARED_VECTOR_PATCH, return_value=(_QUERY_VECTOR, "digest")),
    ):
        from code_indexer.server.mcp.handlers import search_code

        response = search_code(params, _user(username))
    payload = json.loads(response["content"][0]["text"])
    assert payload.get("success") is True, payload
    memories: List[Dict[str, Any]] = payload["results"]["query_metadata"][
        "relevant_memories"
    ]
    return memories


class TestSearchCodeHandlerVisibility:
    @pytest.mark.parametrize("rerank_query", [None, "example rerank"])
    def test_search_code_hides_repo_b_memory_from_repo_a_user(
        self, golden_dir, access_service, hnsw_returns, rerank_query
    ):
        hnsw_returns["candidates"] = _candidates(ALL_SCOPED, golden_dir)
        memories = _search_code(golden_dir, access_service, "user-a", rerank_query)
        assert sorted(_ids(memories)) == sorted([GLOBAL_ID, REPO_A_ID])
        assert BODIES[REPO_B_ID] not in json.dumps(memories)

    @pytest.mark.parametrize("rerank_query", [None, "example rerank"])
    def test_search_code_returns_repo_b_memory_to_admin(
        self, golden_dir, access_service, hnsw_returns, rerank_query
    ):
        hnsw_returns["candidates"] = _candidates(ALL_SCOPED, golden_dir)
        memories = _search_code(golden_dir, access_service, "admin-user", rerank_query)
        assert sorted(_ids(memories)) == sorted(ALL_SCOPED)
