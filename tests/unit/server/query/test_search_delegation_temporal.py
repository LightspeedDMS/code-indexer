"""Search delegation: every query goes to the backend, temporal fails loud.

With a fake backend on ``app.state.search_backend`` (search_backend_wiring's
``active_search_backend()`` reads it without constructing the lazy app):

* SemanticQueryManager._search_single_repository forwards semantic, FTS and
  hybrid queries whatever the strategy (failover, parallel with both providers
  configured, specific) -- before any local branch;
* any temporal parameter raises SemanticQueryError instead of reading the local
  temporal index (which a stateless pod does not have);
* execute_live_temporal_search (the MCP and REST temporal door) returns a
  failed status without submitting a worker;
* the multi-repo search forwards semantic/FTS and refuses temporal.

Without a backend (the local default) every path is unchanged.
"""

from __future__ import annotations

import tempfile
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server import app as app_module
from code_indexer.server.query.semantic_query_manager import (
    SearchRequestError,
    SemanticQueryError,
    SemanticQueryManager,
)
from code_indexer.server.startup.search_backend_wiring import (
    TEMPORAL_NOT_DELEGATED_ERROR_CODE,
    TEMPORAL_NOT_DELEGATED_MESSAGE,
    active_search_backend,
)

_ROW = {
    "file_path": "src/a.py",
    "line_number": 7,
    "code_snippet": "def a(): ...",
    "similarity_score": 0.9,
    "language": "python",
}


class _FakeBackend:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def search(self, repository_alias: str, query_text: str, search_mode: str, **kw):
        self.calls.append(
            {"repository_alias": repository_alias, "search_mode": search_mode, **kw}
        )
        return [dict(_ROW)]


@pytest.fixture
def backend():
    fake = _FakeBackend()
    fake_app = SimpleNamespace(state=SimpleNamespace(search_backend=fake))
    with patch.dict(app_module.__dict__, {"app": fake_app}):
        yield fake


@pytest.fixture
def no_backend():
    fake_app = SimpleNamespace(state=SimpleNamespace(search_backend=None))
    with patch.dict(app_module.__dict__, {"app": fake_app}):
        yield


@pytest.fixture
def manager():
    with tempfile.TemporaryDirectory() as data_dir:
        yield SemanticQueryManager(
            data_dir=data_dir, activated_repo_manager=MagicMock()
        )


def _search(manager: SemanticQueryManager, **kw: Any):
    base: Dict[str, Any] = dict(
        repo_path="/nonexistent/repo",
        repository_alias="flask-global",
        query_text="auth",
        limit=5,
        min_score=None,
        file_extensions=None,
    )
    base.update(kw)
    return manager._search_single_repository(**base)


def test_active_search_backend_reads_app_state(backend) -> None:
    assert active_search_backend() is backend


def test_active_search_backend_is_none_without_an_app() -> None:
    with patch.dict(app_module.__dict__, {"app": None}):
        assert active_search_backend() is None


# -- _search_single_repository ------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {"search_mode": "semantic", "query_strategy": "failover"},
        {"search_mode": "semantic", "query_strategy": "parallel"},
        {
            "search_mode": "semantic",
            "query_strategy": "specific",
            "preferred_provider": "cohere",
        },
        {"search_mode": "fts"},
        {"search_mode": "hybrid"},
    ],
)
def test_every_non_temporal_query_is_delegated(manager, backend, kw) -> None:
    with (
        patch.object(manager, "_both_providers_configured", return_value=True),
        patch.object(manager, "_search_with_provider", side_effect=AssertionError),
        patch.object(manager, "_execute_temporal_query", side_effect=AssertionError),
    ):
        results = _search(manager, **kw)
    assert len(backend.calls) == 1
    assert backend.calls[0]["search_mode"] == kw["search_mode"]
    assert [r.file_path for r in results] == ["src/a.py"]
    assert results[0].repository_alias == "flask-global"


@pytest.mark.parametrize(
    "kw",
    [
        {"time_range": "2024-01-01..2024-12-31"},
        {"time_range_all": True},
        {"at_commit": "abc123"},
        {"chunk_type": "commit_message"},
        {"diff_type": "added"},
        {"author": "alice"},
    ],
)
def test_a_temporal_query_fails_loud_under_delegation(manager, backend, kw) -> None:
    with patch.object(manager, "_execute_temporal_query", side_effect=AssertionError):
        with pytest.raises(SemanticQueryError) as exc:
            _search(manager, **kw)
    assert str(exc.value) == TEMPORAL_NOT_DELEGATED_MESSAGE
    assert backend.calls == []


