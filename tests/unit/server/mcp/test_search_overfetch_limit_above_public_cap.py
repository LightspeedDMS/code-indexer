"""Internal search over-fetch above the public request cap (limit > 100).

Bug: rerank over-fetch (``_compute_rerank_limit``, up to MAX_CANDIDATE_LIMIT
= 200) and access-filter over-fetch (``limit * 2``) raise the internal
retrieval limit above 100. That value used to be fed into the PUBLIC
``SemanticSearchRequest`` model (``limit`` capped ``le=100``), so a request
that is valid against the documented schema failed with a Pydantic
validation error instead of returning results.

These tests drive the REAL code path -- MCP handler / REST route ->
SemanticQueryManager -> SemanticSearchService -- and fake only:
  * the vector-store leaf ``SemanticSearchService._perform_semantic_search``
    (no embedding provider call), which returns exactly ``limit`` hits, and
  * the reranker provider funnel ``_apply_reranking_sync`` (external
    service), which records how many candidates it received.
"""

from contextlib import contextmanager
from typing import Any, Dict, Iterator, List
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.models.api_models import SearchResultItem
from code_indexer.server.query.semantic_query_manager import SemanticQueryManager
from code_indexer.server.services.search_service import SemanticSearchService
from code_indexer.server.utils.config_manager import RerankConfig

_MAX_CANDIDATES = 200
_OVERFETCH_MULTIPLIER = 5
_SEARCH_PKG = "code_indexer.server.mcp.handlers.search"


# ---------------------------------------------------------------------------
# Shared scaffolding
# ---------------------------------------------------------------------------


def _make_user(role: UserRole = UserRole.ADMIN) -> User:
    user = MagicMock(spec=User)
    user.username = "example-user"
    user.role = role
    user.has_permission = MagicMock(return_value=True)
    return user


def _make_config_service() -> MagicMock:
    config = MagicMock()
    config.rerank_config = RerankConfig(
        voyage_reranker_model="rerank-2.5",
        cohere_reranker_model="rerank-v3.5",
        overfetch_multiplier=_OVERFETCH_MULTIPLIER,
    )
    config.memory_retrieval_config.memory_retrieval_enabled = False
    svc = MagicMock()
    svc.get_config.return_value = config
    return svc


class _Recorder:
    """Records leaf search limits and reranker candidate counts."""

    def __init__(self) -> None:
        self.leaf_limits: List[int] = []
        self.rerank_candidate_counts: List[int] = []

    def leaf(self, _self: Any, repo_path: str, query: str, limit: int, *a, **kw):
        self.leaf_limits.append(limit)
        return [
            SearchResultItem(
                score=round(0.99 - i * 0.001, 4),
                file_path=f"src/module_{i}.py",
                line_start=1,
                line_end=5,
                content=f"def function_{i}(): pass",
                language="python",
                file_last_modified=None,
                indexed_timestamp=None,
            )
            for i in range(limit)
        ]

    def rerank(self, results: list, requested_limit: int, **kw):
        self.rerank_candidate_counts.append(len(results))
        meta = {
            "reranker_used": True,
            "reranker_provider": "voyage",
            "rerank_time_ms": 1,
            "rerank_hint": None,
            "reranker_status": {
                "status": "success",
                "provider": "voyage",
                "rerank_time_ms": 1,
                "hint": None,
            },
        }
        return list(reversed(results))[:requested_limit], meta


@contextmanager
def _patched_leaf(recorder: _Recorder) -> Iterator[None]:
    with patch.object(
        SemanticSearchService,
        "_perform_semantic_search",
        autospec=True,
        side_effect=recorder.leaf,
    ):
        yield


@pytest.fixture
def manager(tmp_path) -> SemanticQueryManager:
    """Real SemanticQueryManager with production defaults (data_dir in tmp)."""
    mgr = SemanticQueryManager(
        data_dir=str(tmp_path / "data"),
        activated_repo_manager=MagicMock(),
        background_job_manager=MagicMock(),
    )
    # Deterministic single-provider routing (no env-dependent parallel fan-out).
    mgr._both_providers_configured = lambda repo_path: False  # type: ignore[method-assign]
    return mgr


