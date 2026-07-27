"""Real-infrastructure integration test for Bug #1480 — server-side multimodal
query fan-out.

CLAUDE.md DoD requirement: no mocking of FilesystemVectorStore/vector store
itself. This test builds REAL on-disk code + multimodal collections via
actual FilesystemVectorStore writes (create_collection/upsert_points/
end_indexing — the exact real-infra pattern used by
test_fsv_emit_embed_event_1293_a8.py), using a deterministic FAKE (not
Mock()) embedding-provider double that implements the real EmbeddingProvider
interface with no network calls (mirrors tests/unit/daemon/conftest.py's
FakeEmbeddingProvider pattern). Only the config/backend/embedding FACTORIES
are patched to hand the real objects to SemanticSearchService — never the
vector store or FilesystemVectorStore itself.

Proves end-to-end, through the real production entry point
SemanticSearchService.search_repository_path():
  1. A repo with BOTH a code and a multimodal collection returns results that
     include a multimodal-collection chunk.
  2. A code-only repo (no multimodal collection built) returns results
     IDENTICAL to a direct store.search() call — i.e. the multimodal branch
     never triggers and behavior is unchanged for code-only repos.
"""

import hashlib
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from code_indexer.services.embedding_provider import (
    BatchEmbeddingResult,
    EmbeddingProvider,
    EmbeddingResult,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_DIM = 8


class DeterministicFakeEmbeddingProvider(EmbeddingProvider):
    """Real EmbeddingProvider implementation with deterministic, hash-derived
    vectors — no network calls, no mocking of the interface under test."""

    def __init__(self, console=None):
        super().__init__(console)

    def _vector_for(self, text: str) -> List[float]:
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        seed = int(text_hash[:8], 16)
        rng = np.random.default_rng(seed)
        vec = rng.random(VECTOR_DIM).astype(np.float32)
        vec = vec / np.linalg.norm(vec)
        return vec.tolist()  # type: ignore[no-any-return]

    def get_embedding(
        self, text: str, model: Optional[str] = None, *, embedding_purpose=None
    ) -> List[float]:
        return self._vector_for(text)

    def get_embeddings_batch(
        self,
        texts: List[str],
        model: Optional[str] = None,
        *,
        embedding_purpose=None,
        retry: bool = True,
    ) -> List[List[float]]:
        return [self._vector_for(t) for t in texts]

    def get_embedding_with_metadata(
        self, text: str, model: Optional[str] = None, *, embedding_purpose=None
    ) -> EmbeddingResult:
        return EmbeddingResult(
            embedding=self._vector_for(text),
            model="voyage-code-3",
            tokens_used=len(text.split()),
            provider="fake-voyage-ai",
        )

    def get_embeddings_batch_with_metadata(
        self, texts: List[str], model: Optional[str] = None, *, embedding_purpose=None
    ) -> BatchEmbeddingResult:
        return BatchEmbeddingResult(
            embeddings=[self._vector_for(t) for t in texts],
            model="voyage-code-3",
            total_tokens_used=sum(len(t.split()) for t in texts),
            provider="fake-voyage-ai",
        )

    def health_check(self, *, test_api: bool = False) -> bool:
        return True

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "name": "voyage-code-3",
            "provider": "fake-voyage-ai",
            "dimensions": VECTOR_DIM,
            "max_tokens": 16000,
            "supports_batch": True,
            "api_endpoint": "fake://test",
        }

    def get_provider_name(self) -> str:
        return "voyage-ai"

    def get_current_model(self) -> str:
        return "voyage-code-3"

    def supports_batch_processing(self) -> bool:
        return True


def _build_real_collection(
    store: FilesystemVectorStore,
    collection_name: str,
    provider: DeterministicFakeEmbeddingProvider,
    text: str,
    payload: Dict[str, Any],
) -> None:
    """Build one real on-disk collection with a single real chunk."""
    store.create_collection(collection_name, vector_size=VECTOR_DIM)
    vector = provider.get_embedding(text)
    store.upsert_points(
        collection_name,
        [{"id": f"{collection_name}-pt1", "vector": vector, "payload": payload}],
    )
    store.end_indexing(collection_name)


@pytest.fixture(autouse=True)
def _patch_config_manager():
    """Patch ConfigManager (config content is unused — BackendFactory and
    EmbeddingProviderFactory are both patched to hand over the real
    pre-built store/provider) and set app.state.http_client_factory /
    _server_hnsw_cache, mirroring the established pattern in
    test_search_service_precomputed_vector.py."""
    from code_indexer.server.fault_injection.null_factory import NullFaultFactory
    import code_indexer.server.app as app_module

    had_factory = hasattr(app_module.app.state, "http_client_factory")
    original_factory = getattr(app_module.app.state, "http_client_factory", None)
    app_module.app.state.http_client_factory = NullFaultFactory()

    mock_cfg = MagicMock()
    mock_cfg.embedding_provider = "voyage-ai"
    with patch(
        "code_indexer.server.services.search_service.ConfigManager"
        ".create_with_backtrack"
    ) as mock_cm_cls:
        mock_cm = MagicMock()
        mock_cm.get_config.return_value = mock_cfg
        mock_cm_cls.return_value = mock_cm
        yield

    if had_factory:
        app_module.app.state.http_client_factory = original_factory
    elif hasattr(app_module.app.state, "http_client_factory"):
        del app_module.app.state.http_client_factory