def test_without_a_backend_a_temporal_query_runs_locally(manager, no_backend) -> None:
    sentinel = object()
    with (
        patch.object(manager, "_is_composite_repository", return_value=False),
        patch.object(manager, "_both_providers_configured", return_value=False),
        patch.object(
            manager, "_execute_temporal_query", return_value=sentinel
        ) as local,
    ):
        assert _search(manager, time_range_all=True) is sentinel
    local.assert_called_once()


# -- execute_live_temporal_search ----------------------------------------------


def _live(**kw: Any) -> Dict[str, Any]:
    from code_indexer.server.services.temporal_live_dispatch import (
        execute_live_temporal_search,
    )

    return execute_live_temporal_search(
        worker_input=MagicMock(),
        background_job_manager=MagicMock(),
        payload_cache=MagicMock(),
        access_filtering_service=None,
        is_admin=True,
        inline_wait_seconds=1.0,
        handler_deadline_monotonic=None,
        response_reserve_seconds=0.0,
        **kw,
    )


def test_the_live_temporal_door_refuses_under_delegation(backend) -> None:
    dedup = MagicMock()
    freshness = MagicMock()
    result = _live(dedup_cache=dedup, freshness_cache=freshness)
    assert result["status"] == "failed"
    assert result["error"] == TEMPORAL_NOT_DELEGATED_MESSAGE
    assert result["error_code"] == TEMPORAL_NOT_DELEGATED_ERROR_CODE
    assert dedup.mock_calls == [] and freshness.mock_calls == []
    assert backend.calls == []


def test_the_live_temporal_door_proceeds_locally(no_backend) -> None:
    class _Proceeded(Exception):
        pass

    freshness = MagicMock()
    freshness.get_or_compute.side_effect = _Proceeded
    with (
        patch(
            "code_indexer.server.services.temporal_live_dispatch."
            "_worker_input_signature_dict",
            return_value={},
        ),
        pytest.raises(_Proceeded),
    ):
        _live(dedup_cache=MagicMock(), freshness_cache=freshness)


# -- multi-repo search ----------------------------------------------------------


def _multi(search_type: str):
    from code_indexer.server.multi.models import MultiSearchRequest
    from code_indexer.server.multi.multi_search_config import MultiSearchConfig
    from code_indexer.server.multi.multi_search_service import MultiSearchService

    service = MultiSearchService(MultiSearchConfig())
    request = MultiSearchRequest(
        repositories=["flask-global"],
        query="auth",
        search_type=search_type,
        limit=3,
        language="python",
    )
    return service, request


@pytest.mark.parametrize("search_type", ["semantic", "fts"])
def test_multi_repo_semantic_and_fts_are_delegated(backend, search_type) -> None:
    service, request = _multi(search_type)
    with (
        patch.object(service, "_search_semantic_sync", side_effect=AssertionError),
        patch.object(service, "_search_fts_sync", side_effect=AssertionError),
    ):
        rows = service._search_single_repo_sync("flask-global", request)
    assert backend.calls[0]["repository_alias"] == "flask-global"
    assert backend.calls[0]["search_mode"] == search_type
    assert backend.calls[0]["language"] == "python"
    assert backend.calls[0]["limit"] == 3
    assert rows == [
        {
            "file_path": "src/a.py",
            "line_start": 7,
            "line_end": 7,
            "score": 0.9,
            "content": "def a(): ...",
            "language": "python",
        }
    ]


def test_multi_repo_temporal_fails_loud_under_delegation(backend) -> None:
    service, request = _multi("temporal")
    with patch.object(service, "_search_temporal_sync", side_effect=AssertionError):
        # A SearchRequestError: the refusal describes the caller's request, so
        # its text reaches the client (search_error_policy) instead of the
        # generic "Search failed" a RuntimeError would become.
        with pytest.raises(
            SearchRequestError, match="Temporal search is not available"
        ):
            service._search_single_repo_sync("flask-global", request)
    assert backend.calls == []


@pytest.mark.parametrize(
    "search_type,method",
    [
        ("semantic", "_search_semantic_sync"),
        ("fts", "_search_fts_sync"),
        ("temporal", "_search_temporal_sync"),
    ],
)
def test_multi_repo_runs_locally_without_a_backend(
    no_backend, search_type, method
) -> None:
    service, request = _multi(search_type)
    with patch.object(service, method, return_value=["local"]) as local:
        assert service._search_single_repo_sync("flask-global", request) == ["local"]
    local.assert_called_once()
