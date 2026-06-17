"""Story #1108 (S4) — per-request no_embedding_cache_shortcut for query-embedding cache.

Acceptance Criteria:
  AC1 — SemanticSearchRequest gains no_embedding_cache_shortcut: bool = False.
         MCP tool doc search_code.md declares no_embedding_cache_shortcut in
         inputSchema.properties (type: boolean, default false).
  AC2 — coalesced_query_embedding accepts keyword-only no_embedding_cache_shortcut: bool = False.
         All 4 caller layers thread it from request/param.
  AC3 — Bypass wrap semantics:
         bypass=True + mode=on  -> skip lookup/record_hit; compute live; write record_miss_or_shadow.
         bypass=False + hit     -> cached vec returned; provider NOT called.
         bypass=True + mode=off -> no lookup, no write (mode=off gate fires first).
         default-False miss     -> lookup called; live computed; record_miss_or_shadow called.
"""

from __future__ import annotations

import ast
import inspect
import logging
import struct
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services import governed_call
from code_indexer.server.services.query_embedding_cache import (
    QueryEmbeddingCache,
    build_key,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LIVE_VEC: List[float] = [1.0, 2.0, 3.0]
CACHED_VEC: List[float] = [9.0, 8.0, 7.0]
PROVIDER_NAME = "voyage-ai"
MODEL_NAME = "voyage-code-3"
DIMENSION = 3
TEST_TEXT = "hello world"

# Source roots
_SRC_ROOT = Path(__file__).parents[4] / "src" / "code_indexer"
_TOOL_DOC = (
    _SRC_ROOT.parent
    / "code_indexer"
    / "server"
    / "mcp"
    / "tool_docs"
    / "search"
    / "search_code.md"
)

# Correct path relative to this test file (4 parents up = project root)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cached_bytes(vec: List[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


class _FakeVoyageProvider:
    def get_provider_name(self) -> str:
        return PROVIDER_NAME

    def get_current_model(self) -> str:
        return MODEL_NAME

    def get_model_info(self) -> dict:
        return {"dimensions": DIMENSION}

    def get_embedding(
        self, text: str, *, embedding_purpose: Optional[str] = None
    ) -> List[float]:
        return LIVE_VEC


def _make_cache(
    *,
    enabled: bool = True,
    voyage_mode: str = "on",
    hit_bytes: Optional[bytes] = None,
) -> MagicMock:
    """Build a MagicMock QueryEmbeddingCache with the REAL interface."""
    cache = MagicMock(spec=QueryEmbeddingCache)
    cache.enabled_for.return_value = enabled
    cache.mode_for.return_value = voyage_mode
    cache.lookup.return_value = hit_bytes
    cache.build_key_for_provider = (
        lambda text, provider_name, *, config_digest="test-digest": build_key(
            text, 2, config_digest=config_digest
        )
    )
    cache.qualifier.return_value = MagicMock(
        provider=PROVIDER_NAME, model=MODEL_NAME, dimension=DIMENSION
    )
    return cache


# ---------------------------------------------------------------------------
# AC1 — api_models: SemanticSearchRequest.no_embedding_cache_shortcut field
# ---------------------------------------------------------------------------


class TestApiModelsField:
    """SemanticSearchRequest must expose no_embedding_cache_shortcut: bool = False."""

    def test_default_is_false(self):
        from code_indexer.server.models.api_models import SemanticSearchRequest

        req = SemanticSearchRequest(query="test")
        assert req.no_embedding_cache_shortcut is False

    def test_set_true(self):
        from code_indexer.server.models.api_models import SemanticSearchRequest

        req = SemanticSearchRequest(query="test", no_embedding_cache_shortcut=True)
        assert req.no_embedding_cache_shortcut is True

    def test_parses_from_json(self):
        from code_indexer.server.models.api_models import SemanticSearchRequest

        req = SemanticSearchRequest.model_validate(
            {"query": "test", "no_embedding_cache_shortcut": True}
        )
        assert req.no_embedding_cache_shortcut is True

    def test_absent_from_json_defaults_false(self):
        from code_indexer.server.models.api_models import SemanticSearchRequest

        req = SemanticSearchRequest.model_validate({"query": "test"})
        assert req.no_embedding_cache_shortcut is False


# ---------------------------------------------------------------------------
# AC1 — tool doc: search_code.md declares no_embedding_cache_shortcut
# ---------------------------------------------------------------------------


class TestToolDoc:
    """search_code.md inputSchema.properties must declare no_embedding_cache_shortcut."""

    def test_tool_doc_exists(self):
        assert _TOOL_DOC_PATH.exists(), f"Tool doc not found at {_TOOL_DOC_PATH}"

    def test_tool_doc_has_no_embedding_cache_shortcut_property(self):
        import yaml

        content = _TOOL_DOC_PATH.read_text(encoding="utf-8")
        assert content.startswith("---"), "Tool doc must start with YAML frontmatter"
        parts = content.split("---", 2)
        assert len(parts) >= 3, "Tool doc must have frontmatter section"
        frontmatter = yaml.safe_load(parts[1])
        assert isinstance(frontmatter, dict), "Frontmatter must be a dict"
        props = frontmatter.get("inputSchema", {}).get("properties", {})
        assert "no_embedding_cache_shortcut" in props, (
            "inputSchema.properties must contain 'no_embedding_cache_shortcut'"
        )

    def test_tool_doc_property_is_boolean_type(self):
        import yaml

        content = _TOOL_DOC_PATH.read_text(encoding="utf-8")
        parts = content.split("---", 2)
        frontmatter = yaml.safe_load(parts[1])
        prop = frontmatter["inputSchema"]["properties"]["no_embedding_cache_shortcut"]
        assert prop.get("type") == "boolean", (
            f"no_embedding_cache_shortcut must be type: boolean, got {prop.get('type')!r}"
        )

    def test_tool_doc_property_has_default_false(self):
        import yaml

        content = _TOOL_DOC_PATH.read_text(encoding="utf-8")
        parts = content.split("---", 2)
        frontmatter = yaml.safe_load(parts[1])
        prop = frontmatter["inputSchema"]["properties"]["no_embedding_cache_shortcut"]
        assert prop.get("default") is False, (
            f"no_embedding_cache_shortcut must have default: false, got {prop.get('default')!r}"
        )


# ---------------------------------------------------------------------------
# AC2 — coalesced_query_embedding signature
# ---------------------------------------------------------------------------


class TestCoalescedQueryEmbeddingSignature:
    """coalesced_query_embedding must accept no_embedding_cache_shortcut kwarg."""

    def test_kwarg_exists_with_default_false(self):
        sig = inspect.signature(governed_call.coalesced_query_embedding)
        assert "no_embedding_cache_shortcut" in sig.parameters, (
            "coalesced_query_embedding must have no_embedding_cache_shortcut param"
        )
        param = sig.parameters["no_embedding_cache_shortcut"]
        assert param.default is False, f"default must be False, got {param.default!r}"
        # Must be keyword-only
        assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
            "no_embedding_cache_shortcut must be keyword-only"
        )

    def test_default_false_unchanged_behavior_no_cache(self, monkeypatch):
        """Default False with no cache must call governed_query_embedding (unchanged)."""
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: None)

        live_calls: list = []

        def _fake_governed(
            provider, text, *, embedding_purpose=None, acquire_timeout=30.0
        ):
            live_calls.append(text)
            return LIVE_VEC

        monkeypatch.setattr(governed_call, "governed_query_embedding", _fake_governed)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT
        )
        assert result == LIVE_VEC
        assert live_calls == [TEST_TEXT]

    def test_bypass_true_no_cache_still_computes_live(self, monkeypatch):
        """bypass=True with no cache still computes live (no cache to skip)."""
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: None)

        live_calls: list = []

        def _fake_governed(
            provider, text, *, embedding_purpose=None, acquire_timeout=30.0
        ):
            live_calls.append(text)
            return LIVE_VEC

        monkeypatch.setattr(governed_call, "governed_query_embedding", _fake_governed)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT, no_embedding_cache_shortcut=True
        )
        assert result == LIVE_VEC
        assert live_calls == [TEST_TEXT]