class TestMultimodalRealInfraFanOut:
    """Real FilesystemVectorStore, real HNSW, no mocking of the vector store."""

    def test_both_collections_repo_returns_multimodal_chunk(self, tmp_path):
        """A repo with a real code AND a real multimodal collection on disk
        must return a result sourced from the multimodal collection, via the
        real production entry point search_repository_path()."""
        from code_indexer.server.models.api_models import SemanticSearchRequest
        from code_indexer.server.services.search_service import (
            SemanticSearchService,
        )

        repo_path = tmp_path
        base_path = repo_path / ".code-indexer" / "index"
        store = FilesystemVectorStore(base_path=base_path, project_root=repo_path)
        provider = DeterministicFakeEmbeddingProvider()

        _build_real_collection(
            store,
            "voyage-code-3",
            provider,
            "def login(user, password): return authenticate(user, password)",
            {"path": "src/auth.py", "content": "def login(): ..."},
        )
        _build_real_collection(
            store,
            "voyage-multimodal-3",
            provider,
            "architecture diagram screenshot",
            {"path": "docs/diagram.png", "content": "architecture diagram"},
        )

        backend = MagicMock()
        backend.get_vector_store_client.return_value = store

        # The multimodal provider construction (VoyageMultimodalClient) is an
        # EXTERNAL NETWORK collaborator, not the vector store under test --
        # mocking it here matches CLAUDE.md's "real vector store, fake
        # external network provider" mocking-hierarchy tier. Without this,
        # a real VOYAGE_API_KEY present in the dev environment would make a
        # genuine live network call and produce a 1024-dim vector mismatched
        # against this test's small 8-dim fake collection.
        from code_indexer.services.multi_index_query_service import (
            MultiIndexQueryService,
        )

        with (
            patch(
                "code_indexer.server.services.search_service.BackendFactory.create",
                return_value=backend,
            ),
            patch(
                "code_indexer.server.services.search_service.EmbeddingProviderFactory.create",
                return_value=provider,
            ),
            patch("code_indexer.server.app._server_hnsw_cache", None),
            patch.object(
                MultiIndexQueryService,
                "_get_multimodal_provider",
                return_value=provider,
            ),
        ):
            svc = SemanticSearchService()
            request = SemanticSearchRequest(query="diagram screenshot", limit=10)
            response = svc.search_repository_path(str(repo_path), request)

        result_paths = {item.file_path for item in response.results}
        assert "docs/diagram.png" in result_paths, (
            "Multimodal-collection chunk must appear in the final merged "
            f"results; got paths: {result_paths}"
        )
        assert "src/auth.py" in result_paths

    def test_code_only_repo_matches_direct_store_search(self, tmp_path):
        """A code-only repo (no multimodal collection ever built) must return
        results IDENTICAL to a direct store.search() call — proving the
        multimodal branch never triggers and behavior is byte-identical to
        pre-fix for repos without a multimodal collection."""
        from code_indexer.server.models.api_models import SemanticSearchRequest
        from code_indexer.server.services.search_service import (
            SemanticSearchService,
        )

        repo_path = tmp_path
        base_path = repo_path / ".code-indexer" / "index"
        store = FilesystemVectorStore(base_path=base_path, project_root=repo_path)
        provider = DeterministicFakeEmbeddingProvider()

        _build_real_collection(
            store,
            "voyage-code-3",
            provider,
            "def login(user, password): return authenticate(user, password)",
            {"path": "src/auth.py", "content": "def login(): ..."},
        )
        # No multimodal collection built for this repo.

        backend = MagicMock()
        backend.get_vector_store_client.return_value = store

        # Direct comparison call — same store, same query, same provider.
        direct_results, _ = store.search(
            query="authentication logic",
            embedding_provider=provider,
            collection_name="voyage-code-3",
            limit=10,
            return_timing=True,
            ef=50,
            no_embedding_cache_shortcut=False,
        )
        expected_paths = {r["payload"]["path"] for r in direct_results}

        with (
            patch(
                "code_indexer.server.services.search_service.BackendFactory.create",
                return_value=backend,
            ),
            patch(
                "code_indexer.server.services.search_service.EmbeddingProviderFactory.create",
                return_value=provider,
            ),
            patch("code_indexer.server.app._server_hnsw_cache", None),
        ):
            svc = SemanticSearchService()
            request = SemanticSearchRequest(query="authentication logic", limit=10)
            response = svc.search_repository_path(str(repo_path), request)

        actual_paths = {item.file_path for item in response.results}
        assert actual_paths == expected_paths, (
            "Code-only repo must return results identical to a direct "
            f"store.search() call; expected {expected_paths}, got {actual_paths}"
        )
