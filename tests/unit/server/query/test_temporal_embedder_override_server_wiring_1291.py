"""Story #1291 code-review Finding 2 (MEDIUM) — temporal_embedder query
override wired through the SERVER front door (REST + MCP).

The `temporal_embedder` explicit-override parameter (AC7/AC8) worked via the
shared fusion dispatch (`execute_temporal_query_with_fusion`) and the CLI
(`--temporal-embedder`), but the two SERVER call sites did NOT thread it:
`semantic_query_manager.py` (REST `/api/query` + MCP `search_code`, single
repo) and `multi_search_service.py` (MCP omni/multi-repo search). Since
temporal search runs through REST/MCP in production, AC7/AC8 were
unreachable server-side.

This file proves the override is wired end-to-end at every hop:
  1. SemanticQueryManager._execute_temporal_query forwards temporal_embedder
     to execute_temporal_query_with_fusion (direct dispatch proof).
  2. query_user_repositories threads temporal_embedder all the way through
     _perform_search -> _search_single_repository -> _execute_temporal_query
     (full public-API-to-dispatch proof, used by the REST route).
  3. MCP handlers._build_search_kwargs includes temporal_embedder from params
     (the dict consumed by _perform_search on the MCP search_code path).
  4. REST SemanticQueryRequest model exposes temporal_embedder: Optional[str].
  5. The REST /api/query route handler passes request.temporal_embedder into
     query_user_repositories at BOTH call sites (hybrid mode + default mode).
  6. MultiSearchRequest model exposes temporal_embedder: Optional[str] (omni
     multi-repo search).
  7. MultiSearchService._search_temporal_sync forwards
     temporal_embedder=request.temporal_embedder to fusion dispatch.
  8. MCP handlers._build_multi_search_request threads temporal_embedder from
     params into MultiSearchRequest (the omni/multi-repo MCP path).
  9. The search_code MCP tool doc declares temporal_embedder in its
     inputSchema.properties.
"""

from __future__ import annotations

import inspect
import logging
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# 1. SemanticQueryManager._execute_temporal_query forwards temporal_embedder
# ---------------------------------------------------------------------------


def _enter_semantic_manager_patches(stack: ExitStack):
    """Patch the three heavy lazy imports inside _execute_temporal_query."""
    mock_fusion = stack.enter_context(
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch"
            ".execute_temporal_query_with_fusion"
        )
    )
    mock_cm_cls = stack.enter_context(
        patch("code_indexer.proxy.config_manager.ConfigManager")
    )
    mock_bf = stack.enter_context(
        patch("code_indexer.backends.backend_factory.BackendFactory")
    )

    mock_config = MagicMock()
    mock_cm_instance = MagicMock()
    mock_cm_instance.get_config.return_value = mock_config
    mock_cm_cls.create_with_backtrack.return_value = mock_cm_instance

    mock_backend = MagicMock()
    mock_vector_store = MagicMock()
    mock_backend.get_vector_store_client.return_value = mock_vector_store
    mock_bf.create.return_value = mock_backend

    fake_results = MagicMock()
    fake_results.results = []
    fake_results.warning = None
    fake_results.query = "test"
    fake_results.filter_type = "none"
    fake_results.filter_value = None
    fake_results.total_found = 0
    mock_fusion.return_value = fake_results

    return mock_fusion, mock_cm_cls, mock_bf


class TestExecuteTemporalQueryForwardsEmbedderOverride:
    """Direct dispatch proof: _execute_temporal_query must accept and forward
    temporal_embedder to execute_temporal_query_with_fusion."""

    def test_temporal_embedder_forwarded_to_fusion(self):
        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryManager,
        )

        manager = SemanticQueryManager.__new__(SemanticQueryManager)

        with ExitStack() as stack:
            mock_fusion, _, _ = _enter_semantic_manager_patches(stack)

            manager._execute_temporal_query(
                repo_path=Path("/tmp/test-repo"),
                repository_alias="test-repo",
                query_text="auth code",
                limit=10,
                min_score=None,
                time_range=None,
                time_range_all=True,
                temporal_embedder="embed-v4.0",
            )

        mock_fusion.assert_called_once()
        _, kwargs = mock_fusion.call_args
        assert kwargs.get("temporal_embedder") == "embed-v4.0", (
            f"temporal_embedder='embed-v4.0' must reach "
            f"execute_temporal_query_with_fusion; got kwargs={kwargs}"
        )

    def test_temporal_embedder_omitted_defaults_none(self):
        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryManager,
        )

        manager = SemanticQueryManager.__new__(SemanticQueryManager)

        with ExitStack() as stack:
            mock_fusion, _, _ = _enter_semantic_manager_patches(stack)

            manager._execute_temporal_query(
                repo_path=Path("/tmp/test-repo"),
                repository_alias="test-repo",
                query_text="auth code",
                limit=10,
                min_score=None,
                time_range=None,
                time_range_all=True,
            )

        _, kwargs = mock_fusion.call_args
        assert kwargs.get("temporal_embedder") is None