# ---------------------------------------------------------------------------
# AC2 — 4 caller layers thread the kwarg (AST source-text checks)
# ---------------------------------------------------------------------------


def _read_source(rel_path: str) -> str:
    full = _PROJECT_ROOT / "src" / "code_indexer" / rel_path
    return full.read_text(encoding="utf-8")


def _has_kwarg_in_call(source: str, func_name: str, kwarg: str) -> bool:
    """Return True if any call to func_name in source passes kwarg=... as a keyword arg."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match call whose function name ends with func_name
        fn = node.func
        name = ""
        if isinstance(fn, ast.Name):
            name = fn.id
        elif isinstance(fn, ast.Attribute):
            name = fn.attr
        if name != func_name:
            continue
        for kw in node.keywords:
            if kw.arg == kwarg:
                return True
    return False


class TestCallerLayersThreadKwarg:
    """Each of the 4 caller layers must pass no_embedding_cache_shortcut= to
    coalesced_query_embedding."""

    def test_search_service_threads_kwarg(self):
        source = _read_source("server/services/search_service.py")
        assert _has_kwarg_in_call(
            source, "coalesced_query_embedding", "no_embedding_cache_shortcut"
        ), (
            "search_service.py must pass no_embedding_cache_shortcut= to coalesced_query_embedding"
        )

    def test_mcp_handler_search_threads_kwarg(self):
        source = _read_source("server/mcp/handlers/search.py")
        assert _has_kwarg_in_call(
            source, "coalesced_query_embedding", "no_embedding_cache_shortcut"
        ), (
            "handlers/search.py must pass no_embedding_cache_shortcut= to coalesced_query_embedding"
        )

    def test_temporal_search_service_threads_kwarg(self):
        source = _read_source("services/temporal/temporal_search_service.py")
        assert _has_kwarg_in_call(
            source, "coalesced_query_embedding", "no_embedding_cache_shortcut"
        ), (
            "temporal_search_service.py must pass no_embedding_cache_shortcut= to coalesced_query_embedding"
        )

    def test_filesystem_vector_store_threads_kwarg(self):
        source = _read_source("storage/filesystem_vector_store.py")
        assert _has_kwarg_in_call(
            source, "coalesced_query_embedding", "no_embedding_cache_shortcut"
        ), (
            "filesystem_vector_store.py must pass no_embedding_cache_shortcut= to coalesced_query_embedding"
        )


# ---------------------------------------------------------------------------
# AC3 — Bypass wrap semantics with correctly-shaped fake cache
# ---------------------------------------------------------------------------


class TestBypassWrapSemantics:
    """Validate the bypass branch in coalesced_query_embedding."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self, monkeypatch):
        governed_call.clear_query_embedding_cache()
        yield
        governed_call.clear_query_embedding_cache()

    def _install_cache(self, monkeypatch, cache):
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

    def _fake_live_fn(self, monkeypatch):
        live_calls: list = []

        def _fake(provider, text, *, embedding_purpose=None, acquire_timeout=30.0):
            live_calls.append(text)
            return LIVE_VEC

        monkeypatch.setattr(governed_call, "governed_query_embedding", _fake)
        return live_calls

    def test_bypass_true_mode_on_skips_lookup_computes_live_writes_cache(
        self, monkeypatch
    ):
        """bypass=True + mode=on:
        - lookup NOT called (read skipped)
        - live computed
        - record_miss_or_shadow called once (write still happens)
        - record_hit NOT called
        """
        cache = _make_cache(enabled=True, voyage_mode="on", hit_bytes=None)
        self._install_cache(monkeypatch, cache)
        live_calls = self._fake_live_fn(monkeypatch)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT, no_embedding_cache_shortcut=True
        )

        assert result == LIVE_VEC
        assert live_calls == [TEST_TEXT]
        cache.lookup.assert_not_called()
        cache.record_hit.assert_not_called()
        cache.record_miss_or_shadow.assert_called_once()

    def test_bypass_false_hit_returns_cached_vec_provider_not_called(self, monkeypatch):
        """bypass=False (default) + hit: cached vec returned; live NOT called."""
        cached_bytes = _make_cached_bytes(CACHED_VEC)
        cache = _make_cache(enabled=True, voyage_mode="on", hit_bytes=cached_bytes)
        self._install_cache(monkeypatch, cache)
        live_calls = self._fake_live_fn(monkeypatch)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT, no_embedding_cache_shortcut=False
        )

        # Must return cached vec, not live vec
        assert result == pytest.approx(CACHED_VEC, abs=1e-4)
        # live path must NOT be called
        assert live_calls == []
        cache.lookup.assert_called_once()
        cache.record_hit.assert_called_once()
        cache.record_miss_or_shadow.assert_not_called()

    def test_bypass_true_mode_off_no_lookup_no_write(self, monkeypatch):
        """bypass=True + mode=off:
        The mode=off gate fires FIRST (before bypass check), so:
        - lookup NOT called
        - record_miss_or_shadow NOT called
        - live called (the mode=off path calls live())
        """
        cache = _make_cache(enabled=True, voyage_mode="off", hit_bytes=None)
        self._install_cache(monkeypatch, cache)
        live_calls = self._fake_live_fn(monkeypatch)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT, no_embedding_cache_shortcut=True
        )

        assert result == LIVE_VEC
        assert live_calls == [TEST_TEXT]
        cache.lookup.assert_not_called()
        cache.record_miss_or_shadow.assert_not_called()

    def test_default_false_miss_lookup_called_live_computed_miss_recorded(
        self, monkeypatch
    ):
        """default bypass=False + MISS:
        - lookup called (returns None)
        - live computed
        - record_miss_or_shadow called
        """
        cache = _make_cache(enabled=True, voyage_mode="on", hit_bytes=None)
        self._install_cache(monkeypatch, cache)
        live_calls = self._fake_live_fn(monkeypatch)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT
        )

        assert result == LIVE_VEC
        assert live_calls == [TEST_TEXT]
        cache.lookup.assert_called_once()
        cache.record_miss_or_shadow.assert_called_once()
        cache.record_hit.assert_not_called()

    def test_bypass_true_cache_not_enabled_no_write(self, monkeypatch):
        """bypass=True + cache not enabled for provider:
        The enabled_for gate fires first -> live called, no cache ops.
        """
        cache = _make_cache(enabled=False, voyage_mode="on", hit_bytes=None)
        self._install_cache(monkeypatch, cache)
        live_calls = self._fake_live_fn(monkeypatch)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT, no_embedding_cache_shortcut=True
        )

        assert result == LIVE_VEC
        assert live_calls == [TEST_TEXT]
        cache.lookup.assert_not_called()
        cache.record_miss_or_shadow.assert_not_called()