@pytest.fixture
def repo_dir(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    return path


def _mcp_response_payload(response: Dict[str, Any]) -> Dict[str, Any]:
    import json

    return json.loads(response["content"][0]["text"])  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# MCP search_code -- global repository
# ---------------------------------------------------------------------------


@contextmanager
def _global_repo_env(manager, repo_dir, recorder: _Recorder) -> Iterator[None]:
    utils_mock = MagicMock()
    utils_mock.app_module.semantic_query_manager = manager
    utils_mock.app_module.golden_repo_manager = None
    cfg = _make_config_service()
    with (
        patch(f"{_SEARCH_PKG}.repo_search._utils", utils_mock),
        patch(
            f"{_SEARCH_PKG}.repo_search._resolve_global_repo_target",
            return_value=({"repo_name": "example-repo"}, str(repo_dir), None),
        ),
        patch(f"{_SEARCH_PKG}.repo_search.get_config_service", return_value=cfg),
        patch(f"{_SEARCH_PKG}._shared.get_config_service", return_value=cfg),
        patch(
            f"{_SEARCH_PKG}._shared._get_access_filtering_service", return_value=None
        ),
        patch(f"{_SEARCH_PKG}.repo_search._get_query_tracker", return_value=None),
        patch(f"{_SEARCH_PKG}.repo_search._get_wiki_enabled_repos", return_value=set()),
        patch(f"{_SEARCH_PKG}.repo_search._load_category_map", return_value={}),
        patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            side_effect=recorder.rerank,
        ),
        _patched_leaf(recorder),
    ):
        yield


class TestMcpGlobalRepoOverfetch:
    def test_global_search_code_rerank_limit_60_returns_results(
        self, manager, repo_dir
    ):
        """limit=60 + rerank -> internal limit 200 must search, not fail."""
        from code_indexer.server.mcp.handlers.search.code_search import search_code

        recorder = _Recorder()
        params = {
            "repository_alias": "example-repo-global",
            "query_text": "authentication handler",
            "rerank_query": "where is authentication handled",
            "limit": 60,
        }
        with _global_repo_env(manager, repo_dir, recorder):
            payload = _mcp_response_payload(search_code(params, _make_user()))

        assert payload["success"] is True, payload
        assert len(payload["results"]["results"]) == 60
        assert recorder.leaf_limits == [_MAX_CANDIDATES]
        assert recorder.rerank_candidate_counts == [_MAX_CANDIDATES]

    def test_global_search_code_non_admin_limit_100_access_filter_overfetch(
        self, manager, repo_dir
    ):
        """Non-admin limit=100, no rerank -> access-filter over-fetch 200."""
        from code_indexer.server.mcp.handlers.search.code_search import search_code
        from code_indexer.server.services.access_filtering_service import (
            AccessFilteringService,
        )

        access_svc = AccessFilteringService(MagicMock())
        recorder = _Recorder()
        params = {
            "repository_alias": "example-repo-global",
            "query_text": "authentication handler",
            "limit": 100,
        }
        with (
            _global_repo_env(manager, repo_dir, recorder),
            patch(
                f"{_SEARCH_PKG}._shared._get_access_filtering_service",
                return_value=access_svc,
            ),
            patch.object(access_svc, "is_admin_user", return_value=False),
            patch.object(
                access_svc, "filter_query_results", side_effect=lambda r, u: r
            ),
        ):
            payload = _mcp_response_payload(
                search_code(params, _make_user(UserRole.NORMAL_USER))
            )

        assert payload["success"] is True, payload
        assert len(payload["results"]["results"]) == 100
        assert recorder.leaf_limits == [_MAX_CANDIDATES]


# ---------------------------------------------------------------------------
# MCP search_code -- activated repository
# ---------------------------------------------------------------------------


class TestMcpActivatedRepoOverfetch:
    def test_activated_search_code_rerank_limit_60_returns_results(
        self, manager, repo_dir
    ):
        from code_indexer.server.mcp.handlers.search.code_search import search_code

        manager.activated_repo_manager.list_activated_repositories.return_value = [
            {
                "user_alias": "example-repo",
                "username": "example-user",
                "repo_path": str(repo_dir),
            }
        ]
        utils_mock = MagicMock()
        utils_mock.app_module.semantic_query_manager = manager
        cfg = _make_config_service()
        recorder = _Recorder()
        params = {
            "repository_alias": "example-repo",
            "query_text": "authentication handler",
            "rerank_query": "where is authentication handled",
            "limit": 60,
        }
        with (
            patch(f"{_SEARCH_PKG}.repo_search._utils", utils_mock),
            patch(f"{_SEARCH_PKG}.repo_search.get_config_service", return_value=cfg),
            patch(f"{_SEARCH_PKG}._shared.get_config_service", return_value=cfg),
            patch(
                f"{_SEARCH_PKG}._shared._get_access_filtering_service",
                return_value=None,
            ),
            patch(f"{_SEARCH_PKG}.repo_search._get_query_tracker", return_value=None),
            patch(f"{_SEARCH_PKG}.repo_search._load_category_map", return_value={}),
            patch(
                "code_indexer.server.mcp.reranking._apply_reranking_sync",
                side_effect=recorder.rerank,
            ),
            _patched_leaf(recorder),
        ):
            payload = _mcp_response_payload(search_code(params, _make_user()))

        assert payload["success"] is True, payload
        assert len(payload["results"]["results"]) == 60
        assert recorder.leaf_limits == [_MAX_CANDIDATES]
        assert recorder.rerank_candidate_counts == [_MAX_CANDIDATES]


