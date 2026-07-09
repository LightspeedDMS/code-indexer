"""Regression tests for Story #1104 — Fix Cohere embedding_purpose drop.

Root cause: Cohere queries were embedded as search_document on every server
query path because:
  1. search_service.py ~494 passed embedding_purpose=None.
  2. temporal_search_service.py ~438 passed embedding_purpose=None.
  3. governed_call.coalesced_query_embedding line 166 called coalescer.submit(text)
     with no embedding_purpose.
  4. EmbeddingCoalescer.submit() had no embedding_purpose param.
  5. EmbeddingCoalescer._dispatch.do_call() called
     get_embeddings_batch(texts, retry=False) with no embedding_purpose=,
     defaulting to "document" -> Cohere maps to search_document.

Fix: thread embedding_purpose through submit() -> do_call() ->
get_embeddings_batch(..., embedding_purpose=...) and set "query" at all
server query call sites.

Tests use a scripted fake provider that records embedding_purpose args — NOT
mocking the purpose-mapping logic under test.  All tests are RED until the fix
is applied.
"""

import ast
import inspect
import threading
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.server.services.coalescer_registry import (
    CoalescerRegistry,
    clear_coalescer_registry,
    set_coalescer_registry,
)
from code_indexer.server.services.embedding_coalescer import EmbeddingCoalescer
from code_indexer.server.services.provider_concurrency_governor import (
    ProviderConcurrencyGovernor,
)


# ---------------------------------------------------------------------------
# Shared fixtures & fake providers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons():
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()
    clear_coalescer_registry()
    yield
    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()
    clear_coalescer_registry()


class RecordingCohereProvider:
    """Cohere-shaped fake that records the embedding_purpose passed to get_embeddings_batch.

    Exposes _count_tokens (not _count_tokens_accurately) so _resolve_token_counter
    picks the Cohere path.  _count_tokens_accurately is None so resolver falls
    through to _count_tokens.  Also exposes _get_texts_per_request and
    _get_model_token_limit so _ProviderConstraints can resolve limits.
    """

    # Null out voyage-only counter so the resolver correctly picks _count_tokens.
    _count_tokens_accurately: None = None  # type: ignore[assignment]

    def __init__(self, token_limit: int = 120_000, texts_per_request: int = 96) -> None:
        self._token_limit = token_limit
        self._texts_per_request_val = texts_per_request
        # Records: list of embedding_purpose kwargs passed to get_embeddings_batch.
        self.recorded_purposes: List[Optional[str]] = []
        self._lock = threading.Lock()

    def _count_tokens(self, text: str) -> int:
        return 1  # trivial; each text = 1 token

    def _get_model_token_limit(self) -> int:
        return self._token_limit

    def _get_texts_per_request(self) -> int:
        return self._texts_per_request_val

    def get_provider_name(self) -> str:
        """Real CohereEmbeddingProvider implements this; the coalescer's
        _dispatch() reads it to attribute emitted events (Story #1293)."""
        return "cohere"

    def get_embedding(
        self, text: str, *, embedding_purpose: str = "document"
    ) -> List[float]:
        return self.get_embeddings_batch(
            [text], retry=False, embedding_purpose=embedding_purpose
        )[0]

    def get_embeddings_batch(
        self,
        texts: List[str],
        *,
        embedding_purpose: str = "document",
        retry: bool = True,
    ) -> List[List[float]]:
        with self._lock:
            self.recorded_purposes.append(embedding_purpose)
        return [[float(i), 0.0] for i in range(len(texts))]


class RecordingVoyageProvider:
    """Voyage-shaped fake: exposes _count_tokens_accurately, NO _get_texts_per_request.

    Records the embedding_purpose to assert Voyage is unaffected.
    """

    def __init__(self, token_limit: int = 120_000) -> None:
        self._token_limit = token_limit
        self.recorded_purposes: List[Optional[str]] = []
        self._lock = threading.Lock()

    def _count_tokens_accurately(self, text: str) -> int:
        return 1

    def _get_model_token_limit(self) -> int:
        return self._token_limit

    def get_provider_name(self) -> str:
        """Real VoyageAIClient implements this; the coalescer's _dispatch()
        reads it to attribute emitted events (Story #1293)."""
        return "voyage-ai"

    def get_embedding(
        self, text: str, *, embedding_purpose: str = "document"
    ) -> List[float]:
        return self.get_embeddings_batch(
            [text], retry=False, embedding_purpose=embedding_purpose
        )[0]

    def get_embeddings_batch(
        self,
        texts: List[str],
        *,
        embedding_purpose: str = "document",
        retry: bool = True,
    ) -> List[List[float]]:
        with self._lock:
            self.recorded_purposes.append(embedding_purpose)
        return [[float(i), 0.0] for i in range(len(texts))]


