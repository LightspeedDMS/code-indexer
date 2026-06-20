"""Unit tests for search handler instrumentation — Issue #1159 spec item 8.

Tests exercise the real search_code() routing path with ONLY true external
boundaries mocked:
  - _utils.app_module.semantic_query_manager.query_user_repositories (DB/index)
  - _get_search_event_writer() (writer boundary)

Internal helpers (_compute_shared_query_vector, _apply_rerank_and_filter, etc.)
are NOT mocked — they are real code.  The external service calls they
transitively reach ARE gated by the same two mocks above.

Verifies:
  - SearchEventRecord enqueued on successful search
  - No record enqueued on failed search (spec H11)
  - query_text capped at 500 Unicode code points (spec A8)
  - SearchEventContext ContextVar set BEFORE sub-handler executes
  - ContextVar cleared AFTER search_code returns
  - voyage / cohere cache metadata fields propagated from EmbeddingCacheMetadata
    (spec B1-B5, B6-B10)
"""

import json
from typing import Any, Dict, cast
from unittest.mock import MagicMock, Mock
import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
from code_indexer.server.services.search_event_context import (
    SearchEventContext,
    get_search_event_ctx,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_mcp(result: Dict[str, Any]) -> Dict[str, Any]:
    """Parse inner JSON from MCP-wrapped response."""
    text = result["content"][0]["text"]
    return cast(Dict[str, Any], json.loads(text))


def _make_user(username: str = "alice") -> User:
    user = Mock(spec=User)
    user.username = username
    user.role = UserRole.NORMAL_USER
    return user


def _qm_result(n: int = 5) -> Dict[str, Any]:
    """Minimal result from query_user_repositories (no real index needed).

    Must be fully JSON-serializable: no MagicMocks anywhere.
    Includes all keys that _enrich_activated_results / _enrich_with_wiki_url
    may read so they don't fall back to MagicMock attribute access.
    """
    return {
        "results": [
            {
                "file_path": f"f{i}.py",
                "score": 0.9,
                "content": "",
                "preview": None,
                "cache_handle": None,
                "total_size": None,
                "source_repo": None,
                "repository_alias": None,
            }
            for i in range(n)
        ],
        "success": True,
    }


def _make_writer():
    records: list = []

    class _W:
        enqueued = records

        def enqueue(self, record):
            records.append(record)

    return _W()


def _params(
    query_text: str = "auth token expiry",
    repo_alias: str = "myrepo",
    search_mode: str = "semantic",
) -> Dict[str, Any]:
    return {
        "repository_alias": repo_alias,
        "query_text": query_text,
        "search_mode": search_mode,
    }


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def writer():
    return _make_writer()


@pytest.fixture(autouse=True)
def wire_app_module(monkeypatch):
    """Wire a mock app_module so search_code can route without a real server."""
    from code_indexer.server.mcp.handlers import _utils as utils_mod

    mock_am = MagicMock()
    mock_am.semantic_query_manager.query_user_repositories.return_value = _qm_result()
    mock_am.activated_repo_manager.user_has_activated_repo.return_value = True
    mock_am.activated_repo_manager.touch_last_accessed.return_value = None
    # _load_category_map: no category service -> returns empty dict
    mock_am.golden_repo_manager._repo_category_service = None
    # _get_access_filtering_service() reads app_module.app.state.access_filtering_service
    # Return None so filter_query_results is never called (avoids MagicMock in results)
    mock_am.app.state.access_filtering_service = None
    # _apply_payload_truncation reads app_module.app.state.payload_cache via getattr.
    # Setting to None makes it early-return and skip truncation, avoiding
    # MagicMock.truncate_result() calls that produce non-JSON-serializable values.
    mock_am.app.state.payload_cache = None
    monkeypatch.setattr(utils_mod, "app_module", mock_am)
    return mock_am


@pytest.fixture(autouse=True)
def wire_config_service(monkeypatch):
    """Minimal config: memory retrieval disabled, no reranking."""
    import code_indexer.server.mcp.handlers.search as sh

    cfg = MagicMock()
    cfg.get_config.return_value.memory_retrieval_config.memory_retrieval_enabled = False
    cfg.get_config.return_value.rerank_config = None
    cfg.get_config.return_value.node_id = "test-node"
    monkeypatch.setattr(sh, "get_config_service", lambda: cfg)
    return cfg


# ---------------------------------------------------------------------------
# Tests: enqueue contract
# ---------------------------------------------------------------------------


class TestEnqueueContract:
    def test_enqueues_record_on_success(self, writer, monkeypatch):
        """After a successful search, exactly one SearchEventRecord is enqueued."""
        import code_indexer.server.mcp.handlers.search as sh

        monkeypatch.setattr(
            sh, "_get_search_event_writer", lambda: writer, raising=False
        )

        raw = sh.search_code(_params(), _make_user("alice"))
        result = _parse_mcp(raw)

        assert result.get("success") is True
        assert len(writer.enqueued) == 1
        rec = writer.enqueued[0]
        assert rec.username == "alice"
        assert rec.repo_alias == "myrepo"
        assert rec.query_text == "auth token expiry"
        assert rec.result_count == 5
        assert rec.total_latency_ms >= 0
        assert rec.node_id != ""

    def test_no_enqueue_on_exception(self, writer, monkeypatch, wire_app_module):
        """Spec H11: exception in sub-handler -> no record enqueued."""
        import code_indexer.server.mcp.handlers.search as sh

        monkeypatch.setattr(
            sh, "_get_search_event_writer", lambda: writer, raising=False
        )
        wire_app_module.semantic_query_manager.query_user_repositories.side_effect = (
            RuntimeError("index gone")
        )

        raw = sh.search_code(_params(), _make_user("alice"))
        result = _parse_mcp(raw)

        assert result.get("success") is False
        assert len(writer.enqueued) == 0

    def test_query_text_capped_at_500(self, writer, monkeypatch):
        """Spec A8: query_text stored in record is at most 500 code points."""
        import code_indexer.server.mcp.handlers.search as sh

        monkeypatch.setattr(
            sh, "_get_search_event_writer", lambda: writer, raising=False
        )

        long_q = "é" * 700
        sh.search_code(_params(query_text=long_q), _make_user("alice"))

        assert len(writer.enqueued) == 1
        assert len(writer.enqueued[0].query_text) <= 500

    def test_result_count_zero(self, writer, monkeypatch, wire_app_module):
        """Spec A10: zero results recorded accurately."""
        import code_indexer.server.mcp.handlers.search as sh

        monkeypatch.setattr(
            sh, "_get_search_event_writer", lambda: writer, raising=False
        )
        wire_app_module.semantic_query_manager.query_user_repositories.return_value = (
            _qm_result(0)
        )

        sh.search_code(_params(), _make_user("alice"))

        assert len(writer.enqueued) == 1
        assert writer.enqueued[0].result_count == 0

    def test_no_crash_when_writer_none(self, monkeypatch):
        """search_code must not raise when writer is unavailable."""
        import code_indexer.server.mcp.handlers.search as sh

        monkeypatch.setattr(sh, "_get_search_event_writer", lambda: None, raising=False)

        raw = sh.search_code(_params(), _make_user("alice"))
        # Must return a dict (either MCP wrapper or error response) without raising
        assert isinstance(raw, dict)


# ---------------------------------------------------------------------------
# Tests: ContextVar lifecycle
# ---------------------------------------------------------------------------


class TestContextVarLifecycle:
    def test_context_set_before_sub_handler(self, writer, monkeypatch, wire_app_module):
        """SearchEventContext ContextVar is installed before query_user_repositories runs."""
        import code_indexer.server.mcp.handlers.search as sh

        monkeypatch.setattr(
            sh, "_get_search_event_writer", lambda: writer, raising=False
        )

        captured: list = []

        def capturing_qur(**kwargs):
            captured.append(get_search_event_ctx())
            return _qm_result(3)

        wire_app_module.semantic_query_manager.query_user_repositories.side_effect = (
            capturing_qur
        )

        sh.search_code(
            _params(query_text="find auth", repo_alias="repo1"), _make_user("alice")
        )

        assert len(captured) >= 1, "sub-handler was never called"
        ctx = captured[0]
        assert ctx is not None, "ContextVar must be set when sub-handler runs"
        assert isinstance(ctx, SearchEventContext)
        assert ctx.username == "alice"
        assert ctx.repo_alias == "repo1"
        assert ctx.query_text == "find auth"

    def test_context_cleared_after_search(self, writer, monkeypatch):
        """After search_code returns, ContextVar is reset to None."""
        import code_indexer.server.mcp.handlers.search as sh

        monkeypatch.setattr(
            sh, "_get_search_event_writer", lambda: writer, raising=False
        )

        sh.search_code(_params(), _make_user("alice"))

        assert get_search_event_ctx() is None


# ---------------------------------------------------------------------------
# Tests: embedding metadata fields in enqueued record
# ---------------------------------------------------------------------------


class TestEmbeddingMetadataInRecord:
    """Verify voyage/cohere cache metadata propagates from coalesced_query_embedding
    through SearchEventContext into the enqueued SearchEventRecord.

    These tests call _compute_shared_query_vector and _compute_memory_query_vector
    DIRECTLY (not through search_code) with a SearchEventContext installed and
    coalesced_query_embedding mocked at the module level. They verify that the
    REAL production ctx-write code in those functions populates the ctx correctly.

    The tests FAIL if the call-site code does not write to ctx — they do NOT
    manually set ctx fields.
    """

    def _with_ctx(self):
        """Install a fresh SearchEventContext and return (ctx, token)."""
        from code_indexer.server.services.search_event_context import (
            SearchEventContext,
            _search_event_ctx,
        )

        ctx = SearchEventContext(
            username="alice",
            repo_alias="myrepo",
            search_type="semantic",
            query_text="test query",
        )
        token = _search_event_ctx.set(ctx)
        return ctx, token

    def test_voyage_cache_hit_in_record(self, writer, monkeypatch, wire_app_module):
        """Spec B1: Voyage mode=on, key found -> voyage_cache_hit=True, latency=None in ctx.

        _compute_shared_query_vector calls coalesced_query_embedding (mocked to
        return HIT metadata). The production call-site code writes to ctx.
        This test verifies the ctx is populated — NOT the search_code record.
        """
        import code_indexer.server.mcp.handlers.search as sh
        from code_indexer.server.services.search_event_context import _search_event_ctx

        hit_meta = EmbeddingCacheMetadata(
            key_found=True, cache_mode="on", provider_latency_ms=None
        )
        fake_vec = [0.1, 0.2, 0.3]

        import code_indexer.server.services.governed_call as gc_mod

        monkeypatch.setattr(
            gc_mod,
            "coalesced_query_embedding",
            lambda *a, **kw: (fake_vec, hit_meta),
        )

        ctx, token = self._with_ctx()
        try:
            # Call the production function directly; it writes to ctx via _search_event_ctx.get()
            sh._compute_shared_query_vector("test query")
        finally:
            _search_event_ctx.reset(token)

        # The call-site code in _compute_shared_query_vector must have written to ctx
        assert ctx.voyage_cache_hit is True
        assert ctx.voyage_cache_mode == "on"
        assert ctx.voyage_latency_ms is None

    def test_voyage_cache_miss_in_record(self, writer, monkeypatch, wire_app_module):
        """Spec B2: Voyage mode=on, key NOT found -> voyage_cache_hit=False, latency set in ctx."""
        import code_indexer.server.mcp.handlers.search as sh
        from code_indexer.server.services.search_event_context import _search_event_ctx

        miss_meta = EmbeddingCacheMetadata(
            key_found=False, cache_mode="on", provider_latency_ms=45
        )
        fake_vec = [0.1, 0.2, 0.3]

        import code_indexer.server.services.governed_call as gc_mod

        monkeypatch.setattr(
            gc_mod,
            "coalesced_query_embedding",
            lambda *a, **kw: (fake_vec, miss_meta),
        )

        ctx, token = self._with_ctx()
        try:
            sh._compute_shared_query_vector("test query")
        finally:
            _search_event_ctx.reset(token)

        assert ctx.voyage_cache_hit is False
        assert ctx.voyage_cache_mode == "on"
        assert ctx.voyage_latency_ms == 45

    def test_memory_query_vector_writes_ctx(self, writer, monkeypatch, wire_app_module):
        """_compute_memory_query_vector also writes voyage metadata to ctx on success."""
        import code_indexer.server.mcp.handlers.search as sh
        from code_indexer.server.services.search_event_context import _search_event_ctx

        hit_meta = EmbeddingCacheMetadata(
            key_found=True, cache_mode="shadow", provider_latency_ms=12
        )
        fake_vec = [0.4, 0.5]

        import code_indexer.server.services.governed_call as gc_mod

        monkeypatch.setattr(
            gc_mod,
            "coalesced_query_embedding",
            lambda *a, **kw: (fake_vec, hit_meta),
        )

        ctx, token = self._with_ctx()
        try:
            sh._compute_memory_query_vector("test query")
        finally:
            _search_event_ctx.reset(token)

        assert ctx.voyage_cache_hit is True
        assert ctx.voyage_cache_mode == "shadow"
        assert ctx.voyage_latency_ms == 12

    def test_cohere_cache_hit_in_record(self, writer, monkeypatch, wire_app_module):
        """Spec B6: cache HIT -> metadata propagated to ctx (voyage fields, since VoyageAIClient is used)."""
        import code_indexer.server.mcp.handlers.search as sh
        from code_indexer.server.services.search_event_context import _search_event_ctx

        hit_meta = EmbeddingCacheMetadata(
            key_found=True, cache_mode="on", provider_latency_ms=None
        )
        fake_vec = [0.1, 0.2, 0.3]

        import code_indexer.server.services.governed_call as gc_mod

        monkeypatch.setattr(
            gc_mod,
            "coalesced_query_embedding",
            lambda *a, **kw: (fake_vec, hit_meta),
        )

        ctx, token = self._with_ctx()
        try:
            sh._compute_shared_query_vector("test query")
        finally:
            _search_event_ctx.reset(token)

        # _compute_shared_query_vector uses VoyageAIClient, so voyage_* fields are populated.
        assert ctx.voyage_cache_hit is True
        assert ctx.voyage_cache_mode == "on"

    def test_cohere_cache_miss_in_record(self, writer, monkeypatch, wire_app_module):
        """Spec B7: cache miss -> provider_latency_ms propagated to ctx."""
        import code_indexer.server.mcp.handlers.search as sh
        from code_indexer.server.services.search_event_context import _search_event_ctx

        miss_meta = EmbeddingCacheMetadata(
            key_found=False, cache_mode="on", provider_latency_ms=37
        )
        fake_vec = [0.1, 0.2, 0.3]

        import code_indexer.server.services.governed_call as gc_mod

        monkeypatch.setattr(
            gc_mod,
            "coalesced_query_embedding",
            lambda *a, **kw: (fake_vec, miss_meta),
        )

        ctx, token = self._with_ctx()
        try:
            sh._compute_shared_query_vector("test query")
        finally:
            _search_event_ctx.reset(token)

        assert ctx.voyage_cache_hit is False
        assert ctx.voyage_cache_mode == "on"
        assert ctx.voyage_latency_ms == 37

    def test_fsv_generate_embedding_writes_embed_meta_to_ctx(self, monkeypatch):
        """Root Cause 2: FSV generate_embedding() must write _embed_meta to _search_event_ctx.

        RED test: before the fix, FSV discards _embed_meta and ctx fields stay None.
        GREEN: after the fix, ctx voyage fields are populated from the real metadata.
        """
        import numpy as np
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
        from code_indexer.server.services.search_event_context import (
            SearchEventContext,
            _search_event_ctx,
        )
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        DIMS = 4
        FAKE_VEC = [0.1, 0.2, 0.3, 0.4]
        HIT_META = EmbeddingCacheMetadata(
            key_found=True, cache_mode="on", provider_latency_ms=None
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            base_path = Path(tmp_dir)
            store = FilesystemVectorStore(base_path=base_path, project_root=base_path)
            store.create_collection("test_coll", vector_size=DIMS)

            # Upsert one vector and build HNSW index
            vec = np.array(FAKE_VEC, dtype=np.float32)
            store.upsert_points(
                "test_coll",
                [
                    {
                        "id": "pt1",
                        "vector": vec.tolist(),
                        "payload": {"content": "hello", "file_path": "a.py"},
                    }
                ],
            )
            store.end_indexing("test_coll")  # builds HNSW index on disk

            # Mock provider (no real API call)
            mock_provider = MagicMock()
            mock_provider.get_provider_name.return_value = "voyage-ai"

            ctx = SearchEventContext(
                username="alice",
                repo_alias="repo1",
                search_type="semantic",
                query_text="test",
            )
            token = _search_event_ctx.set(ctx)
            try:
                import code_indexer.storage.filesystem_vector_store as fsv_mod

                with patch.object(
                    fsv_mod,
                    "coalesced_query_embedding",
                    return_value=(FAKE_VEC, HIT_META),
                ):
                    store.search(
                        query="test query",
                        embedding_provider=mock_provider,
                        collection_name="test_coll",
                        limit=5,
                    )
            finally:
                _search_event_ctx.reset(token)

        # After the fix: ctx fields must be populated from HIT_META
        assert ctx.voyage_cache_hit is True, (
            "FSV generate_embedding() must write key_found=True to voyage_cache_hit"
        )
        assert ctx.voyage_cache_mode == "on", (
            "FSV generate_embedding() must write cache_mode='on' to voyage_cache_mode"
        )
        assert ctx.voyage_latency_ms is None  # hit_meta has None latency

    def test_search_service_backend_path_writes_embed_meta_to_ctx(self):
        """Root Cause 4 / Story #1159: Backend path in search_service.py must write
        _embed_meta to _search_event_ctx after coalesced_query_embedding() returns."""
        from unittest.mock import MagicMock, patch

        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
        from code_indexer.server.services.search_event_context import (
            SearchEventContext,
            _search_event_ctx,
        )
        from code_indexer.server.services.search_service import SemanticSearchService

        HIT_META = EmbeddingCacheMetadata(
            key_found=True, cache_mode="on", provider_latency_ms=None
        )
        FAKE_VEC = [0.1, 0.2, 0.3]

        mock_vsc = MagicMock()
        mock_vsc.resolve_collection_name.return_value = "test_coll"
        mock_vsc.search.return_value = []
        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = mock_vsc

        import code_indexer.server.services.search_service as ss_mod
        import code_indexer.server.services.governed_call as gc_mod

        ctx = SearchEventContext(
            username="alice", repo_alias="r", search_type="semantic", query_text="q"
        )
        token = _search_event_ctx.set(ctx)
        try:
            with (
                patch.object(
                    gc_mod,
                    "coalesced_query_embedding",
                    return_value=(FAKE_VEC, HIT_META),
                ),
                patch.object(
                    ss_mod,
                    "_load_repo_config",
                    return_value={"embedding_provider": "voyage-ai"},
                ),
                patch(
                    "code_indexer.server.services.search_service.BackendFactory"
                ) as mock_bf,
                patch(
                    "code_indexer.server.services.search_service.EmbeddingProviderFactory"
                ) as mock_epf,
            ):
                mock_bf.create.return_value = mock_backend
                mock_epf.create.return_value = MagicMock()
                SemanticSearchService()._perform_semantic_search(
                    "/fake/repo", "q", 5, False
                )
        finally:
            _search_event_ctx.reset(token)

        assert ctx.voyage_cache_hit is True
        assert ctx.voyage_cache_mode == "on"
        assert ctx.voyage_latency_ms is None

    def test_cohere_fsv_generate_embedding_writes_cohere_fields_to_ctx(
        self, monkeypatch
    ):
        """C5 gap: Cohere-provider FSV search must write cohere_* fields, NOT voyage_* fields.

        Configures a mock Cohere provider (get_provider_name returns "cohere"),
        sets up _search_event_ctx, calls FSV search() with coalesced_query_embedding
        mocked to return EmbeddingCacheMetadata with key_found=True, and asserts
        that cohere_cache_hit is True and voyage_cache_hit remains None.
        """
        import numpy as np
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
        from code_indexer.server.services.search_event_context import (
            SearchEventContext,
            _search_event_ctx,
        )
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        DIMS = 4
        FAKE_VEC = [0.1, 0.2, 0.3, 0.4]
        HIT_META = EmbeddingCacheMetadata(
            key_found=True, cache_mode="on", provider_latency_ms=None
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            base_path = Path(tmp_dir)
            store = FilesystemVectorStore(base_path=base_path, project_root=base_path)
            store.create_collection("test_coll", vector_size=DIMS)

            # Upsert one vector and build HNSW index
            vec = np.array(FAKE_VEC, dtype=np.float32)
            store.upsert_points(
                "test_coll",
                [
                    {
                        "id": "pt1",
                        "vector": vec.tolist(),
                        "payload": {"content": "hello", "file_path": "a.py"},
                    }
                ],
            )
            store.end_indexing("test_coll")

            # Mock Cohere provider (get_provider_name returns "cohere")
            mock_cohere_provider = MagicMock()
            mock_cohere_provider.get_provider_name.return_value = "cohere"

            ctx = SearchEventContext(
                username="alice",
                repo_alias="repo1",
                search_type="semantic",
                query_text="test",
            )
            token = _search_event_ctx.set(ctx)
            try:
                import code_indexer.storage.filesystem_vector_store as fsv_mod

                with patch.object(
                    fsv_mod,
                    "coalesced_query_embedding",
                    return_value=(FAKE_VEC, HIT_META),
                ):
                    store.search(
                        query="test query",
                        embedding_provider=mock_cohere_provider,
                        collection_name="test_coll",
                        limit=5,
                    )
            finally:
                _search_event_ctx.reset(token)

        # Cohere provider: cohere_* fields must be set; voyage_* must remain None
        assert ctx.cohere_cache_hit is True, (
            "FSV with Cohere provider must write key_found=True to cohere_cache_hit"
        )
        assert ctx.cohere_cache_mode == "on", (
            "FSV with Cohere provider must write cache_mode='on' to cohere_cache_mode"
        )
        assert ctx.cohere_latency_ms is None  # hit_meta has None latency
        assert ctx.voyage_cache_hit is None, (
            "FSV with Cohere provider must NOT write to voyage_cache_hit"
        )
        assert ctx.voyage_cache_mode is None, (
            "FSV with Cohere provider must NOT write to voyage_cache_mode"
        )

    def test_fts_search_has_null_provider_fields(
        self, writer, monkeypatch, wire_app_module
    ):
        """Spec A2: FTS search -> all 6 provider cache fields remain None.

        FTS does not call coalesced_query_embedding, so no metadata is
        written to ctx and all provider fields in the record remain None.
        """
        import code_indexer.server.mcp.handlers.search as sh

        monkeypatch.setattr(
            sh, "_get_search_event_writer", lambda: writer, raising=False
        )
        # FTS does not call coalesced_query_embedding — ctx fields remain None
        wire_app_module.semantic_query_manager.query_user_repositories.return_value = (
            _qm_result(3)
        )

        sh.search_code(_params(search_mode="fts"), _make_user("alice"))

        assert len(writer.enqueued) == 1
        rec = writer.enqueued[0]
        assert rec.voyage_cache_hit is None
        assert rec.voyage_cache_mode is None
        assert rec.voyage_latency_ms is None
        assert rec.cohere_cache_hit is None
        assert rec.cohere_cache_mode is None
        assert rec.cohere_latency_ms is None


# ---------------------------------------------------------------------------
# Tests: _write_embed_meta_to_event_ctx direct (Defect B regression guard)
# ---------------------------------------------------------------------------


class TestWriteEmbedMetaToEventCtxDirect:
    """Defect B regression: _write_embed_meta_to_event_ctx must populate
    SearchEventContext fields when called with non-None EmbeddingCacheMetadata.

    Calls the FSV helper directly (not via search_code or FSV.search) to assert
    the function correctly routes metadata to voyage_* or cohere_* ctx fields.
    """

    def test_voyage_shadow_miss_populates_voyage_fields(self):
        """shadow-mode MISS with voyage provider -> voyage_* ctx fields populated."""
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
        from code_indexer.server.services.search_event_context import (
            SearchEventContext,
            _search_event_ctx,
        )
        from code_indexer.storage.filesystem_vector_store import (
            _write_embed_meta_to_event_ctx,
        )

        miss_meta = EmbeddingCacheMetadata(
            key_found=False, cache_mode="shadow", provider_latency_ms=42
        )
        ctx = SearchEventContext(
            username="alice", repo_alias="r", search_type="semantic", query_text="q"
        )
        token = _search_event_ctx.set(ctx)
        try:
            _write_embed_meta_to_event_ctx(miss_meta, provider_name="voyage-ai")
        finally:
            _search_event_ctx.reset(token)

        assert ctx.voyage_cache_hit is False, (
            "_write_embed_meta_to_event_ctx must set voyage_cache_hit=False on MISS"
        )
        assert ctx.voyage_cache_mode == "shadow"
        assert ctx.voyage_latency_ms == 42
        assert ctx.cohere_cache_hit is None, (
            "voyage provider must not touch cohere fields"
        )

    def test_voyage_shadow_hit_populates_voyage_fields(self):
        """shadow-mode HIT with voyage provider -> voyage_* ctx fields populated."""
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
        from code_indexer.server.services.search_event_context import (
            SearchEventContext,
            _search_event_ctx,
        )
        from code_indexer.storage.filesystem_vector_store import (
            _write_embed_meta_to_event_ctx,
        )

        hit_meta = EmbeddingCacheMetadata(
            key_found=True, cache_mode="shadow", provider_latency_ms=15
        )
        ctx = SearchEventContext(
            username="alice", repo_alias="r", search_type="semantic", query_text="q"
        )
        token = _search_event_ctx.set(ctx)
        try:
            _write_embed_meta_to_event_ctx(hit_meta, provider_name="voyage-ai")
        finally:
            _search_event_ctx.reset(token)

        assert ctx.voyage_cache_hit is True
        assert ctx.voyage_cache_mode == "shadow"
        assert ctx.voyage_latency_ms == 15
        assert ctx.cohere_cache_hit is None

    def test_cohere_shadow_miss_populates_cohere_fields_only(self):
        """shadow-mode MISS with cohere provider -> cohere_* set, voyage_* stay None."""
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
        from code_indexer.server.services.search_event_context import (
            SearchEventContext,
            _search_event_ctx,
        )
        from code_indexer.storage.filesystem_vector_store import (
            _write_embed_meta_to_event_ctx,
        )

        miss_meta = EmbeddingCacheMetadata(
            key_found=False, cache_mode="shadow", provider_latency_ms=30
        )
        ctx = SearchEventContext(
            username="bob", repo_alias="r2", search_type="semantic", query_text="q2"
        )
        token = _search_event_ctx.set(ctx)
        try:
            _write_embed_meta_to_event_ctx(miss_meta, provider_name="cohere")
        finally:
            _search_event_ctx.reset(token)

        assert ctx.cohere_cache_hit is False
        assert ctx.cohere_cache_mode == "shadow"
        assert ctx.cohere_latency_ms == 30
        assert ctx.voyage_cache_hit is None, (
            "cohere provider must not touch voyage fields"
        )
        assert ctx.voyage_cache_mode is None, (
            "cohere provider must not touch voyage fields"
        )