# ---------------------------------------------------------------------------
# VALUE-FLOW TESTS — prove the boolean VALUE reaches coalesced_query_embedding
# on each primary path (not just that the kwarg exists in source text).
# ---------------------------------------------------------------------------


class TestValueFlowBuildSearchKwargs:
    """_build_search_kwargs must include no_embedding_cache_shortcut from params
    so that callers forwarding kwargs to query_user_repositories can thread the flag.

    _build_search_kwargs is a pure dict-building function — no external deps needed.
    """

    def test_flag_true_present_in_returned_dict(self):
        """When params has no_embedding_cache_shortcut=True it must appear in kwargs."""
        from code_indexer.server.mcp.handlers import search as search_handler

        class _User:
            username = "alice"

        params = {"query_text": "find auth", "no_embedding_cache_shortcut": True}
        kwargs = search_handler._build_search_kwargs(params, _User(), [], 10)

        assert "no_embedding_cache_shortcut" in kwargs, (
            "_build_search_kwargs must include no_embedding_cache_shortcut in returned dict"
        )
        assert kwargs["no_embedding_cache_shortcut"] is True

    def test_flag_absent_defaults_to_false(self):
        """When params omits no_embedding_cache_shortcut, returned dict must default to False."""
        from code_indexer.server.mcp.handlers import search as search_handler

        class _User:
            username = "alice"

        params = {"query_text": "find auth"}
        kwargs = search_handler._build_search_kwargs(params, _User(), [], 10)

        assert "no_embedding_cache_shortcut" in kwargs, (
            "_build_search_kwargs must include no_embedding_cache_shortcut key even when absent from params"
        )
        assert kwargs["no_embedding_cache_shortcut"] is False