# ---------------------------------------------------------------------------
# AC1 / AC3: EmbeddingCoalescer threads embedding_purpose through to
# get_embeddings_batch (the coalesced path)
# ---------------------------------------------------------------------------


class TestCoalescerThreadsPurpose:
    """AC1 + AC3: embedding_purpose threaded submit() -> do_call() -> get_embeddings_batch."""

    def test_submit_with_query_purpose_reaches_get_embeddings_batch(self):
        """submit(text, embedding_purpose='query') must call get_embeddings_batch(
        ..., embedding_purpose='query') — NOT the default 'document'.

        This test FAILS before the fix because submit() has no embedding_purpose
        param and do_call() calls get_embeddings_batch(texts, retry=False) with
        no purpose kwarg.
        """
        provider = RecordingCohereProvider()
        coalescer = EmbeddingCoalescer("cohere:embed", provider)

        coalescer.submit("hello world", embedding_purpose="query")

        assert len(provider.recorded_purposes) == 1
        assert provider.recorded_purposes[0] == "query", (
            f"Expected 'query' but got {provider.recorded_purposes[0]!r}; "
            "do_call() is calling get_embeddings_batch without embedding_purpose="
        )

    def test_submit_default_is_query(self):
        """submit(text) with no explicit purpose should default to 'query'."""
        provider = RecordingCohereProvider()
        coalescer = EmbeddingCoalescer("cohere:embed", provider)

        coalescer.submit("hello")

        assert provider.recorded_purposes[0] == "query", (
            f"Default purpose should be 'query', got {provider.recorded_purposes[0]!r}"
        )

    def test_submit_document_purpose_still_works(self):
        """submit(text, embedding_purpose='document') propagates 'document' (indexing path)."""
        provider = RecordingCohereProvider()
        coalescer = EmbeddingCoalescer("cohere:embed", provider)

        coalescer.submit("some doc", embedding_purpose="document")

        assert provider.recorded_purposes[0] == "document"

    def test_voyage_provider_purpose_propagated_unchanged(self):
        """Voyage fake also records purpose — confirms Voyage submit path is unaffected."""
        provider = RecordingVoyageProvider()
        coalescer = EmbeddingCoalescer("voyage:embed", provider)

        coalescer.submit("hello", embedding_purpose="query")

        assert provider.recorded_purposes[0] == "query"

    def test_each_sequential_submit_carries_purpose(self):
        """Each sequential submit call carries its own purpose to get_embeddings_batch.

        Sequential submit() calls each block until dispatched, so 3 calls =
        3 batches (without governor saturation).  Each batch must record the
        correct purpose — proving purpose is threaded per submission.
        """
        provider = RecordingCohereProvider(texts_per_request=10)
        coalescer = EmbeddingCoalescer("cohere:embed", provider)

        coalescer.submit("text-0", embedding_purpose="query")
        coalescer.submit("text-1", embedding_purpose="query")
        coalescer.submit("text-2", embedding_purpose="document")

        # 3 sequential submits = 3 separate batches; each with its own purpose.
        assert len(provider.recorded_purposes) == 3
        assert provider.recorded_purposes[0] == "query"
        assert provider.recorded_purposes[1] == "query"
        assert provider.recorded_purposes[2] == "document"


# ---------------------------------------------------------------------------
# AC3 + AC4: coalesced_query_embedding passes embedding_purpose to coalescer.submit
# ---------------------------------------------------------------------------


class _FakeConfig:
    def __init__(self, coalesce_enabled: bool = True) -> None:
        self.coalesce_enabled = coalesce_enabled


class _FakeConfigService:
    def __init__(self, cfg: _FakeConfig) -> None:
        self._cfg = cfg

    def get_config(self) -> _FakeConfig:
        return self._cfg


