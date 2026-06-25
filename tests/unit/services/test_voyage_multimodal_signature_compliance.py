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


# ---------------------------------------------------------------------------
# Test 5 (Bug #1212): get_provider_name() exists and returns correct identifier
# ---------------------------------------------------------------------------


class TestGetProviderName1212:
    """Bug #1212: VoyageMultimodalClient is missing get_provider_name().

    VoyageMultimodalClient is used as an EmbeddingProvider in the multi-index
    query path.  filesystem_vector_store.py calls embedding_provider.get_provider_name()
    to route telemetry.  The missing method raises AttributeError which is swallowed
    as a WARNING, silently dropping the multimodal contribution from results.

    The correct return value is "voyage-ai" because:
    - VoyageAIClient.get_provider_name() returns "voyage-ai" (voyage_ai.py:650).
    - _write_embed_meta_to_event_ctx checks 'if "cohere" in provider_name.lower()'
      to route telemetry; anything else routes to the voyage branch — so returning
      "voyage-ai" correctly selects voyage telemetry for a Voyage multimodal client.
    - The value is used for labelling/telemetry only, not for collection keying.
    """

    EXPECTED_PROVIDER_NAME = "voyage-ai"

    def test_get_provider_name_method_exists(self, client):
        """VoyageMultimodalClient must have a get_provider_name() method."""
        assert hasattr(client, "get_provider_name"), (
            "VoyageMultimodalClient is missing get_provider_name() — "
            "required by EmbeddingProvider ABC and called by filesystem_vector_store.py"
        )
        assert callable(client.get_provider_name), "get_provider_name must be callable"

    def test_get_provider_name_returns_voyage_ai(self, client):
        """get_provider_name() must return 'voyage-ai' to route telemetry correctly."""
        result = client.get_provider_name()

        assert result == self.EXPECTED_PROVIDER_NAME, (
            f"Expected get_provider_name() == {self.EXPECTED_PROVIDER_NAME!r}, "
            f"got {result!r}. "
            "The return value must match VoyageAIClient.get_provider_name() so "
            "_write_embed_meta_to_event_ctx routes to the voyage telemetry branch."
        )

    def test_get_provider_name_returns_string(self, client):
        """get_provider_name() must return a str."""
        result = client.get_provider_name()
        assert isinstance(result, str), f"Expected str, got {type(result).__name__}"

    def test_get_provider_name_not_cohere(self, client):
        """Return value must NOT contain 'cohere' — telemetry would route incorrectly."""
        result = client.get_provider_name()
        assert "cohere" not in result.lower(), (
            f"get_provider_name() returned {result!r} which contains 'cohere'; "
            "this would incorrectly route voyage multimodal telemetry to cohere fields."
        )


# ---------------------------------------------------------------------------
# Test 6 (Bug #1212): multi-index path calls get_provider_name() without AttributeError
# ---------------------------------------------------------------------------


class TestMultiIndexGetProviderNameNotDropped1212:
    """Bug #1212: multi-index path must not silently drop multimodal contribution.

    The multi-index query service catches ANY exception (including AttributeError
    from a missing method) and logs a WARNING, silently dropping the multimodal
    contribution.  This test drives the path that calls get_provider_name() and
    asserts the call succeeds — no AttributeError, no multimodal_index WARNING.
    """

    GET_PROVIDER_NAME_ERROR_FRAGMENT = "get_provider_name"

    def test_get_provider_name_called_without_attribute_error(
        self, multimodal_service, caplog
    ):
        """Drive the multi-index search path; assert no AttributeError on get_provider_name.

        The mock vector_store.search side_effect additionally calls
        embedding_provider.get_provider_name() to replicate the
        filesystem_vector_store.py path that triggers Bug #1212.
        """
        service, real_multimodal_client = multimodal_service

        # Extend the existing mock search side_effect to also call get_provider_name()
        # This replicates filesystem_vector_store.py:2670 where get_provider_name() is called.
        original_side_effect = service.vector_store.search.side_effect

        def search_with_get_provider_name(
            query, embedding_provider, collection_name, **kw
        ):
            # Call get_provider_name() exactly as filesystem_vector_store.py does
            _provider_name = embedding_provider.get_provider_name()
            return original_side_effect(
                query, embedding_provider, collection_name, **kw
            )

        service.vector_store.search.side_effect = search_with_get_provider_name

        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.services.multi_index_query_service",
        ):
            service.query(
                query_text="find authentication code",
                limit=SEARCH_LIMIT,
                collection_name="voyage-code-3",
            )

        # Assert no multimodal_index failure warning (AttributeError would cause this)
        attribute_error_warnings = [
            r
            for r in caplog.records
            if "multimodal_index query failed" in r.message
            and self.GET_PROVIDER_NAME_ERROR_FRAGMENT in r.message
        ]
        assert not attribute_error_warnings, (
            f"get_provider_name() raised AttributeError that was silently swallowed: "
            f"{[r.message for r in attribute_error_warnings]}"
        )

        # Assert no multimodal_index failure at all (contribution must NOT be dropped)
        any_multimodal_failures = [
            r for r in caplog.records if "multimodal_index query failed" in r.message
        ]
        assert not any_multimodal_failures, (
            f"Multimodal contribution was silently dropped: "
            f"{[r.message for r in any_multimodal_failures]}"
        )
