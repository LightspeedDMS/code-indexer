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
    cache.build_key_for_provider = lambda text, provider_name: build_key(text, 2)
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
        """Default False with no cache must call _compute_live (unchanged)."""
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: None)

        live_calls: list = []

        def _fake_live(provider, text, embedding_purpose=None, acquire_timeout=30.0):
            live_calls.append(text)
            return LIVE_VEC

        monkeypatch.setattr(governed_call, "_compute_live", _fake_live)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT
        )
        assert result == LIVE_VEC
        assert live_calls == [TEST_TEXT]

    def test_bypass_true_no_cache_still_computes_live(self, monkeypatch):
        """bypass=True with no cache still computes live (no cache to skip)."""
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: None)

        live_calls: list = []

        def _fake_live(provider, text, embedding_purpose=None, acquire_timeout=30.0):
            live_calls.append(text)
            return LIVE_VEC

        monkeypatch.setattr(governed_call, "_compute_live", _fake_live)

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

        def _fake(provider, text, embedding_purpose=None, acquire_timeout=30.0):
            live_calls.append(text)
            return LIVE_VEC

        monkeypatch.setattr(governed_call, "_compute_live", _fake)
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