class TestValueFlowQueryUserRepositories:
    """query_user_repositories must accept no_embedding_cache_shortcut and thread it
    through _perform_search -> _search_single_repository -> SemanticSearchRequest.

    Strategy: spy at search_service.SemanticSearchService.search_repository_path,
    capturing the search_request argument. The real SemanticSearchRequest and all
    intermediate functions run unmodified; only filesystem I/O is bypassed.
    """

    def test_flag_reaches_semantic_search_request(self, monkeypatch, tmp_path):
        """no_embedding_cache_shortcut=True in query_user_repositories must appear
        as search_request.no_embedding_cache_shortcut=True when search_repository_path
        is called on the semantic search path."""
        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryManager,
        )
        from code_indexer.server.services import search_service as ss_mod
        from code_indexer.server.models.api_models import SemanticSearchResponse

        captured_requests: list = []

        class _SpySearchService(ss_mod.SemanticSearchService):
            def search_repository_path(self, repo_path, search_request, **kw):
                captured_requests.append(search_request.no_embedding_cache_shortcut)
                return SemanticSearchResponse(
                    query=search_request.query, results=[], total=0
                )

        monkeypatch.setattr(ss_mod, "SemanticSearchService", _SpySearchService)

        # Build a real manager with minimal stubbed ARM
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

        # Stub backend_registry so no global-repo lookup fires;
        # http_client_factory must be None-able (search_service reads it via app.state)
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
            # Force primary_only to avoid auto-parallel dispatch which routes
            # through _search_with_provider dicts, bypassing the spy.
            query_strategy="primary_only",
            no_embedding_cache_shortcut=True,
        )

        assert captured_requests, (
            "search_repository_path was never called — semantic search path not entered"
        )
        assert all(v is True for v in captured_requests), (
            f"no_embedding_cache_shortcut=True must reach SemanticSearchRequest "
            f"(and thus search_repository_path), but captured: {captured_requests}"
        )


class TestValueFlowComputeSharedQueryVector:
    """_search_activated_repo must pass no_embedding_cache_shortcut from params
    into coalesced_query_embedding via _compute_shared_query_vector when the
    memory-retrieval shared-vector path is active.

    Stubbed external deps (not system-under-test):
    - VoyageAIClient: external network call
    - _get_http_client_factory: reads app.state (not available in unit test)
    - get_config_service: reads DB / runtime config
    - app_module.semantic_query_manager: full backend search infrastructure
    - app_module.activated_repo_manager: database access

    _compute_shared_query_vector and _compute_memory_query_vector run unmodified.
    """

    def test_flag_reaches_coalesced_query_embedding_via_shared_vector(
        self, monkeypatch
    ):
        """When params has no_embedding_cache_shortcut=True and memory retrieval is
        enabled, _search_activated_repo must call coalesced_query_embedding with
        no_embedding_cache_shortcut=True (via _compute_shared_query_vector)."""
        from code_indexer.server.mcp.handlers import search as search_handler
        from code_indexer.server.services import governed_call as gc

        coalesce_calls: list = []

        def spy_coalesce(provider, text, *, no_embedding_cache_shortcut=False, **kw):
            coalesce_calls.append(no_embedding_cache_shortcut)
            return LIVE_VEC

        monkeypatch.setattr(gc, "coalesced_query_embedding", spy_coalesce)

        # External dep: VoyageAIClient makes network calls — stub it
        import code_indexer.services.voyage_ai as voyage_mod

        class _FakeVoyageClient:
            def __init__(self, *a, **kw):
                pass

            def get_provider_name(self):
                return "voyage-ai"

            def get_current_model(self):
                return "voyage-code-3"

            def get_model_info(self):
                return {"dimensions": 1024}

        monkeypatch.setattr(voyage_mod, "VoyageAIClient", _FakeVoyageClient)

        # External dep: _get_http_client_factory reads app.state — not available here
        import code_indexer.server.services.search_service as ss_mod

        monkeypatch.setattr(
            ss_mod, "_get_http_client_factory", lambda: None, raising=False
        )

        # External dep: get_config_service reads DB/runtime config — stub with
        # memory_retrieval_enabled=True to trigger the shared-vector path.
        # Additional fields read by _run_memory_retrieval's MemoryRetrievalPipelineConfig.
        class _FakeMemCfg:
            memory_retrieval_enabled = True
            memory_voyage_min_score = 0.7
            memory_cohere_min_score = 0.7
            memory_retrieval_k_multiplier = 2
            memory_retrieval_max_body_chars = 5000

        class _FakeCfg:
            memory_retrieval_config = _FakeMemCfg()

        class _FakeConfigService:
            def get_config(self):
                return _FakeCfg()

        monkeypatch.setattr(
            search_handler, "get_config_service", lambda: _FakeConfigService()
        )

        # External dep: app_module.semantic_query_manager is full backend infra;
        # app_module.app.state is accessed for payload_cache (getattr guarded).
        import code_indexer.server.mcp.handlers._utils as _utils_mod

        class _FakeSQM:
            def query_user_repositories(self_, **kw):
                return {"results": [], "total_results": 0, "query_metadata": {}}

        class _FakeAppState:
            payload_cache = None
            http_client_factory = None

        class _FakeApp:
            state = _FakeAppState()

        class _FakeAppModule:
            semantic_query_manager = _FakeSQM()
            activated_repo_manager = None
            app = _FakeApp()

        monkeypatch.setattr(_utils_mod, "app_module", _FakeAppModule(), raising=False)

        params = {
            "query_text": "find auth",
            "search_mode": "semantic",
            "no_embedding_cache_shortcut": True,
            "repository_alias": "myrepo",
            "limit": 5,
        }

        class _FakeUser:
            username = "alice"

        # _search_activated_repo may raise RuntimeError from golden_repos_dir
        # access in the memory-retrieval pipeline — that fires AFTER coalesced_query_embedding
        # is called and the spy captures the flag. Catch only RuntimeError here; any
        # other exception type re-raises so unexpected failures are not hidden.
        try:
            search_handler._search_activated_repo(params, _FakeUser())
        except RuntimeError:
            pass  # golden_repos_dir not set in unit-test env — expected infra gap

        assert coalesce_calls, (
            "coalesced_query_embedding was never called. "
            "Check that search_mode=semantic + memory_retrieval_enabled=True "
            "enters the shared-vector path in _search_activated_repo."
        )
        assert coalesce_calls[0] is True, (
            f"no_embedding_cache_shortcut=True must be forwarded to coalesced_query_embedding "
            f"via _compute_shared_query_vector, but captured: {coalesce_calls}"
        )