# ---------------------------------------------------------------------------
# REST POST /api/query with rerank_query
# ---------------------------------------------------------------------------


class TestRestQueryRerankOverfetch:
    @pytest.mark.parametrize("limit, fetch", [(21, 105), (60, _MAX_CANDIDATES)])
    def test_rest_query_rerank_limit_above_20_returns_results(
        self, manager, repo_dir, limit, fetch
    ):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from code_indexer.server.auth import dependencies as auth_deps
        from code_indexer.server.routers.inline_query import register_query_routes

        arm = manager.activated_repo_manager
        arm.list_activated_repositories.return_value = [
            {
                "user_alias": "example-repo",
                "username": "example-user",
                "repo_path": str(repo_dir),
            }
        ]
        app = FastAPI()
        app.state.payload_cache = None
        app.state.access_filtering_service = None
        app.state.search_event_log_writer = None
        register_query_routes(
            app, semantic_query_manager=manager, activated_repo_manager=arm
        )
        app.dependency_overrides[auth_deps.get_current_user] = _make_user
        recorder = _Recorder()
        with (
            patch(
                "code_indexer.server.services.config_service.get_config_service",
                return_value=_make_config_service(),
            ),
            patch(
                "code_indexer.server.routers.inline_query._rest_apply_reranking_sync",
                side_effect=recorder.rerank,
            ),
            _patched_leaf(recorder),
        ):
            response = TestClient(app).post(
                "/api/query",
                json={
                    "query_text": "authentication handler",
                    "repository_alias": "example-repo",
                    "limit": limit,
                    "rerank_query": "where is authentication handled",
                },
            )

        assert response.status_code == 200, response.text
        assert len(response.json()["results"]) == limit
        assert recorder.leaf_limits == [fetch]
        assert recorder.rerank_candidate_counts == [fetch]


# ---------------------------------------------------------------------------
# Public contract: SemanticSearchRequest keeps le=100
# ---------------------------------------------------------------------------


