"""Shared fixtures for daemon unit tests.

Provides a fake embedding provider for tests that need to run without real
VoyageAI API calls. The FakeEmbeddingProvider generates deterministic embeddings
based on text hashing, allowing tests to validate indexing and query flows
without external API dependencies.
"""

import hashlib
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import numpy as np
import pytest

from code_indexer.services.embedding_provider import (
    BatchEmbeddingResult,
    EmbeddingProvider,
    EmbeddingResult,
)


class FakeEmbeddingProvider(EmbeddingProvider):
    """Fake embedding provider for unit tests.

    Generates deterministic 1024-dimensional embeddings based on text content hash.
    This allows tests to verify indexing and query flows without requiring real
    VoyageAI API keys or making actual API calls.

    Embedding Strategy:
    - Uses SHA256 hash of text to seed numpy random generator
    - Returns consistent 1024-dim embeddings (voyage-3 default dimension)
    - Same text always produces same embedding (deterministic)
    """

    VECTOR_DIM = 1024  # voyage-3 default dimension

    def __init__(self, console=None):
        super().__init__(console)

    def _generate_embedding_from_text(self, text: str) -> List[float]:
        """Generate deterministic embedding from text content.

        Uses text hash to seed random generator for reproducible results.
        """
        # Create hash from text content
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        # Convert first 8 hex chars to seed (uint32 range)
        seed = int(text_hash[:8], 16)
        # Create RNG with seed for deterministic results
        rng = np.random.default_rng(seed)
        # Generate normalized embedding vector
        embedding = rng.random(self.VECTOR_DIM).astype(np.float32)
        # Normalize to unit length (important for cosine similarity)
        embedding = embedding / np.linalg.norm(embedding)
        return embedding.tolist()

    def get_embedding(self, text: str, model: Optional[str] = None) -> List[float]:
        """Generate embedding for a single text."""
        return self._generate_embedding_from_text(text)

    def get_embeddings_batch(
        self, texts: List[str], model: Optional[str] = None
    ) -> List[List[float]]:
        """Generate embeddings for multiple texts in batch."""
        return [self._generate_embedding_from_text(t) for t in texts]

    def get_embedding_with_metadata(
        self, text: str, model: Optional[str] = None
    ) -> EmbeddingResult:
        """Generate embedding with metadata."""
        return EmbeddingResult(
            embedding=self._generate_embedding_from_text(text),
            model="voyage-3",
            tokens_used=len(text.split()),
            provider="fake-voyage-ai",
        )

    def get_embeddings_batch_with_metadata(
        self, texts: List[str], model: Optional[str] = None
    ) -> BatchEmbeddingResult:
        """Generate batch embeddings with metadata."""
        return BatchEmbeddingResult(
            embeddings=[self._generate_embedding_from_text(t) for t in texts],
            model="voyage-3",
            total_tokens_used=sum(len(t.split()) for t in texts),
            provider="fake-voyage-ai",
        )

    def health_check(self) -> bool:
        """Check if the embedding provider is healthy."""
        return True

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the current model."""
        return {
            "name": "voyage-3",
            "provider": "fake-voyage-ai",
            "dimensions": self.VECTOR_DIM,
            "max_tokens": 16000,
            "supports_batch": True,
            "api_endpoint": "fake://test",
        }

    def get_provider_name(self) -> str:
        """Get the name of this embedding provider."""
        return "fake-voyage-ai"

    def get_current_model(self) -> str:
        """Get the current active model name."""
        return "voyage-3"

    def supports_batch_processing(self) -> bool:
        """Check if provider supports efficient batch processing."""
        return True

    def _get_model_token_limit(self) -> int:
        """Get token limit for current model.

        Required by file chunking manager for batch size calculations.
        Returns conservative value matching voyage-3 API limit.
        """
        return 120000


@pytest.fixture(autouse=True)
def mock_embedding_provider_for_staleness_tests(request):
    """Auto-use fixture to mock embedding provider for staleness tests.

    This fixture automatically patches EmbeddingProviderFactory.create to return
    a FakeEmbeddingProvider for tests in these specific test modules:
    - test_daemon_staleness_detection.py
    - test_daemon_staleness_ordering_bug.py

    Tests in other modules are NOT affected by this fixture.
    """
    # Only apply to staleness-related test modules
    test_module = request.node.module.__name__
    staleness_modules = [
        "test_daemon_staleness_detection",
        "test_daemon_staleness_ordering_bug",
    ]

    if not any(mod in test_module for mod in staleness_modules):
        # Not a staleness test - don't apply mock
        yield
        return

    # Create the fake provider instance
    fake_provider = FakeEmbeddingProvider()

    # Patch EmbeddingProviderFactory.create to return fake provider
    with patch(
        "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
        return_value=fake_provider,
    ):
        yield fake_provider