# ---------------------------------------------------------------------------
# 2. query_user_repositories end-to-end threading (REST entry point)
# ---------------------------------------------------------------------------


class TestQueryUserRepositoriesThreadsEmbedderOverride:
    """query_user_repositories must accept temporal_embedder and thread it
    through _perform_search -> _search_single_repository ->
    _execute_temporal_query (mocked at the class boundary; its own forwarding
    to fusion dispatch is proven above)."""

    def test_flag_reaches_execute_temporal_query(self, monkeypatch, tmp_path):
        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryManager,
        )

        captured: list = []

        def _fake_execute_temporal_query(self, **kwargs):
            captured.append(kwargs.get("temporal_embedder"))
            result = MagicMock()
            result.results = []
            return []

        monkeypatch.setattr(
            SemanticQueryManager,
            "_execute_temporal_query",
            _fake_execute_temporal_query,
        )

        manager = SemanticQueryManager.__new__(SemanticQueryManager)
        manager.max_results_per_query = 50
        manager.logger = logging.getLogger("test.semantic_query_manager")

        class _FakeARM:
            activated_repos_dir = str(tmp_path / "activated-repos")

            def list_activated_repositories(self, _u):
                return [{"user_alias": "myrepo", "repo_path": str(tmp_path)}]

            def get_activated_repo_path(self, _u, _a):
                return str(tmp_path)

        manager.activated_repo_manager = _FakeARM()

        import code_indexer.server.app as _app_mod

        class _FakeState:
            backend_registry = None
            http_client_factory = None

        class _FakeApp:
            state = _FakeState()

        monkeypatch.setattr(_app_mod, "app", _FakeApp(), raising=False)

        manager.query_user_repositories(
            username="alice",
            query_text="find auth",
            repository_alias="myrepo",
            time_range_all=True,
            query_strategy="primary_only",
            temporal_embedder="embed-v4.0",
        )

        assert captured, (
            "_execute_temporal_query was never called — temporal path not entered"
        )
        assert all(v == "embed-v4.0" for v in captured), (
            f"temporal_embedder='embed-v4.0' must reach _execute_temporal_query "
            f"end-to-end through query_user_repositories; captured={captured}"
        )


# ---------------------------------------------------------------------------
# 3. MCP handlers._build_search_kwargs includes temporal_embedder
# ---------------------------------------------------------------------------


class TestBuildSearchKwargsIncludesTemporalEmbedder:
    """_build_search_kwargs must include temporal_embedder from params so the
    MCP search_code path (which calls _perform_search directly) can thread
    the override."""

    def test_present_when_provided(self):
        from code_indexer.server.mcp.handlers import search as search_handler

        class _User:
            username = "alice"

        params = {"query_text": "find auth", "temporal_embedder": "embed-v4.0"}
        kwargs = search_handler._build_search_kwargs(params, _User(), [], 10)

        assert kwargs.get("temporal_embedder") == "embed-v4.0"

    def test_absent_defaults_to_none(self):
        from code_indexer.server.mcp.handlers import search as search_handler

        class _User:
            username = "alice"

        params = {"query_text": "find auth"}
        kwargs = search_handler._build_search_kwargs(params, _User(), [], 10)

        assert kwargs.get("temporal_embedder") is None


# ---------------------------------------------------------------------------
# 4. REST SemanticQueryRequest model field
# ---------------------------------------------------------------------------


class TestSemanticQueryRequestTemporalEmbedderField:
    def test_default_is_none(self):
        from code_indexer.server.models.query import SemanticQueryRequest

        req = SemanticQueryRequest(query_text="test")
        assert req.temporal_embedder is None

    def test_set_value(self):
        from code_indexer.server.models.query import SemanticQueryRequest

        req = SemanticQueryRequest(query_text="test", temporal_embedder="embed-v4.0")
        assert req.temporal_embedder == "embed-v4.0"

    def test_parses_from_json(self):
        from code_indexer.server.models.query import SemanticQueryRequest

        req = SemanticQueryRequest.model_validate(
            {"query_text": "test", "temporal_embedder": "embed-v4.0"}
        )
        assert req.temporal_embedder == "embed-v4.0"