class RecordingCoalescer:
    """Coalescer spy that records (text, embedding_purpose) submissions."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def submit(
        self,
        text: str,
        embedding_purpose: str = "query",
        *,
        no_embedding_cache_shortcut: bool = False,
        audit_ctx: Optional[Any] = None,
    ) -> List[float]:
        self.calls.append({"text": text, "embedding_purpose": embedding_purpose})
        return [1.0, 2.0]


def _patch_config(
    monkeypatch: Any, governed_call_module: Any, cfg: _FakeConfig
) -> None:
    monkeypatch.setattr(
        governed_call_module,
        "get_config_service",
        lambda: _FakeConfigService(cfg),
        raising=False,
    )


class TestCoalescedQueryEmbeddingPassesPurpose:
    """AC3/AC4: coalesced_query_embedding must pass embedding_purpose to coalescer.submit()."""

    def test_coalesced_path_passes_query_purpose_to_submit(self, monkeypatch):
        """When coalescer.submit() is called, embedding_purpose='query' must be forwarded.

        This test FAILS before the fix because coalesced_query_embedding calls
        coalescer.submit(text) without the embedding_purpose kwarg.
        """
        from code_indexer.server.services import governed_call

        _patch_config(monkeypatch, governed_call, _FakeConfig(coalesce_enabled=True))

        coalescer = RecordingCoalescer()
        set_coalescer_registry(CoalescerRegistry.__new__(CoalescerRegistry))
        reg = governed_call.get_coalescer_registry()
        monkeypatch.setattr(
            reg,
            "get_or_create",
            lambda lane, digest, provider: coalescer,
            raising=False,
        )

        class _FakeProviderNotCohere:
            pass

        governed_call.coalesced_query_embedding(
            _FakeProviderNotCohere(), "test query", embedding_purpose="query"
        )

        assert len(coalescer.calls) == 1
        assert coalescer.calls[0]["embedding_purpose"] == "query", (
            f"coalesced_query_embedding did not forward embedding_purpose to submit(); "
            f"got {coalescer.calls[0]!r}"
        )

    def test_coalesced_path_passes_explicit_document_purpose(self, monkeypatch):
        """embedding_purpose='document' is forwarded faithfully to submit()."""
        from code_indexer.server.services import governed_call

        _patch_config(monkeypatch, governed_call, _FakeConfig(coalesce_enabled=True))

        coalescer = RecordingCoalescer()
        set_coalescer_registry(CoalescerRegistry.__new__(CoalescerRegistry))
        reg = governed_call.get_coalescer_registry()
        monkeypatch.setattr(
            reg,
            "get_or_create",
            lambda lane, digest, provider: coalescer,
            raising=False,
        )

        class _FakeProvider:
            pass

        governed_call.coalesced_query_embedding(
            _FakeProvider(), "doc text", embedding_purpose="document"
        )

        assert coalescer.calls[0]["embedding_purpose"] == "document"


# ---------------------------------------------------------------------------
# AC4: ALL server query call sites use embedding_purpose="query" (not None)
# ---------------------------------------------------------------------------


def _ast_coalesced_calls(source: str) -> List[ast.Call]:
    """Return all AST Call nodes for coalesced_query_embedding in source."""
    tree = ast.parse(source)
    return [
        node
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "coalesced_query_embedding"
        )
    ]


class TestQueryCallSitesPurpose:
    """AC4: Prove all 3 server query embedding call sites pass embedding_purpose='query'."""

    def test_search_service_passes_query_purpose(self):
        """search_service.py coalesced_query_embedding call must NOT pass embedding_purpose=None.

        This test FAILS before the fix because search_service.py:~494 passes
        embedding_purpose=None.
        """
        import code_indexer.server.services.search_service as ss_module

        source = inspect.getsource(ss_module)
        calls = _ast_coalesced_calls(source)

        assert calls, (
            "No call to coalesced_query_embedding found in search_service.py — "
            "expected at least one call site"
        )

        found_purpose_kw = False
        for call in calls:
            for kw in call.keywords:
                if kw.arg == "embedding_purpose":
                    found_purpose_kw = True
                    assert not (
                        isinstance(kw.value, ast.Constant) and kw.value.value is None
                    ), (
                        "search_service.py passes embedding_purpose=None to "
                        "coalesced_query_embedding — must be 'query'"
                    )
                    if isinstance(kw.value, ast.Constant):
                        assert kw.value.value == "query", (
                            f"search_service.py passes embedding_purpose="
                            f"{kw.value.value!r}, expected 'query'"
                        )

        assert found_purpose_kw, (
            "search_service.py does not pass embedding_purpose= kwarg to "
            "coalesced_query_embedding — must explicitly pass 'query' (not rely on default)"
        )

    def test_temporal_search_service_passes_query_purpose(self):
        """temporal_search_service.py passes embedding_purpose='query', not None."""
        import code_indexer.services.temporal.temporal_search_service as tss_module

        source = inspect.getsource(tss_module)
        calls = _ast_coalesced_calls(source)

        assert calls, (
            "No call to coalesced_query_embedding found in temporal_search_service.py"
        )

        found_purpose_kw = False
        for call in calls:
            for kw in call.keywords:
                if kw.arg == "embedding_purpose":
                    found_purpose_kw = True
                    assert not (
                        isinstance(kw.value, ast.Constant) and kw.value.value is None
                    ), (
                        "temporal_search_service.py passes embedding_purpose=None — "
                        "must be 'query'"
                    )
                    if isinstance(kw.value, ast.Constant):
                        assert kw.value.value == "query", (
                            f"temporal_search_service.py passes embedding_purpose="
                            f"{kw.value.value!r}, expected 'query'"
                        )

        assert found_purpose_kw, (
            "temporal_search_service.py does not pass embedding_purpose= kwarg to "
            "coalesced_query_embedding — must explicitly pass 'query'"
        )

    def test_mcp_handlers_search_does_not_pass_none_purpose(self):
        """mcp/handlers/search.py _compute_memory_query_vector must NOT pass None.

        The MCP site may use the default (no kwarg) or pass 'query' explicitly;
        either is acceptable.  Only None is forbidden.
        """
        import code_indexer.server.mcp.handlers.search as mcp_search_module

        source = inspect.getsource(mcp_search_module)
        calls = _ast_coalesced_calls(source)

        assert calls, (
            "No call to coalesced_query_embedding found in mcp/handlers/search.py"
        )

        for call in calls:
            for kw in call.keywords:
                if kw.arg == "embedding_purpose":
                    assert not (
                        isinstance(kw.value, ast.Constant) and kw.value.value is None
                    ), (
                        "mcp/handlers/search.py passes embedding_purpose=None to "
                        "coalesced_query_embedding — must not be None"
                    )


# ---------------------------------------------------------------------------
# AC3: Cohere _map_embedding_purpose integration — real mapping, no mock
# ---------------------------------------------------------------------------


class TestCoherePurposeMapping:
    """AC3: Real Cohere provider mapping — 'query' -> 'search_query'.

    Uses real CohereEmbeddingProvider._map_embedding_purpose (the logic under
    test), not a mock.  Proves the entire chain from purpose string to Cohere
    input_type is correct.
    """

    def test_query_maps_to_search_query(self):
        """'query' embedding_purpose maps to Cohere input_type='search_query'."""
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

        # Use _map_embedding_purpose directly — it's the boundary we must reach.
        # Access via class (no self-dependency; pure function of the arg).
        result = CohereEmbeddingProvider._map_embedding_purpose(
            None,  # type: ignore[arg-type]
            "query",
        )
        assert result == "search_query", (
            f"_map_embedding_purpose('query') returned {result!r}, expected 'search_query'"
        )

    def test_document_maps_to_search_document(self):
        """'document' embedding_purpose maps to Cohere input_type='search_document'."""
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

        result = CohereEmbeddingProvider._map_embedding_purpose(None, "document")  # type: ignore[arg-type]
        assert result == "search_document"

    def test_none_purpose_maps_to_search_document(self):
        """None purpose falls through to search_document — confirms the old bug."""
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

        result = CohereEmbeddingProvider._map_embedding_purpose(None, None)  # type: ignore[arg-type]
        assert result == "search_document", (
            "None purpose should still map to search_document "
            "(confirms fix targets the call sites, not the mapping)"
        )


# ---------------------------------------------------------------------------
# AC3: Voyage unaffected — no input_type sent regardless of purpose
# ---------------------------------------------------------------------------


class TestVoyageUnaffected:
    """AC3: Voyage accepts embedding_purpose but never sends input_type."""

    def test_voyage_get_embeddings_batch_accepts_query_purpose(self):
        """VoyageAIClient.get_embeddings_batch accepts embedding_purpose='query'
        without error — Voyage ignores it (no input_type in the HTTP payload).

        We verify the method signature accepts the kwarg.
        """
        from code_indexer.services.voyage_ai import VoyageAIClient

        sig = inspect.signature(VoyageAIClient.get_embeddings_batch)
        assert "embedding_purpose" in sig.parameters, (
            "VoyageAIClient.get_embeddings_batch missing embedding_purpose parameter"
        )

    def test_recording_voyage_provider_records_query_purpose_from_coalescer(self):
        """When Voyage fake goes through the coalescer with purpose='query', it records 'query'."""
        provider = RecordingVoyageProvider()
        coalescer = EmbeddingCoalescer("voyage:embed", provider)

        coalescer.submit("some search query", embedding_purpose="query")

        assert provider.recorded_purposes == ["query"], (
            f"Voyage coalesced path did not forward purpose; got {provider.recorded_purposes!r}"
        )