class TestPublicContractUnchanged:
    def test_public_v2_search_endpoint_rejects_limit_101(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from code_indexer.server.auth import dependencies as auth_deps
        from code_indexer.server.routers.inline_repos_v2 import (
            register_repos_v2_routes,
        )

        app = FastAPI()
        register_repos_v2_routes(
            app,
            activated_repo_manager=MagicMock(),
            repository_listing_manager=MagicMock(),
            background_job_manager=MagicMock(),
        )
        app.dependency_overrides[auth_deps.get_current_user] = _make_user
        response = TestClient(app).post(
            "/api/repositories/example-repo/search",
            json={"query": "authentication handler", "limit": 101},
        )

        assert response.status_code == 422
        assert "less than or equal to 100" in response.text

    def test_internal_request_bounds(self):
        from pydantic import ValidationError

        from code_indexer.server.models.api_models import (
            MAX_CANDIDATE_LIMIT,
            InternalSemanticSearchRequest,
            SemanticSearchRequest,
        )

        assert MAX_CANDIDATE_LIMIT == _MAX_CANDIDATES
        req = InternalSemanticSearchRequest(query="q", limit=MAX_CANDIDATE_LIMIT)
        assert req.limit == MAX_CANDIDATE_LIMIT
        with pytest.raises(ValidationError):
            InternalSemanticSearchRequest(query="q", limit=MAX_CANDIDATE_LIMIT + 1)
        with pytest.raises(ValidationError):
            SemanticSearchRequest(query="q", limit=101)


# ---------------------------------------------------------------------------
# Provider-specific search (parallel / specific query strategies)
# ---------------------------------------------------------------------------


class TestProviderSpecificOverfetch:
    def test_search_with_provider_accepts_internal_limit_200(self, manager, repo_dir):
        recorder = _Recorder()
        with _patched_leaf(recorder):
            results = manager._search_with_provider(
                repo_path=str(repo_dir),
                repository_alias="example-repo",
                query_text="authentication handler",
                limit=_MAX_CANDIDATES,
                min_score=None,
                file_extensions=None,
                provider_name="voyage-ai",
            )

        assert len(results) == _MAX_CANDIDATES
        assert recorder.leaf_limits == [_MAX_CANDIDATES]


# ---------------------------------------------------------------------------
# Access-filter over-fetch bound
# ---------------------------------------------------------------------------


class TestAccessFilterOverfetchBound:
    @pytest.mark.parametrize(
        "requested, expected",
        [
            (10, 20),
            (100, 200),
            (150, _MAX_CANDIDATES),
            (200, _MAX_CANDIDATES),
            (250, 250),
        ],
    )
    def test_over_fetch_is_bounded_by_candidate_cap(self, requested, expected):
        """limit*2 is capped at MAX_CANDIDATE_LIMIT, never below the request."""
        from code_indexer.server.services.access_filtering_service import (
            AccessFilteringService,
        )

        service = AccessFilteringService(MagicMock())
        assert service.calculate_over_fetch_limit(requested) == expected


# ---------------------------------------------------------------------------
# MCP omni search_code through the real door (from_config + singleton)
# ---------------------------------------------------------------------------


class TestMcpOmniConfiguredPerRepoCap:
    def test_omni_search_code_honours_configured_per_repo_cap(self, repo_dir):
        """Configured omni_max_results_per_repo=500 + non-admin over-fetch.

        limit=100 -> access-filter over-fetch 200; the operator's per-repo
        value (500) must reach the live service via from_config(), so the
        per-repo search retrieves the full 200 candidates with no
        validation error.
        """
        import code_indexer.server.app as app_module
        from code_indexer.server.mcp.handlers.search.code_search import search_code
        from code_indexer.server.multi.multi_search_service import (
            MultiSearchService,
        )
        from code_indexer.server.services.access_filtering_service import (
            AccessFilteringService,
        )
        from code_indexer.server.utils.config_manager import (
            MultiSearchLimitsConfig,
        )

        cfg = _make_config_service()
        cfg.get_config.return_value.multi_search_limits_config = (
            MultiSearchLimitsConfig(
                omni_max_results_per_repo=500, multi_search_max_workers=2
            )
        )
        access_svc = AccessFilteringService(MagicMock())
        fake_app = MagicMock()
        fake_app.state.payload_cache = None
        recorder = _Recorder()
        params = {
            "repository_alias": ["example-repo-global"],
            "query_text": "authentication handler",
            "limit": 100,
        }
        omni = f"{_SEARCH_PKG}.omni"
        MultiSearchService._reset_singleton()
        try:
            with (
                # Pre-seed lazy app attributes so no full create_app() runs.
                patch.dict(
                    app_module.__dict__,
                    {"_server_hnsw_cache": None, "app": fake_app},
                ),
                patch(
                    f"{omni}._expand_wildcard_patterns",
                    side_effect=lambda patterns, user, search_mode: patterns,
                ),
                patch(f"{omni}.get_config_service", return_value=cfg),
                patch(
                    "code_indexer.server.mcp.handlers._utils.get_config_service",
                    return_value=cfg,
                ),
                patch(f"{_SEARCH_PKG}._shared.get_config_service", return_value=cfg),
                patch(f"{omni}._get_access_filtering_service", return_value=access_svc),
                patch(
                    f"{_SEARCH_PKG}._shared._get_access_filtering_service",
                    return_value=access_svc,
                ),
                patch.object(access_svc, "is_admin_user", return_value=False),
                patch.object(
                    access_svc, "filter_query_results", side_effect=lambda r, u: r
                ),
                patch(
                    f"{omni}._compute_shared_query_vector", return_value=(None, None)
                ),
                patch(f"{omni}._get_wiki_enabled_repos", return_value=set()),
                patch(f"{omni}._load_category_map", return_value={}),
                patch.object(
                    MultiSearchService,
                    "_get_repository_path",
                    return_value=str(repo_dir),
                ),
                _patched_leaf(recorder),
            ):
                payload = _mcp_response_payload(
                    search_code(params, _make_user(UserRole.NORMAL_USER))
                )
        finally:
            MultiSearchService._reset_singleton()

        assert payload["success"] is True, payload
        assert not payload["results"]["errors"], payload["results"]["errors"]
        assert len(payload["results"]["results"]) == 100
        assert recorder.leaf_limits == [_MAX_CANDIDATES]


# ---------------------------------------------------------------------------
# Omni / multi-search with an operator-raised per-repo result cap
# ---------------------------------------------------------------------------


class TestMultiSearchRaisedPerRepoCap:
    @pytest.mark.parametrize(
        "request_limit, expected", [(150, 150), (500, _MAX_CANDIDATES)]
    )
    def test_semantic_per_repo_search_with_raised_setting(
        self, repo_dir, request_limit, expected
    ):
        from code_indexer.server.multi.models import MultiSearchRequest
        from code_indexer.server.multi.multi_search_config import MultiSearchConfig
        from code_indexer.server.multi.multi_search_service import (
            MultiSearchService,
        )

        service = MultiSearchService(MultiSearchConfig(max_results_per_repo=500))
        request = MultiSearchRequest.model_validate(
            {
                "repositories": ["example-repo-global"],
                "query": "authentication handler",
                "search_type": "semantic",
                "limit": request_limit,
            }
        )
        recorder = _Recorder()
        try:
            with (
                patch.object(
                    service, "_get_repository_path", return_value=str(repo_dir)
                ),
                _patched_leaf(recorder),
            ):
                results = service._search_semantic_sync("example-repo-global", request)
        finally:
            service.thread_executor.shutdown(wait=True)

        assert len(results) == expected
        assert recorder.leaf_limits == [expected]


# ---------------------------------------------------------------------------
# Omni FTS / temporal / regex per-repo limits are bounded by the cap too
# ---------------------------------------------------------------------------


@contextmanager
def _raised_cap_service() -> Iterator[Any]:
    from code_indexer.server.multi.multi_search_config import MultiSearchConfig
    from code_indexer.server.multi.multi_search_service import MultiSearchService

    service = MultiSearchService(MultiSearchConfig(max_results_per_repo=500))
    try:
        yield service
    finally:
        service.thread_executor.shutdown(wait=True)


def _raised_cap_request(search_type: str) -> Any:
    from code_indexer.server.multi.models import MultiSearchRequest

    return MultiSearchRequest.model_validate(
        {
            "repositories": ["example-repo-global"],
            "query": "authentication handler",
            "search_type": search_type,
            "limit": 1000,
        }
    )


class TestMultiSearchNonSemanticPathsBounded:
    def test_fts_per_repo_limit_bounded(self, repo_dir):
        (repo_dir / ".code-indexer" / "tantivy_index").mkdir(parents=True)
        manager_cls = MagicMock()
        manager_cls.return_value.search.return_value = []
        with (
            _raised_cap_service() as service,
            patch.object(service, "_get_repository_path", return_value=str(repo_dir)),
            patch(
                "code_indexer.services.tantivy_index_manager.TantivyIndexManager",
                manager_cls,
            ),
        ):
            service._search_fts_sync("example-repo-global", _raised_cap_request("fts"))

        limit = manager_cls.return_value.search.call_args.kwargs["limit"]
        assert limit == _MAX_CANDIDATES

    def test_temporal_per_repo_limit_bounded(self, repo_dir, tmp_path):
        import code_indexer.config as config_mod
        import code_indexer.services.temporal.temporal_fusion_dispatch as fusion_mod
        import code_indexer.services.temporal.temporal_server_paths as paths_mod
        import code_indexer.server.utils.server_managed_provider_settings as sm_mod
        import code_indexer.storage.filesystem_vector_store as fvs_mod

        fusion = MagicMock()
        fusion.return_value.warning = None
        fusion.return_value.results = []
        with (
            _raised_cap_service() as service,
            patch.object(service, "_get_repository_path", return_value=str(repo_dir)),
            patch.object(
                config_mod.ConfigManager,
                "load_verified_config",
                return_value=MagicMock(),
            ),
            patch.object(sm_mod, "enforce_server_managed_provider_settings"),
            patch.object(
                paths_mod,
                "resolve_golden_repo_coordinates",
                return_value=(tmp_path, "example-repo"),
            ),
            patch.object(fvs_mod, "FilesystemVectorStore", MagicMock()),
            patch.object(fusion_mod, "execute_temporal_query_with_fusion", fusion),
        ):
            service._search_temporal_sync(
                "example-repo-global", _raised_cap_request("temporal")
            )

        assert fusion.call_args.kwargs["limit"] == _MAX_CANDIDATES

    def test_regex_subprocess_per_repo_limit_bounded(self, repo_dir):
        import subprocess

        run = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
        )
        with (
            _raised_cap_service() as service,
            patch.object(service, "_get_repository_path", return_value=str(repo_dir)),
            patch("code_indexer.server.multi.multi_search_service.subprocess.run", run),
        ):
            service._search_single_repo_subprocess(
                "example-repo-global", _raised_cap_request("regex")
            )

        cmd = run.call_args.args[0]
        assert cmd[cmd.index("--limit") + 1] == str(_MAX_CANDIDATES)