class TestValueFlowRunMemoryRetrieval:
    """Value-flow test: _run_memory_retrieval fallback (query_vector=None) must forward
    no_embedding_cache_shortcut from params into coalesced_query_embedding.

    This tests the code path at search.py ~line 593-599:
        if query_vector is None:
            query_vector = _compute_memory_query_vector(
                query_text,
                no_embedding_cache_shortcut=params.get("no_embedding_cache_shortcut", False),
            )
    """

    def test_run_memory_retrieval_fallback_forwards_flag(self, monkeypatch):
        """When _run_memory_retrieval is called with query_vector=None,
        the no_embedding_cache_shortcut flag from params reaches coalesced_query_embedding.
        """
        import code_indexer.server.mcp.handlers.search as search_handler
        import code_indexer.server.services.governed_call as governed_mod

        coalesce_calls: list = []

        def _spy_coalesce(provider, text, *, no_embedding_cache_shortcut=False, **kw):
            coalesce_calls.append(no_embedding_cache_shortcut)
            # Return a real-looking vector so the pipeline doesn't short-circuit.
            return [0.1] * 8

        monkeypatch.setattr(governed_mod, "coalesced_query_embedding", _spy_coalesce)

        # Stub VoyageAIClient so no network call is made.
        import code_indexer.services.voyage_ai as voyage_mod

        class _FakeVoyageClient:
            def __init__(self, *a, **kw):
                pass

            def get_provider_name(self):
                return "voyage-ai"

            def get_current_model(self):
                return "voyage-code-3"

            def get_model_info(self):
                return {"dimensions": 1024}

        monkeypatch.setattr(voyage_mod, "VoyageAIClient", _FakeVoyageClient)

        # Stub _get_http_client_factory — reads app.state, not available in unit tests.
        import code_indexer.server.services.search_service as ss_mod

        monkeypatch.setattr(
            ss_mod, "_get_http_client_factory", lambda: None, raising=False
        )

        # Build the config stub that _run_memory_retrieval reads.
        class _FakeMemCfg:
            memory_retrieval_enabled = True
            memory_voyage_min_score = 0.7
            memory_cohere_min_score = 0.7
            memory_retrieval_k_multiplier = 2
            memory_retrieval_max_body_chars = 5000

        class _FakeCfg:
            memory_retrieval_config = _FakeMemCfg()

        class _FakeConfigService:
            def get_config(self):
                return _FakeCfg()

        params = {
            "query_text": "find auth",
            "search_mode": "semantic",
            "no_embedding_cache_shortcut": True,
            "limit": 5,
        }

        class _FakeUser:
            username = "alice"

        # Call the fallback path directly: query_vector=None forces _compute_memory_query_vector.
        # _get_golden_repos_dir() fires after the vector is computed — RuntimeError expected.
        try:
            search_handler._run_memory_retrieval(
                params=params,
                user=_FakeUser(),
                config_service=_FakeConfigService(),
                reranker_status="disabled",
                query_vector=None,
            )
        except RuntimeError:
            pass  # golden_repos_dir not set in unit-test env — expected infra gap

        assert coalesce_calls, (
            "coalesced_query_embedding was never called via the _run_memory_retrieval "
            "fallback path. Ensure query_vector=None triggers _compute_memory_query_vector."
        )
        assert coalesce_calls[0] is True, (
            f"no_embedding_cache_shortcut=True must be forwarded through "
            f"_run_memory_retrieval -> _compute_memory_query_vector -> coalesced_query_embedding, "
            f"but captured: {coalesce_calls}"
        )


