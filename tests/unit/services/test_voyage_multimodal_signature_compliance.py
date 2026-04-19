"""Tests for VoyageMultimodalClient.get_embedding signature compliance with EmbeddingProvider contract.

Bug #841: VoyageMultimodalClient.get_embedding lacks 'embedding_purpose' and 'model' kwargs
required by the EmbeddingProvider abstract base class, causing TypeError in MultiIndexQueryService
which silently drops the multimodal side of every dual-provider RRF query.
"""

import inspect
import logging
import os
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.code_indexer.config import VoyageAIConfig, VOYAGE_MULTIMODAL_MODEL
from src.code_indexer.services.embedding_provider import EmbeddingProvider
from src.code_indexer.services.voyage_multimodal import VoyageMultimodalClient

# ---------------------------------------------------------------------------
# Test-local named constants — no magic numbers anywhere in this file
# ---------------------------------------------------------------------------
EMBEDDING_DIMENSION = 1024
EMBEDDING_VALUE = 0.1
SEARCH_LIMIT = 5
MOCK_SCORE = 0.9
ELAPSED_MS = 5.0
HTTP_OK_STATUS = 200
TOTAL_TOKENS = 10
CHUNK_OFFSET = 0


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_api_key():
    """Provide a fake VOYAGE_API_KEY so the client can be instantiated."""
    with patch.dict(os.environ, {"VOYAGE_API_KEY": "test-key-placeholder"}):
        yield "test-key-placeholder"


@pytest.fixture
def voyage_config():
    """Minimal VoyageAIConfig for the multimodal client.

    Uses the config-level constant for the model name.
    Does not set api_endpoint — VoyageMultimodalClient.__init__ always overrides it
    to the multimodal endpoint regardless of what is supplied here.
    """
    return VoyageAIConfig(model=VOYAGE_MULTIMODAL_MODEL)


@pytest.fixture
def client(mock_api_key, voyage_config):
    """Return a VoyageMultimodalClient instance."""
    return VoyageMultimodalClient(voyage_config)


@pytest.fixture
def mock_httpx_client():
    """Patch httpx.Client and return a pre-configured mock that returns a 1024-dim embedding.

    Centralises all httpx mocking to avoid duplication across test methods.
    """
    mock_response = Mock()
    mock_response.json.return_value = {
        "data": [{"embedding": [EMBEDDING_VALUE] * EMBEDDING_DIMENSION}],
        "usage": {"total_tokens": TOTAL_TOKENS},
    }
    mock_response.raise_for_status = Mock()
    mock_response.status_code = HTTP_OK_STATUS

    mock_http = MagicMock()
    mock_http.__enter__.return_value = mock_http
    mock_http.post.return_value = mock_response

    with patch("httpx.Client", return_value=mock_http):
        yield mock_http


@pytest.fixture
def multimodal_service(mock_api_key, voyage_config, tmp_path, mock_httpx_client):
    """Build a MultiIndexQueryService with a real VoyageMultimodalClient (mocked HTTP).

    Creates the multimodal collection directory so has_multimodal_index() returns True.
    Sets mock_vector_store.search.side_effect inline via a lambda that calls
    embedding_provider.get_embedding(query, embedding_purpose='query') using a walrus
    operator — this replicates the exact bug path from filesystem_vector_store.py:2447.

    Returns (service, real_multimodal_client) so tests can spy on get_embedding.
    """
    from src.code_indexer.services.multi_index_query_service import (
        MultiIndexQueryService,
    )

    collection_dir = tmp_path / ".code-indexer" / "index" / VOYAGE_MULTIMODAL_MODEL
    collection_dir.mkdir(parents=True)

    real_multimodal_client = VoyageMultimodalClient(voyage_config)

    mock_vector_store = Mock()
    # side_effect is assigned inline as a lambda — no separate def.
    # The walrus operator (:=) calls get_embedding (triggering any TypeError from the bug)
    # before the tuple is returned, replicating filesystem_vector_store.py:2447.
    mock_vector_store.search.side_effect = (
        lambda query, embedding_provider, collection_name, **kw: (
            _ := embedding_provider.get_embedding(query, embedding_purpose="query"),
            (
                [
                    {
                        "file_path": "test.py",
                        "chunk_offset": CHUNK_OFFSET,
                        "score": MOCK_SCORE,
                        "content": "x",
                    }
                ],
                {"elapsed_ms": ELAPSED_MS},
            ),
        )[1]
    )

    mock_code_provider = Mock()
    mock_code_provider.get_embedding.return_value = [
        EMBEDDING_VALUE
    ] * EMBEDDING_DIMENSION

    service = MultiIndexQueryService(
        project_root=tmp_path,
        vector_store=mock_vector_store,
        embedding_provider=mock_code_provider,
    )
    service._multimodal_provider = real_multimodal_client

    return service, real_multimodal_client


