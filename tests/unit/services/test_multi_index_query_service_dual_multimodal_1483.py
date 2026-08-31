"""Real-infrastructure regression test for Bug #1483.

Bug #1483: on a repo with BOTH multimodal collections present
(voyage-multimodal-3 AND embed-v4.0-multimodal), MultiIndexQueryService had
two independently-selected, disagreeing mechanisms:
  - _get_multimodal_provider() checked the Cohere collection FIRST and
    returned a CohereMultimodalClient (1536-dim query vector).
  - _query_multimodal_index() picked `actual_collection` as the FIRST entry
    of MULTIMODAL_MODELS that exists on disk -> voyage-multimodal-3
    (1024-dim collection).
The query text was therefore embedded in one vector space and searched
against a collection built in a DIFFERENT vector space -> HNSW's dimension
check raises -> the multimodal branch is caught fail-open -> zero multimodal
results, even though both multimodal collections legitimately have data that
matches the query.

This test builds THREE real on-disk collections via a real
FilesystemVectorStore (code + voyage-multimodal-3 + embed-v4.0-multimodal),
each with a DIFFERENT vector dimension (mirroring the real-world 1024 vs
1536 split), and drives a real query through MultiIndexQueryService.query().
Only the outbound network HTTP call inside each multimodal provider
(get_multimodal_embedding) is stubbed -- the provider objects themselves
(VoyageMultimodalClient / CohereMultimodalClient), the on-disk collection
dimensions, and the full HNSW search path are all real, never mocked.

RED (pre-fix): only 1 result (code) -- both multimodal collections are
either skipped or raise a dimension mismatch depending on selection order,
so their real, matching data never reaches the final merged results.
GREEN (post-fix): all 3 results are present -- each multimodal collection is
queried with ITS OWN matching provider, so no dimension mismatch ever
occurs for a correctly-configured dual-provider repo.
"""

from typing import Any, Dict, List, Optional

import pytest

from code_indexer.config import (
    COHERE_MULTIMODAL_MODEL,
    VOYAGE_MULTIMODAL_MODEL,
)
from code_indexer.services.embedding_provider import (
    BatchEmbeddingResult,
    EmbeddingProvider,
    EmbeddingResult,
)
from code_indexer.services.multi_index_query_service import MultiIndexQueryService
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

CODE_DIM = 3
VOYAGE_MULTIMODAL_DIM = 4
COHERE_MULTIMODAL_DIM = 6


class _FixedVectorCodeProvider(EmbeddingProvider):
    """Minimal real EmbeddingProvider double for the code collection -- always
    returns the same fixed-length vector. No network calls."""

    def __init__(self, vector: List[float], console=None):
        super().__init__(console)
        self._vector = vector

    def get_embedding(
        self, text: str, model: Optional[str] = None, *, embedding_purpose=None
    ) -> List[float]:
        return self._vector

    def get_embeddings_batch(
        self,
        texts: List[str],
        model: Optional[str] = None,
        *,
        embedding_purpose=None,
        retry: bool = True,
    ) -> List[List[float]]:
        return [self._vector for _ in texts]

    def get_embedding_with_metadata(
        self, text: str, model: Optional[str] = None, *, embedding_purpose=None
    ) -> EmbeddingResult:
        return EmbeddingResult(
            embedding=self._vector,
            model="voyage-code-3",
            tokens_used=len(text.split()),
            provider="fake-voyage-ai",
        )

    def get_embeddings_batch_with_metadata(
        self, texts: List[str], model: Optional[str] = None, *, embedding_purpose=None
    ) -> BatchEmbeddingResult:
        return BatchEmbeddingResult(
            embeddings=[self._vector for _ in texts],
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
            "dimensions": CODE_DIM,
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
    vector: List[float],
    payload: Dict[str, Any],
) -> None:
    """Build one real on-disk collection with a single real chunk."""
    store.create_collection(collection_name, vector_size=len(vector))
    store.upsert_points(
        collection_name,
        [{"id": f"{collection_name}-pt1", "vector": vector, "payload": payload}],
    )
    store.end_indexing(collection_name)