# ---------------------------------------------------------------------------
# ENTRY-POINT VALUE-FLOW TESTS (Story #1108 S4 gap fix)
# These tests drive the REAL entry points (_execute_temporal_query and
# _search_temporal_sync) with a spy on execute_temporal_query_with_fusion.
# The prior TestValueFlowTemporalDispatch tests drove leaf functions directly
# and missed the entry-vs-leaf drop.
# ---------------------------------------------------------------------------


class TestTemporalEntryPointValueFlow:
    """Prove that no_embedding_cache_shortcut=True reaches execute_temporal_query_with_fusion
    when called from the REAL entry points, not just the leaf dispatch functions."""

    def _make_fusion_results(self):
        """Return a minimal TemporalSearchResults stub."""
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchResults,
        )

        return TemporalSearchResults(
            results=[],
            query="find auth",
            filter_type="none",
            filter_value=None,
        )

    def test_execute_temporal_query_forwards_flag(self, monkeypatch, tmp_path):
        """_execute_temporal_query (SemanticQueryManager) must pass
        no_embedding_cache_shortcut=True to execute_temporal_query_with_fusion
        when a temporal param (e.g. time_range_all=True) is present.

        Entry point: _execute_temporal_query
        Spy on: execute_temporal_query_with_fusion (the immediate callee)
        """
        import code_indexer.server.query.semantic_query_manager as sqm_mod
        import code_indexer.services.temporal.temporal_fusion_dispatch as dispatch_mod

        captured: list = []
        fake_results = self._make_fusion_results()

        def _spy_fusion(*, no_embedding_cache_shortcut=False, **kw):
            captured.append(no_embedding_cache_shortcut)
            return fake_results

        # Patch where _execute_temporal_query imports it (inside the function body)
        monkeypatch.setattr(
            dispatch_mod, "execute_temporal_query_with_fusion", _spy_fusion
        )

        # We also need to patch the local import inside the function; the function does:
        #   from ...services.temporal.temporal_fusion_dispatch import execute_temporal_query_with_fusion
        # So we need to patch the module-level name there too.

        # Stub ConfigManager and BackendFactory so _execute_temporal_query doesn't
        # hit the filesystem
        import code_indexer.proxy.config_manager as config_mod
        import code_indexer.backends.backend_factory as backend_mod

        class _FakeConfig:
            voyage_ai = type("V", (), {"api_key": "k", "model": "voyage-code-3"})()
            cohere = None

        class _FakeCM:
            @classmethod
            def create_with_backtrack(cls, p):
                return cls()

            def get_config(self):
                return _FakeConfig()

        monkeypatch.setattr(config_mod, "ConfigManager", _FakeCM)

        class _FakeVS:
            project_root = str(tmp_path)

        class _FakeBackend:
            def get_vector_store_client(self):
                return _FakeVS()

        class _FakeFactory:
            @staticmethod
            def create(config, project_root, hnsw_cache=None):
                return _FakeBackend()

        monkeypatch.setattr(backend_mod, "BackendFactory", _FakeFactory)

        # Stub _server_hnsw_cache in app module
        import code_indexer.server.app as app_mod

        monkeypatch.setattr(app_mod, "_server_hnsw_cache", None, raising=False)

        # Build a SemanticQueryManager with minimal stubs
        manager = sqm_mod.SemanticQueryManager.__new__(sqm_mod.SemanticQueryManager)
        manager.max_results_per_query = 50
        manager.logger = logging.getLogger("test.sqm")

        # Call the entry point directly with time_range_all=True (triggers temporal path)
        # and no_embedding_cache_shortcut=True
        manager._execute_temporal_query(
            repo_path=tmp_path,
            repository_alias="myrepo",
            query_text="find auth",
            limit=5,
            min_score=None,
            time_range=None,
            time_range_all=True,
            no_embedding_cache_shortcut=True,
        )

        assert captured, (
            "execute_temporal_query_with_fusion was never called from _execute_temporal_query"
        )
        assert captured[0] is True, (
            f"no_embedding_cache_shortcut=True must reach execute_temporal_query_with_fusion "
            f"via _execute_temporal_query, but captured: {captured}"
        )

    def test_search_temporal_sync_forwards_flag(self, monkeypatch, tmp_path):
        """MultiSearchService._search_temporal_sync must pass
        no_embedding_cache_shortcut=True from request to execute_temporal_query_with_fusion.

        Entry point: _search_temporal_sync
        Spy on: execute_temporal_query_with_fusion
        """
        import code_indexer.server.multi.multi_search_service as mss_mod
        import code_indexer.services.temporal.temporal_fusion_dispatch as dispatch_mod

        captured: list = []
        fake_results = self._make_fusion_results()

        def _spy_fusion(*, no_embedding_cache_shortcut=False, **kw):
            captured.append(no_embedding_cache_shortcut)
            return fake_results

        monkeypatch.setattr(
            dispatch_mod, "execute_temporal_query_with_fusion", _spy_fusion
        )

        # Stub ConfigManager and FilesystemVectorStore
        import code_indexer.config as config_mod2
        import code_indexer.storage.filesystem_vector_store as fvs_mod

        class _FakeConfig:
            pass

        class _FakeCM:
            @classmethod
            def create_with_backtrack(cls, p):
                return cls()

            def get_config(self):
                return _FakeConfig()

        monkeypatch.setattr(config_mod2, "ConfigManager", _FakeCM)

        class _FakeVS:
            project_root = str(tmp_path)

        monkeypatch.setattr(fvs_mod, "FilesystemVectorStore", lambda **kw: _FakeVS())

        # Build a MultiSearchService with minimal stubs
        service = mss_mod.MultiSearchService.__new__(mss_mod.MultiSearchService)

        class _FakeMSSConfig:
            max_results_per_repo = 50

        service.config = _FakeMSSConfig()

        def _fake_get_repo_path(repo_id):
            return str(tmp_path)

        service._get_repository_path = _fake_get_repo_path

        # Build a MultiSearchRequest with no_embedding_cache_shortcut=True
        from code_indexer.server.multi.models import MultiSearchRequest

        request = MultiSearchRequest(
            repositories=["myrepo"],
            query="find auth",
            search_type="temporal",
            limit=5,
            no_embedding_cache_shortcut=True,
        )

        service._search_temporal_sync("myrepo", request)

        assert captured, (
            "execute_temporal_query_with_fusion was never called from _search_temporal_sync"
        )
        assert captured[0] is True, (
            f"no_embedding_cache_shortcut=True must reach execute_temporal_query_with_fusion "
            f"via _search_temporal_sync, but captured: {captured}"
        )