# ---------------------------------------------------------------------------
# Test 1: get_embedding accepts embedding_purpose kwarg — no TypeError
# ---------------------------------------------------------------------------


class TestGetEmbeddingAcceptsEmbeddingPurposeKwarg:
    """Ensure calling get_embedding(text, embedding_purpose=...) does not raise TypeError."""

    def test_get_embedding_accepts_embedding_purpose_kwarg(
        self, client, mock_httpx_client
    ):
        """Call .get_embedding with embedding_purpose='query' — must not raise TypeError."""
        result = client.get_embedding("test query text", embedding_purpose="query")

        assert isinstance(result, list)
        assert len(result) == EMBEDDING_DIMENSION


# ---------------------------------------------------------------------------
# Test 2: get_embedding accepts model kwarg — no TypeError
# ---------------------------------------------------------------------------


class TestGetEmbeddingAcceptsModelKwarg:
    """Ensure calling get_embedding(text, model=...) does not raise TypeError."""

    def test_get_embedding_accepts_model_kwarg(self, client, mock_httpx_client):
        """Call .get_embedding with model=VOYAGE_MULTIMODAL_MODEL — must not raise TypeError."""
        result = client.get_embedding("test query text", model=VOYAGE_MULTIMODAL_MODEL)

        assert isinstance(result, list)
        assert len(result) == EMBEDDING_DIMENSION


# ---------------------------------------------------------------------------
# Test 3: Signature introspection — all contract params present
# ---------------------------------------------------------------------------


class TestGetEmbeddingSignatureMatchesContract:
    """Verify VoyageMultimodalClient.get_embedding signature satisfies EmbeddingProvider contract."""

    def test_get_embedding_signature_matches_embedding_provider_contract(self):
        """
        Use inspect.signature to assert all EmbeddingProvider.get_embedding params
        are accepted by VoyageMultimodalClient.get_embedding.

        Minimum required params: text, model, embedding_purpose.
        """
        base_sig = inspect.signature(EmbeddingProvider.get_embedding)
        concrete_sig = inspect.signature(VoyageMultimodalClient.get_embedding)

        base_params = set(base_sig.parameters.keys()) - {"self"}
        concrete_params = set(concrete_sig.parameters.keys()) - {"self"}

        has_var_keyword = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in concrete_sig.parameters.values()
        )

        if not has_var_keyword:
            missing = base_params - concrete_params
            assert not missing, (
                f"VoyageMultimodalClient.get_embedding is missing params declared "
                f"by EmbeddingProvider abstract base: {missing}"
            )

        assert "model" in concrete_params or has_var_keyword, (
            "VoyageMultimodalClient.get_embedding must accept 'model' kwarg"
        )
        assert "embedding_purpose" in concrete_params or has_var_keyword, (
            "VoyageMultimodalClient.get_embedding must accept 'embedding_purpose' kwarg"
        )


# ---------------------------------------------------------------------------
# Test 4: MultiIndexQueryService does NOT log 'multimodal_index query failed'
# ---------------------------------------------------------------------------


class TestMultiIndexQueryServiceNoMultimodalFailureWarning:
    """Verify that MultiIndexQueryService does not emit the multimodal WARNING after the fix."""

    def test_multi_index_query_service_does_not_log_multimodal_failure(
        self, multimodal_service, caplog
    ):
        """
        Use the multimodal_service fixture (real VoyageMultimodalClient, mocked HTTP).

        The mock search lambda calls get_embedding(query, embedding_purpose='query'),
        replicating filesystem_vector_store.py:2447, so any TypeError from the missing
        kwarg is surfaced and caught by MultiIndexQueryService as a WARNING.

        Asserts:
        - The multimodal provider's get_embedding was actually called (not bypassed).
        - No 'multimodal_index query failed' WARNING is logged.
        """
        service, real_multimodal_client = multimodal_service

        with (
            patch.object(
                real_multimodal_client,
                "get_embedding",
                wraps=real_multimodal_client.get_embedding,
            ) as spy_get_embedding,
            caplog.at_level(
                logging.WARNING,
                logger="code_indexer.services.multi_index_query_service",
            ),
        ):
            service.query(
                query_text="find authentication code",
                limit=SEARCH_LIMIT,
                collection_name="voyage-code-3",
            )

        assert spy_get_embedding.called, (
            "Expected real_multimodal_client.get_embedding to be called "
            "but it was never invoked — the multimodal path was not exercised."
        )

        failure_warnings = [
            r for r in caplog.records if "multimodal_index query failed" in r.message
        ]
        assert not failure_warnings, (
            f"Expected no 'multimodal_index query failed' warnings but got: "
            f"{[r.message for r in failure_warnings]}"
        )