# ---------------------------------------------------------------------------
# 5. REST route handler threads request.temporal_embedder (source inspection
#    — the route is a FastAPI closure registered via register_query_routes;
#    a full HTTP-level test would require a live app, which the REST/MCP
#    front-door tests already exercise separately for the other params).
# ---------------------------------------------------------------------------


class TestInlineQueryRouteThreadsEmbedderOverride:
    def _route_source(self) -> str:
        from code_indexer.server.routers import inline_query

        return inspect.getsource(inline_query.register_query_routes)

    def test_both_query_user_repositories_call_sites_pass_temporal_embedder(self):
        source = self._route_source()
        occurrences = source.count("temporal_embedder=request.temporal_embedder")
        assert occurrences >= 2, (
            "Both query_user_repositories() call sites (hybrid mode and "
            "default semantic mode) must pass temporal_embedder=request."
            f"temporal_embedder. Found {occurrences} occurrence(s)."
        )


# ---------------------------------------------------------------------------
# 6. MultiSearchRequest model field (omni multi-repo search)
# ---------------------------------------------------------------------------


class TestMultiSearchRequestTemporalEmbedderField:
    def test_default_is_none(self):
        from code_indexer.server.multi.models import MultiSearchRequest

        req = MultiSearchRequest(
            repositories=["repo1"], query="test", search_type="temporal", limit=10
        )
        assert req.temporal_embedder is None

    def test_set_value(self):
        from code_indexer.server.multi.models import MultiSearchRequest

        req = MultiSearchRequest(
            repositories=["repo1"],
            query="test",
            search_type="temporal",
            limit=10,
            temporal_embedder="embed-v4.0",
        )
        assert req.temporal_embedder == "embed-v4.0"


# ---------------------------------------------------------------------------
# 7. MultiSearchService._search_temporal_sync forwards temporal_embedder
#    (source inspection — matches the established pattern in
#    test_temporal_cache_injection_1170.py / test_multi_search_filter_wiring.py
#    for this exact method, to avoid heavy local-import patching).
# ---------------------------------------------------------------------------


class TestSearchTemporalSyncThreadsEmbedderOverride:
    def test_search_temporal_sync_passes_temporal_embedder(self):
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        source = inspect.getsource(MultiSearchService._search_temporal_sync)
        assert "temporal_embedder=request.temporal_embedder" in source, (
            "_search_temporal_sync must pass "
            "temporal_embedder=request.temporal_embedder to "
            "execute_temporal_query_with_fusion"
        )


# ---------------------------------------------------------------------------
# 8. MCP handlers._build_multi_search_request threads temporal_embedder
# ---------------------------------------------------------------------------


class TestBuildMultiSearchRequestThreadsEmbedderOverride:
    def test_build_multi_search_request_passes_temporal_embedder(self):
        from code_indexer.server.mcp.handlers import search as search_handler

        source = inspect.getsource(search_handler._build_multi_search_request)
        assert 'temporal_embedder=params.get("temporal_embedder")' in source, (
            "_build_multi_search_request must pass "
            'temporal_embedder=params.get("temporal_embedder") into '
            "MultiSearchRequest"
        )


# ---------------------------------------------------------------------------
# 9. search_code MCP tool doc declares temporal_embedder
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parents[4]
_TOOL_DOC_PATH = (
    _PROJECT_ROOT
    / "src"
    / "code_indexer"
    / "server"
    / "mcp"
    / "tool_docs"
    / "search"
    / "search_code.md"
)


class TestToolDocHasTemporalEmbedder:
    def test_tool_doc_has_temporal_embedder_property(self):
        import yaml

        content = _TOOL_DOC_PATH.read_text(encoding="utf-8")
        parts = content.split("---", 2)
        frontmatter = yaml.safe_load(parts[1])
        props = frontmatter.get("inputSchema", {}).get("properties", {})
        assert "temporal_embedder" in props, (
            "inputSchema.properties must contain 'temporal_embedder'"
        )

    def test_tool_doc_property_is_string_type(self):
        import yaml

        content = _TOOL_DOC_PATH.read_text(encoding="utf-8")
        parts = content.split("---", 2)
        frontmatter = yaml.safe_load(parts[1])
        prop = frontmatter["inputSchema"]["properties"]["temporal_embedder"]
        assert prop.get("type") == "string", (
            f"temporal_embedder must be type: string, got {prop.get('type')!r}"
        )