class TestValueFlowTemporalDispatch:
    """Value-flow tests: temporal dispatch layer must forward no_embedding_cache_shortcut
    into query_temporal() for both single-provider and multi-provider paths.

    Tests DEFECT 2: _query_single_provider and query_provider closure inside
    _query_multi_provider_fusion must accept and pass the flag down to query_temporal().
    """

    def _make_fake_results(self):
        """Return a minimal TemporalSearchResults stub."""
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchResults,
        )

        return TemporalSearchResults(
            results=[],
            query="find auth",
            filter_type="none",
            filter_value=None,
        )

    def test_single_provider_forwards_flag(self, monkeypatch):
        """_query_single_provider must pass no_embedding_cache_shortcut=True to query_temporal."""
        import code_indexer.services.temporal.temporal_fusion_dispatch as dispatch_mod
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchService,
        )

        captured: list = []
        fake_results = self._make_fake_results()

        def _spy_query_temporal(self_, *, no_embedding_cache_shortcut=False, **kw):
            captured.append(no_embedding_cache_shortcut)
            return fake_results

        monkeypatch.setattr(
            TemporalSearchService, "query_temporal", _spy_query_temporal
        )

        # Stub infra helpers — external deps (filesystem + provider instantiation).
        class _FakeProvider:
            pass

        class _FakeConfigManager:
            pass

        monkeypatch.setattr(
            dispatch_mod,
            "_create_embedding_provider_for_collection",
            lambda config, coll_name: _FakeProvider(),
        )
        monkeypatch.setattr(
            dispatch_mod,
            "_make_config_manager",
            lambda config: _FakeConfigManager(),
        )

        class _FakeVectorStore:
            project_root = "/fake/root"

        dispatch_mod._query_single_provider(
            config=object(),
            vector_store=_FakeVectorStore(),
            coll_name="temporal-voyage-code-3",
            query_text="find auth",
            limit=5,
            time_range=None,
            file_path_filter=None,
            no_embedding_cache_shortcut=True,
        )

        assert captured, "_spy_query_temporal was never called."
        assert captured[0] is True, (
            f"no_embedding_cache_shortcut=True must reach query_temporal via "
            f"_query_single_provider, but captured: {captured}"
        )

    def test_multi_provider_forwards_flag(self, monkeypatch):
        """query_provider closure in _query_multi_provider_fusion must pass
        no_embedding_cache_shortcut=True to query_temporal.
        """
        import code_indexer.services.temporal.temporal_fusion_dispatch as dispatch_mod
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchService,
        )

        captured: list = []
        fake_results = self._make_fake_results()

        def _spy_query_temporal(self_, *, no_embedding_cache_shortcut=False, **kw):
            captured.append(no_embedding_cache_shortcut)
            return fake_results

        monkeypatch.setattr(
            TemporalSearchService, "query_temporal", _spy_query_temporal
        )

        class _FakeProvider:
            pass

        class _FakeConfigManager:
            pass

        monkeypatch.setattr(
            dispatch_mod,
            "_create_embedding_provider_for_collection",
            lambda config, coll_name: _FakeProvider(),
        )
        monkeypatch.setattr(
            dispatch_mod,
            "_make_config_manager",
            lambda config: _FakeConfigManager(),
        )

        class _FakeVectorStore:
            project_root = "/fake/root"

        # Two collections to trigger the multi-provider path.
        collections = [
            ("temporal-voyage-code-3", "/fake/path/voyage"),
            ("temporal-cohere-embed-v4.0", "/fake/path/cohere"),
        ]

        dispatch_mod._query_multi_provider_fusion(
            config=object(),
            vector_store=_FakeVectorStore(),
            collections=collections,
            query_text="find auth",
            limit=5,
            time_range=None,
            file_path_filter=None,
            no_embedding_cache_shortcut=True,
        )

        assert captured, "_spy_query_temporal was never called in multi-provider path."
        assert all(v is True for v in captured), (
            f"no_embedding_cache_shortcut=True must reach query_temporal for ALL providers "
            f"in _query_multi_provider_fusion, but captured: {captured}"
        )