@pytest.fixture(autouse=True)
def _stub_outbound_multimodal_http_calls(monkeypatch):
    """Stub ONLY the outbound HTTP embedding call on each real multimodal
    client class -- get_embedding()/get_multimodal_embedding() dispatch,
    provider selection, and the full HNSW search path all stay real."""
    from code_indexer.services.voyage_multimodal import VoyageMultimodalClient
    from code_indexer.services.cohere_multimodal import CohereMultimodalClient

    def _fake_voyage_multimodal_embedding(
        self, text: str, image_paths, input_type: Optional[str] = None
    ) -> List[float]:
        return [0.1, 0.2, 0.3, 0.4][:VOYAGE_MULTIMODAL_DIM]

    def _fake_cohere_multimodal_embedding(
        self, text: str, image_paths, input_type: Optional[str] = None
    ) -> List[float]:
        return [0.1, 0.2, 0.3, 0.4, 0.5, 0.6][:COHERE_MULTIMODAL_DIM]

    monkeypatch.setattr(
        VoyageMultimodalClient,
        "get_multimodal_embedding",
        _fake_voyage_multimodal_embedding,
    )
    monkeypatch.setattr(
        CohereMultimodalClient,
        "get_multimodal_embedding",
        _fake_cohere_multimodal_embedding,
    )
    monkeypatch.setenv("VOYAGE_API_KEY", "test-voyage-key-not-real")
    monkeypatch.setenv("CO_API_KEY", "test-cohere-key-not-real")


class TestDualProviderMultimodalQuery:
    """Bug #1483: a repo with BOTH multimodal collections must be searchable
    via both spaces -- never a provider/collection dimension mismatch."""

    def test_dual_multimodal_collections_both_contribute_results(self, tmp_path):
        """A repo with code + voyage-multimodal-3 (dim=4) +
        embed-v4.0-multimodal (dim=6) must return results sourced from ALL
        THREE collections -- proving neither multimodal collection is ever
        queried with the WRONG provider's vector space."""
        project_root = tmp_path
        index_dir = project_root / ".code-indexer" / "index"
        store = FilesystemVectorStore(base_path=index_dir, project_root=project_root)

        code_vector = [0.9, 0.1, 0.2][:CODE_DIM]
        voyage_mm_vector = [0.1, 0.2, 0.3, 0.4][:VOYAGE_MULTIMODAL_DIM]
        cohere_mm_vector = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6][:COHERE_MULTIMODAL_DIM]

        _build_real_collection(
            store,
            "voyage-code-3",
            code_vector,
            {"path": "src/auth.py", "content": "def login(): ..."},
        )
        _build_real_collection(
            store,
            VOYAGE_MULTIMODAL_MODEL,
            voyage_mm_vector,
            {
                "path": "images/database-schema.png",
                "content": "database schema user_id username email password_hash",
            },
        )
        _build_real_collection(
            store,
            COHERE_MULTIMODAL_MODEL,
            cohere_mm_vector,
            {
                "path": "images/other-diagram.png",
                "content": "architecture diagram",
            },
        )

        code_provider = _FixedVectorCodeProvider(code_vector)
        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=store,
            embedding_provider=code_provider,
        )

        results, timing = service.query(
            query_text="database schema user_id username email password_hash",
            limit=10,
            collection_name="voyage-code-3",
        )

        result_paths = {r["payload"]["path"] for r in results}
        assert result_paths == {
            "src/auth.py",
            "images/database-schema.png",
            "images/other-diagram.png",
        }, (
            "Both multimodal collections must contribute a result -- a "
            f"dimension mismatch silently dropped one or both. Got: {result_paths}"
        )

    def test_single_provider_repo_still_works(self, tmp_path):
        """Regression guard: a repo with ONLY voyage-multimodal-3 (no Cohere
        collection) must remain byte-identical -- single-provider repos are
        not affected by the dual-provider fix."""
        project_root = tmp_path
        index_dir = project_root / ".code-indexer" / "index"
        store = FilesystemVectorStore(base_path=index_dir, project_root=project_root)

        code_vector = [0.9, 0.1, 0.2][:CODE_DIM]
        voyage_mm_vector = [0.1, 0.2, 0.3, 0.4][:VOYAGE_MULTIMODAL_DIM]

        _build_real_collection(
            store,
            "voyage-code-3",
            code_vector,
            {"path": "src/auth.py", "content": "def login(): ..."},
        )
        _build_real_collection(
            store,
            VOYAGE_MULTIMODAL_MODEL,
            voyage_mm_vector,
            {
                "path": "images/database-schema.png",
                "content": "database schema",
            },
        )

        code_provider = _FixedVectorCodeProvider(code_vector)
        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=store,
            embedding_provider=code_provider,
        )

        results, timing = service.query(
            query_text="database schema",
            limit=10,
            collection_name="voyage-code-3",
        )

        result_paths = {r["payload"]["path"] for r in results}
        assert result_paths == {"src/auth.py", "images/database-schema.png"}
