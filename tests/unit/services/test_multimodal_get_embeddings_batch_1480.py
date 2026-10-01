"""Bug #1480 follow-up: multimodal clients must implement the standard
EmbeddingProvider.get_embeddings_batch() contract.

The server-side embedding path (EmbeddingCoalescer) calls
``provider.get_embeddings_batch(texts, ...)``. The multimodal clients
previously only implemented ``get_multimodal_embedding*`` / ``get_embedding``,
so a server-side multimodal query (parallel/failover strategy fan-out via
``query_multimodal_only``) raised ``AttributeError: 'VoyageMultimodalClient'
object has no attribute 'get_embeddings_batch'`` and zeroed the whole query.

These are existence/signature-contract guards that both multimodal clients
implement a callable ``get_embeddings_batch`` whose first parameter is
``texts`` (the shape the coalescer calls). The end-to-end embedding/delegation
behaviour is proven by the real front-door E2E on staging (a dual-provider
repo query returns the multimodal doc), not by mocking these clients here.
"""

import inspect

from code_indexer.config import VoyageAIConfig, CohereConfig, VOYAGE_MULTIMODAL_MODEL
from code_indexer.services.voyage_multimodal import VoyageMultimodalClient
from code_indexer.services.cohere_multimodal import CohereMultimodalClient


def _accepts_texts_first_arg(method) -> bool:
    """The contract's first positional parameter (after self) is ``texts``."""
    params = list(inspect.signature(method).parameters)
    return bool(params) and params[0] == "texts"


class TestMultimodalGetEmbeddingsBatchContract:
    def test_voyage_multimodal_implements_get_embeddings_batch(self):
        client = VoyageMultimodalClient(VoyageAIConfig(model=VOYAGE_MULTIMODAL_MODEL))
        assert hasattr(client, "get_embeddings_batch")
        assert callable(client.get_embeddings_batch)
        assert _accepts_texts_first_arg(client.get_embeddings_batch)

    def test_cohere_multimodal_implements_get_embeddings_batch(self):
        client = CohereMultimodalClient(CohereConfig(model="embed-v4.0"))
        assert hasattr(client, "get_embeddings_batch")
        assert callable(client.get_embeddings_batch)
        assert _accepts_texts_first_arg(client.get_embeddings_batch)