# ---------------------------------------------------------------------------
# Story #1108 S4 gap fix — SemanticQueryRequest field + /api/query threading
# ---------------------------------------------------------------------------


class TestSemanticQueryRequestField:
    """SemanticQueryRequest (models/query.py) must expose
    no_embedding_cache_shortcut: bool = False so the /api/query client
    can set the flag (Pydantic silently drops unknown extra keys).
    """

    def test_default_is_false(self):
        from code_indexer.server.models.query import SemanticQueryRequest

        req = SemanticQueryRequest(query_text="find auth")
        assert req.no_embedding_cache_shortcut is False

    def test_set_true(self):
        from code_indexer.server.models.query import SemanticQueryRequest

        req = SemanticQueryRequest(
            query_text="find auth", no_embedding_cache_shortcut=True
        )
        assert req.no_embedding_cache_shortcut is True

    def test_parses_from_json_true(self):
        from code_indexer.server.models.query import SemanticQueryRequest

        req = SemanticQueryRequest.model_validate(
            {"query_text": "find auth", "no_embedding_cache_shortcut": True}
        )
        assert req.no_embedding_cache_shortcut is True

    def test_absent_from_json_defaults_false(self):
        from code_indexer.server.models.query import SemanticQueryRequest

        req = SemanticQueryRequest.model_validate({"query_text": "find auth"})
        assert req.no_embedding_cache_shortcut is False


class TestValueFlowApiQueryRoute:
    """The /api/query route handler (routers/inline_query.py) must thread
    no_embedding_cache_shortcut from SemanticQueryRequest into
    query_user_repositories on BOTH code paths:
      - the default semantic mode path (bottom of handler)
      - the hybrid/semantic branch of the fts+hybrid mode (lines 300-326)

    Strategy: monkeypatch semantic_query_manager.query_user_repositories on
    the module-local reference that the route closure captures, spy on the kwarg.
    """

    def _make_spy_manager(self, captured: list):
        """Return a fake SemanticQueryManager whose query_user_repositories
        records the no_embedding_cache_shortcut kwarg value it receives."""
        from unittest.mock import MagicMock

        mgr = MagicMock()
        mgr.query_user_repositories.return_value = {
            "results": [],
            "total_results": 0,
            "query_metadata": {
                "query_text": "find auth",
                "execution_time_ms": 1,
                "repositories_searched": 0,
                "timeout_occurred": False,
            },
            "warning": None,
        }

        def _spy(**kw):
            captured.append(kw.get("no_embedding_cache_shortcut"))
            return mgr.query_user_repositories.return_value

        mgr.query_user_repositories.side_effect = _spy
        return mgr

    def _make_app(self, spy_manager):
        """Build a minimal FastAPI test app with the /api/query route registered."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from unittest.mock import MagicMock
        from code_indexer.server.routers.inline_query import register_query_routes

        test_app = FastAPI()

        # Minimal activated_repo_manager stub
        arm = MagicMock()
        arm.list_activated_repositories.return_value = []

        register_query_routes(
            test_app,
            semantic_query_manager=spy_manager,
            activated_repo_manager=arm,
        )

        # Override auth dependency so tests don't need a real token
        from code_indexer.server.auth import dependencies

        class _FakeUser:
            username = "alice"

        test_app.dependency_overrides[dependencies.get_current_user] = (
            lambda: _FakeUser()
        )
        return TestClient(test_app, raise_server_exceptions=True)

    def test_semantic_mode_threads_flag_true(self):
        """Default semantic mode: no_embedding_cache_shortcut=True must reach
        query_user_repositories."""
        captured: list = []
        spy_mgr = self._make_spy_manager(captured)
        client = self._make_app(spy_mgr)

        resp = client.post(
            "/api/query",
            json={
                "query_text": "find auth",
                "search_mode": "semantic",
                "no_embedding_cache_shortcut": True,
            },
        )
        assert resp.status_code in (200, 202), resp.text
        assert captured, "query_user_repositories was never called"
        assert captured[0] is True, (
            f"no_embedding_cache_shortcut=True must reach query_user_repositories "
            f"via /api/query semantic path, but captured: {captured}"
        )

    def test_semantic_mode_default_false_propagates(self):
        """When no_embedding_cache_shortcut is absent, False reaches the manager."""
        captured: list = []
        spy_mgr = self._make_spy_manager(captured)
        client = self._make_app(spy_mgr)

        resp = client.post("/api/query", json={"query_text": "find auth"})
        assert resp.status_code in (200, 202), resp.text
        assert captured, "query_user_repositories was never called"
        assert captured[0] is False, (
            f"no_embedding_cache_shortcut must default to False, but captured: {captured}"
        )
