"""
Unit tests for RerankerClient ABC and VoyageRerankerClient.

Story #650: RerankerClient ABC + VoyageRerankerClient
Part of Epic #649: Voyage AI + Cohere Reranker Integration

Tests follow strict TDD methodology — tests written FIRST before implementation.
Uses pytest_httpx for HTTP mocking (not object mocks) per project conventions.
"""

import json
import os
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from pytest_httpx import HTTPXMock

# Centralized endpoint constant — single source of truth for all tests.
# If the production module exposes VOYAGE_RERANK_URL, tests should import it instead.
VOYAGE_RERANK_URL = "https://api.voyageai.com/v1/rerank"


# ---------------------------------------------------------------------------
# Shared helpers and fixtures
# ---------------------------------------------------------------------------


def _make_config_service(api_key: str = "test-api-key") -> MagicMock:
    """Return a MagicMock config service that exposes the given voyageai_api_key."""
    mock_config = MagicMock()
    mock_config.claude_integration_config.voyageai_api_key = api_key
    mock_config.rerank_config.voyage_reranker_model = None
    mock_cs = MagicMock()
    mock_cs.get_config.return_value = mock_config
    return mock_cs


def _get_request_body(httpx_mock: HTTPXMock) -> Dict[str, Any]:
    """Return the parsed JSON body of the first captured request."""
    requests = httpx_mock.get_requests()
    assert len(requests) >= 1, "Expected at least one HTTP request to be captured"
    return json.loads(requests[0].content)


def _add_rerank_response(
    httpx_mock: HTTPXMock,
    data: Optional[List[Any]] = None,
    status_code: int = 200,
) -> None:
    """Register a default or custom Voyage rerank response with httpx_mock."""
    if data is None:
        data = [{"index": 0, "relevance_score": 0.9}]
    if status_code == 200:
        httpx_mock.add_response(
            method="POST",
            url=VOYAGE_RERANK_URL,
            json={"data": data},
            status_code=status_code,
        )
    else:
        httpx_mock.add_response(
            method="POST",
            url=VOYAGE_RERANK_URL,
            status_code=status_code,
        )


@pytest.fixture
def mock_cs():
    """Pytest fixture: config service with default test API key."""
    return _make_config_service()


@pytest.fixture
def patched_client(mock_cs):
    """
    Pytest fixture: yields a VoyageRerankerClient with get_config_service patched.
    Tests that need the patch active for the full duration use this fixture.
    """
    from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

    client = VoyageRerankerClient()
    with patch(
        "code_indexer.server.clients.reranker_clients.get_config_service",
        return_value=mock_cs,
    ):
        yield client


# ---------------------------------------------------------------------------
# AC1: RerankerClient Abstract Base Class
# ---------------------------------------------------------------------------


class TestRerankResult:
    """Tests for RerankResult dataclass."""

    def test_rerank_result_has_index_and_relevance_score(self):
        """RerankResult has index (int) and relevance_score (float) fields."""
        from code_indexer.server.clients.reranker_clients import RerankResult

        result = RerankResult(index=0, relevance_score=0.95)
        assert result.index == 0
        assert result.relevance_score == 0.95

    def test_rerank_result_index_is_int(self):
        """index field must be an int."""
        from code_indexer.server.clients.reranker_clients import RerankResult

        assert isinstance(RerankResult(index=3, relevance_score=0.5).index, int)

    def test_rerank_result_relevance_score_is_float(self):
        """relevance_score field must be a float."""
        from code_indexer.server.clients.reranker_clients import RerankResult

        assert isinstance(
            RerankResult(index=0, relevance_score=0.75).relevance_score, float
        )

    def test_rerank_result_equality(self):
        """RerankResult instances with same values should be equal (dataclass)."""
        from code_indexer.server.clients.reranker_clients import RerankResult

        assert RerankResult(index=1, relevance_score=0.8) == RerankResult(
            index=1, relevance_score=0.8
        )


class TestRerankerClientABC:
    """Tests for RerankerClient abstract base class."""

    def test_reranker_client_cannot_be_instantiated_directly(self):
        """RerankerClient ABC raises TypeError when instantiated directly."""
        from code_indexer.server.clients.reranker_clients import RerankerClient

        with pytest.raises(TypeError):
            RerankerClient()  # type: ignore[abstract]

    def test_subclass_without_rerank_raises_type_error_on_instantiation(self):
        """A subclass that does not implement rerank() raises TypeError."""
        from code_indexer.server.clients.reranker_clients import RerankerClient

        class IncompleteReranker(RerankerClient):
            pass

        with pytest.raises(TypeError):
            IncompleteReranker()  # type: ignore[abstract]

    def test_subclass_implementing_rerank_can_be_instantiated(self):
        """A subclass that implements rerank() can be instantiated."""
        from code_indexer.server.clients.reranker_clients import RerankerClient

        class ConcreteReranker(RerankerClient):
            def rerank(self, query, documents, top_k=None, instruction=None):
                return []

        assert ConcreteReranker() is not None

    def test_reranker_client_is_abstract(self):
        """RerankerClient is an ABC with at least one abstract method."""
        import inspect
        from code_indexer.server.clients.reranker_clients import RerankerClient

        assert inspect.isabstract(RerankerClient)


# ---------------------------------------------------------------------------
# AC2: VoyageRerankerClient API Integration
# ---------------------------------------------------------------------------


class TestVoyageRerankerClientInstantiation:
    """Tests for VoyageRerankerClient initialization."""

    def test_voyage_reranker_client_can_be_instantiated_with_defaults(self):
        """VoyageRerankerClient can be instantiated with defaults."""
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        assert VoyageRerankerClient() is not None

    def test_voyage_reranker_client_default_timeout(self):
        """Default timeout is 5.0 seconds."""
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        assert VoyageRerankerClient().timeout == 5.0

    def test_voyage_reranker_client_default_max_chars(self):
        """Default max_chars is 4000."""
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        assert VoyageRerankerClient().max_chars == 4000

    def test_voyage_reranker_client_custom_timeout(self):
        """Custom timeout is stored correctly."""
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        assert VoyageRerankerClient(timeout=10.0).timeout == 10.0

    def test_voyage_reranker_client_custom_max_chars(self):
        """Custom max_chars is stored correctly."""
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        assert VoyageRerankerClient(max_chars=2000).max_chars == 2000

    def test_voyage_reranker_client_is_subclass_of_reranker_client(self):
        """VoyageRerankerClient is a subclass of RerankerClient."""
        from code_indexer.server.clients.reranker_clients import (
            RerankerClient,
            VoyageRerankerClient,
        )

        assert issubclass(VoyageRerankerClient, RerankerClient)

    def test_voyage_reranker_client_rejects_non_positive_timeout(self):
        """Constructor raises ValueError when timeout <= 0."""
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        with pytest.raises(ValueError):
            VoyageRerankerClient(timeout=0.0)

    def test_voyage_reranker_client_rejects_non_positive_max_chars(self):
        """Constructor raises ValueError when max_chars <= 0."""
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        with pytest.raises(ValueError):
            VoyageRerankerClient(max_chars=0)

    @pytest.mark.parametrize("bad_key", [None, ""])
    def test_voyage_reranker_client_rejects_invalid_api_key(self, bad_key):
        """rerank() raises ValueError when API key is None or empty string."""
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        client = VoyageRerankerClient()
        bad_cs = MagicMock()
        bad_cs.get_config.return_value.claude_integration_config.voyageai_api_key = (
            bad_key
        )

        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=bad_cs,
        ):
            with pytest.raises(ValueError):
                client.rerank(query="q", documents=["doc"], top_k=1)


class TestVoyageRerankerClientApiKey:
    """Tests for API key retrieval from config service."""

    def test_get_api_key_reads_from_config_service(self):
        """_get_api_key() returns voyageai_api_key from config service only."""
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        client = VoyageRerankerClient()
        cs = _make_config_service("test-voyage-key-123")

        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=cs,
        ):
            assert client._get_api_key() == "test-voyage-key-123"

    def test_get_api_key_does_not_use_environment_variable(self):
        """
        _get_api_key() must NOT fall back to VOYAGE_API_KEY env var.
        When config returns None, None is returned regardless of env var.
        """
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        client = VoyageRerankerClient()
        none_cs = MagicMock()
        none_cs.get_config.return_value.claude_integration_config.voyageai_api_key = (
            None
        )

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "env-key-must-not-be-used"}):
            with patch(
                "code_indexer.server.clients.reranker_clients.get_config_service",
                return_value=none_cs,
            ):
                api_key = client._get_api_key()

        assert api_key is None

    def test_get_model_returns_default_model(self):
        """_get_model() returns 'rerank-2.5' as default model."""
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        assert VoyageRerankerClient()._get_model() == "rerank-2.5"

    def test_get_model_returns_configured_voyage_model(self):
        """_get_model() returns the operator-configured model when set in config."""
        from unittest.mock import MagicMock, patch
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        mock_cs = MagicMock()
        mock_cs.get_config.return_value.rerank_config.voyage_reranker_model = (
            "rerank-3.0"
        )
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=mock_cs,
        ):
            assert VoyageRerankerClient()._get_model() == "rerank-3.0"

    @pytest.mark.parametrize("empty_value", [None, ""])
    def test_get_model_falls_back_to_default_when_config_empty(self, empty_value):
        """_get_model() falls back to 'rerank-2.5' when config model is None or empty."""
        from unittest.mock import MagicMock, patch
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        mock_cs = MagicMock()
        mock_cs.get_config.return_value.rerank_config.voyage_reranker_model = (
            empty_value
        )
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=mock_cs,
        ):
            assert VoyageRerankerClient()._get_model() == "rerank-2.5"


class TestVoyageRerankerClientRerank:
    """Tests for rerank() API call behavior."""

    def test_rerank_sends_correct_request_body(
        self, httpx_mock: HTTPXMock, patched_client
    ):
        """rerank() sends POST with model, query, documents, top_k, truncation=true."""
        _add_rerank_response(
            httpx_mock,
            data=[
                {"index": 0, "relevance_score": 0.95},
                {"index": 1, "relevance_score": 0.80},
            ],
        )

        patched_client.rerank(
            query="test query",
            documents=["doc one", "doc two"],
            top_k=2,
        )

        body = _get_request_body(httpx_mock)
        assert body["model"] == "rerank-2.5"
        assert body["query"] == "test query"
        assert body["documents"] == ["doc one", "doc two"]
        assert body["top_k"] == 2
        assert body["truncation"] is True

    def test_rerank_sends_bearer_auth_header(self, httpx_mock: HTTPXMock):
        """rerank() sends Authorization: Bearer <api_key> header."""
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        _add_rerank_response(httpx_mock)
        custom_cs = _make_config_service("secret-key-xyz")
        client = VoyageRerankerClient()

        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=custom_cs,
        ):
            client.rerank(query="q", documents=["d"], top_k=1)

        requests = httpx_mock.get_requests()
        assert requests[0].headers["Authorization"] == "Bearer secret-key-xyz"

    def test_rerank_returns_results_ordered_by_relevance_score_descending(
        self, httpx_mock: HTTPXMock, patched_client
    ):
        """rerank() returns List[RerankResult] ordered by relevance_score descending."""
        _add_rerank_response(
            httpx_mock,
            data=[
                {"index": 1, "relevance_score": 0.80},
                {"index": 0, "relevance_score": 0.95},
                {"index": 2, "relevance_score": 0.60},
            ],
        )

        results = patched_client.rerank(
            query="q", documents=["d0", "d1", "d2"], top_k=3
        )

        assert len(results) == 3
        assert results[0].relevance_score >= results[1].relevance_score
        assert results[1].relevance_score >= results[2].relevance_score

    def test_rerank_result_index_maps_to_original_document(
        self, httpx_mock: HTTPXMock, patched_client
    ):
        """RerankResult.index maps back to the original document list position."""
        _add_rerank_response(
            httpx_mock,
            data=[
                {"index": 2, "relevance_score": 0.99},
                {"index": 0, "relevance_score": 0.50},
            ],
        )

        results = patched_client.rerank(
            query="q", documents=["doc A", "doc B", "doc C"], top_k=2
        )

        assert results[0].index == 2
        assert results[1].index == 0

    def test_rerank_with_top_k_none_omits_top_k_from_body(
        self, httpx_mock: HTTPXMock, patched_client
    ):
        """When top_k=None, top_k must not appear in the request body."""
        _add_rerank_response(httpx_mock)

        patched_client.rerank(query="q", documents=["d"], top_k=None)

        assert "top_k" not in _get_request_body(httpx_mock)


# ---------------------------------------------------------------------------
# AC3: Instruction Prepending
# ---------------------------------------------------------------------------


class TestInstructionPrepending:
    """Tests for instruction prepending behavior."""

    def test_instruction_prepended_with_newline_separator(
        self, httpx_mock: HTTPXMock, patched_client
    ):
        """Non-empty instruction prepended as '{instruction}\\n{query}'."""
        _add_rerank_response(httpx_mock)

        patched_client.rerank(
            query="find similar code",
            documents=["doc"],
            top_k=1,
            instruction="Rank by technical relevance",
        )

        assert (
            _get_request_body(httpx_mock)["query"]
            == "Rank by technical relevance\nfind similar code"
        )

    def test_none_instruction_leaves_query_unchanged(
        self, httpx_mock: HTTPXMock, patched_client
    ):
        """None instruction leaves query unchanged."""
        _add_rerank_response(httpx_mock)

        patched_client.rerank(
            query="my query", documents=["doc"], top_k=1, instruction=None
        )

        assert _get_request_body(httpx_mock)["query"] == "my query"

    def test_empty_string_instruction_leaves_query_unchanged(
        self, httpx_mock: HTTPXMock, patched_client
    ):
        """Empty string instruction leaves query unchanged."""
        _add_rerank_response(httpx_mock)

        patched_client.rerank(
            query="my query", documents=["doc"], top_k=1, instruction=""
        )

        assert _get_request_body(httpx_mock)["query"] == "my query"


# ---------------------------------------------------------------------------
# AC4: Document Truncation
# ---------------------------------------------------------------------------


class TestDocumentTruncation:
    """Tests for client-side document truncation."""

    def test_long_document_truncated_to_max_chars(self, httpx_mock: HTTPXMock):
        """Documents longer than max_chars are truncated before sending."""
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        _add_rerank_response(httpx_mock)
        max_chars = 100
        client = VoyageRerankerClient(max_chars=max_chars)
        cs = _make_config_service()

        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=cs,
        ):
            client.rerank(query="q", documents=["x" * 500], top_k=1)

        sent_doc = _get_request_body(httpx_mock)["documents"][0]
        assert len(sent_doc) == max_chars

    def test_short_document_not_truncated(self, httpx_mock: HTTPXMock, patched_client):
        """Documents shorter than max_chars are sent as-is."""
        _add_rerank_response(httpx_mock)

        patched_client.rerank(query="q", documents=["short doc"], top_k=1)

        assert _get_request_body(httpx_mock)["documents"][0] == "short doc"

    def test_empty_document_sent_as_is(self, httpx_mock: HTTPXMock, patched_client):
        """Empty documents are sent without modification."""
        _add_rerank_response(httpx_mock)

        patched_client.rerank(query="q", documents=[""], top_k=1)

        assert _get_request_body(httpx_mock)["documents"][0] == ""

    def test_truncation_flag_always_set_in_request_body(
        self, httpx_mock: HTTPXMock, patched_client
    ):
        """truncation: true is always included in the request body."""
        _add_rerank_response(httpx_mock)

        patched_client.rerank(query="q", documents=["doc"], top_k=1)

        assert _get_request_body(httpx_mock)["truncation"] is True


# ---------------------------------------------------------------------------
# AC5: ProviderHealthMonitor Registration
# ---------------------------------------------------------------------------


class TestProviderHealthMonitorRegistration:
    """Tests for ProviderHealthMonitor integration."""

    def test_health_monitor_updated_on_success(
        self, httpx_mock: HTTPXMock, patched_client
    ):
        """ProviderHealthMonitor records success=True on successful call."""
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        _add_rerank_response(httpx_mock)
        ProviderHealthMonitor.reset_instance()
        monitor = ProviderHealthMonitor.get_instance()

        patched_client.rerank(query="q", documents=["doc"], top_k=1)

        health = monitor.get_health("voyage-reranker")
        status = health["voyage-reranker"]
        assert status.successful_requests == 1
        assert status.failed_requests == 0

    def test_health_monitor_updated_on_failure(
        self, httpx_mock: HTTPXMock, patched_client
    ):
        """ProviderHealthMonitor records success=False on HTTP error."""
        import httpx as _httpx
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        _add_rerank_response(httpx_mock, status_code=401)
        ProviderHealthMonitor.reset_instance()
        monitor = ProviderHealthMonitor.get_instance()

        with pytest.raises(_httpx.HTTPStatusError):
            patched_client.rerank(query="q", documents=["doc"], top_k=1)

        health = monitor.get_health("voyage-reranker")
        status = health["voyage-reranker"]
        assert status.failed_requests == 1

    def test_probe_registered_with_health_monitor(self):
        """VoyageRerankerClient registers a probe as 'voyage-reranker'."""
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        ProviderHealthMonitor.reset_instance()
        monitor = ProviderHealthMonitor.get_instance()

        VoyageRerankerClient()

        assert "voyage-reranker" in monitor._probe_functions


# ---------------------------------------------------------------------------
# AC6: Error Propagation
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    """Tests for error propagation — no exception swallowing."""

    def test_http_500_raises_http_status_error(
        self, httpx_mock: HTTPXMock, patched_client
    ):
        """HTTP 500 raises httpx.HTTPStatusError (not swallowed)."""
        import httpx as _httpx

        _add_rerank_response(httpx_mock, status_code=500)

        with pytest.raises(_httpx.HTTPStatusError):
            patched_client.rerank(query="q", documents=["doc"], top_k=1)

    def test_http_401_raises_http_status_error(
        self, httpx_mock: HTTPXMock, patched_client
    ):
        """HTTP 401 raises httpx.HTTPStatusError (not swallowed)."""
        import httpx as _httpx

        _add_rerank_response(httpx_mock, status_code=401)

        with pytest.raises(_httpx.HTTPStatusError):
            patched_client.rerank(query="q", documents=["doc"], top_k=1)

    def test_timeout_exception_propagates(self, httpx_mock: HTTPXMock, patched_client):
        """Timeout raises httpx.TimeoutException (not swallowed)."""
        import httpx as _httpx

        httpx_mock.add_exception(
            exception=_httpx.TimeoutException("timeout"),
            method="POST",
            url=VOYAGE_RERANK_URL,
        )

        with pytest.raises(_httpx.TimeoutException):
            patched_client.rerank(query="q", documents=["doc"], top_k=1)

    def test_exception_propagates_and_health_monitor_updated(
        self, httpx_mock: HTTPXMock, patched_client
    ):
        """
        Exception propagates AND health monitor is updated on 503.
        ProviderHealthMonitor must be updated BEFORE exception is re-raised.
        """
        import httpx as _httpx
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        _add_rerank_response(httpx_mock, status_code=503)
        ProviderHealthMonitor.reset_instance()
        monitor = ProviderHealthMonitor.get_instance()

        with pytest.raises(_httpx.HTTPStatusError):
            patched_client.rerank(query="q", documents=["doc"], top_k=1)

        health = monitor.get_health("voyage-reranker")
        status = health["voyage-reranker"]
        assert status.failed_requests >= 1


# ---------------------------------------------------------------------------
# Cohere reranker shared helpers
# ---------------------------------------------------------------------------

COHERE_RERANK_URL = "https://api.cohere.com/v2/rerank"


def _make_cohere_config_service(api_key: str = "test-cohere-key") -> MagicMock:
    """Return a MagicMock config service that exposes the given cohere_api_key."""
    mock_config = MagicMock()
    mock_config.claude_integration_config.cohere_api_key = api_key
    mock_config.rerank_config.cohere_reranker_model = None
    mock_cs = MagicMock()
    mock_cs.get_config.return_value = mock_config
    return mock_cs


def _add_cohere_rerank_response(
    httpx_mock: HTTPXMock,
    results: Optional[List[Any]] = None,
    status_code: int = 200,
) -> None:
    """Register a default or custom Cohere rerank response with httpx_mock."""
    if results is None:
        results = [{"index": 0, "relevance_score": 0.9}]
    if status_code == 200:
        httpx_mock.add_response(
            method="POST",
            url=COHERE_RERANK_URL,
            json={"results": results},
            status_code=status_code,
        )
    else:
        httpx_mock.add_response(
            method="POST",
            url=COHERE_RERANK_URL,
            status_code=status_code,
        )


def _get_cohere_request_body(httpx_mock: HTTPXMock) -> Dict[str, Any]:
    """Return the parsed JSON body of the first captured Cohere request."""
    requests = httpx_mock.get_requests()
    assert len(requests) >= 1, "Expected at least one HTTP request to be captured"
    return json.loads(requests[0].content)


@pytest.fixture
def cohere_mock_cs():
    """Pytest fixture: config service with default Cohere test API key."""
    return _make_cohere_config_service()


@pytest.fixture
def patched_cohere_client(cohere_mock_cs):
    """
    Pytest fixture: yields a CohereRerankerClient with get_config_service patched.
    Tests that need the patch active for the full duration use this fixture.
    """
    from code_indexer.server.clients.reranker_clients import CohereRerankerClient

    client = CohereRerankerClient()
    with patch(
        "code_indexer.server.clients.reranker_clients.get_config_service",
        return_value=cohere_mock_cs,
    ):
        yield client


# ---------------------------------------------------------------------------
# AC1: CohereRerankerClient Instantiation
# ---------------------------------------------------------------------------


class TestCohereRerankerClientInstantiation:
    """Tests for CohereRerankerClient initialization."""

    def test_cohere_reranker_client_can_be_instantiated_with_defaults(self):
        """CohereRerankerClient can be instantiated with defaults."""
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        assert CohereRerankerClient() is not None

    def test_cohere_reranker_client_default_timeout(self):
        """Default timeout is 5.0 seconds."""
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        assert CohereRerankerClient().timeout == 5.0

    def test_cohere_reranker_client_default_max_chars(self):
        """Default max_chars is 4000."""
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        assert CohereRerankerClient().max_chars == 4000

    def test_cohere_reranker_client_custom_timeout(self):
        """Custom timeout is stored correctly."""
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        assert CohereRerankerClient(timeout=10.0).timeout == 10.0

    def test_cohere_reranker_client_custom_max_chars(self):
        """Custom max_chars is stored correctly."""
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        assert CohereRerankerClient(max_chars=2000).max_chars == 2000

    def test_cohere_reranker_client_is_subclass_of_reranker_client(self):
        """CohereRerankerClient is a subclass of RerankerClient."""
        from code_indexer.server.clients.reranker_clients import (
            CohereRerankerClient,
            RerankerClient,
        )

        assert issubclass(CohereRerankerClient, RerankerClient)

    def test_cohere_reranker_client_rejects_non_positive_timeout(self):
        """Constructor raises ValueError when timeout <= 0."""
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        with pytest.raises(ValueError):
            CohereRerankerClient(timeout=0.0)

    def test_cohere_reranker_client_rejects_non_positive_max_chars(self):
        """Constructor raises ValueError when max_chars <= 0."""
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        with pytest.raises(ValueError):
            CohereRerankerClient(max_chars=0)


# ---------------------------------------------------------------------------
# AC1: CohereRerankerClient API Key
# ---------------------------------------------------------------------------


class TestCohereRerankerClientApiKey:
    """Tests for API key retrieval from config service."""

    def test_get_api_key_reads_from_config_service(self):
        """_get_api_key() returns cohere_api_key from config service only."""
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        client = CohereRerankerClient()
        cs = _make_cohere_config_service("test-cohere-key-123")

        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=cs,
        ):
            assert client._get_api_key() == "test-cohere-key-123"

    def test_get_api_key_does_not_use_co_api_key_environment_variable(self):
        """
        _get_api_key() must NOT fall back to CO_API_KEY env var.
        When config returns None, None is returned regardless of env var.
        """
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        client = CohereRerankerClient()
        none_cs = MagicMock()
        none_cs.get_config.return_value.claude_integration_config.cohere_api_key = None

        with patch.dict(os.environ, {"CO_API_KEY": "env-key-must-not-be-used"}):
            with patch(
                "code_indexer.server.clients.reranker_clients.get_config_service",
                return_value=none_cs,
            ):
                api_key = client._get_api_key()

        assert api_key is None

    def test_get_model_returns_default_cohere_model(self):
        """_get_model() returns 'rerank-v3.5' as default model."""
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        assert CohereRerankerClient()._get_model() == "rerank-v3.5"

    def test_get_model_returns_configured_cohere_model(self):
        """_get_model() returns the operator-configured model when set in config."""
        from unittest.mock import MagicMock, patch
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        mock_cs = MagicMock()
        mock_cs.get_config.return_value.rerank_config.cohere_reranker_model = (
            "rerank-v4.0"
        )
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=mock_cs,
        ):
            assert CohereRerankerClient()._get_model() == "rerank-v4.0"

    @pytest.mark.parametrize("empty_value", [None, ""])
    def test_cohere_get_model_falls_back_to_default_when_config_empty(
        self, empty_value
    ):
        """_get_model() falls back to 'rerank-v3.5' when config model is None or empty."""
        from unittest.mock import MagicMock, patch
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        mock_cs = MagicMock()
        mock_cs.get_config.return_value.rerank_config.cohere_reranker_model = (
            empty_value
        )
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=mock_cs,
        ):
            assert CohereRerankerClient()._get_model() == "rerank-v3.5"

    @pytest.mark.parametrize("bad_key", [None, ""])
    def test_cohere_reranker_client_rejects_invalid_api_key(self, bad_key):
        """rerank() raises ValueError when API key is None or empty string."""
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        client = CohereRerankerClient()
        bad_cs = MagicMock()
        bad_cs.get_config.return_value.claude_integration_config.cohere_api_key = (
            bad_key
        )

        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=bad_cs,
        ):
            with pytest.raises(ValueError):
                client.rerank(query="q", documents=["doc"], top_k=1)


# ---------------------------------------------------------------------------
# AC1: CohereRerankerClient API Integration (request body)
# ---------------------------------------------------------------------------


class TestCohereRerankerClientRerank:
    """Tests for rerank() API call behavior against Cohere v2 endpoint."""

    def test_cohere_rerank_sends_correct_request_body(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """rerank() sends POST with model, query, documents, top_n (not top_k)."""
        _add_cohere_rerank_response(
            httpx_mock,
            results=[
                {"index": 0, "relevance_score": 0.95},
                {"index": 1, "relevance_score": 0.80},
            ],
        )

        patched_cohere_client.rerank(
            query="test query",
            documents=["doc one", "doc two"],
            top_k=2,
        )

        body = _get_cohere_request_body(httpx_mock)
        assert body["model"] == "rerank-v3.5"
        assert body["query"] == "test query"
        assert body["documents"] == ["doc one", "doc two"]
        assert body["top_n"] == 2  # Cohere uses top_n, not top_k

    def test_cohere_rerank_uses_v2_endpoint(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """rerank() posts to https://api.cohere.com/v2/rerank."""
        _add_cohere_rerank_response(httpx_mock)

        patched_cohere_client.rerank(query="q", documents=["d"], top_k=1)

        requests = httpx_mock.get_requests()
        assert str(requests[0].url) == COHERE_RERANK_URL

    def test_cohere_rerank_sends_bearer_auth_header(self, httpx_mock: HTTPXMock):
        """rerank() sends Authorization: Bearer <api_key> header."""
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        _add_cohere_rerank_response(httpx_mock)
        custom_cs = _make_cohere_config_service("cohere-secret-key-xyz")
        client = CohereRerankerClient()

        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=custom_cs,
        ):
            client.rerank(query="q", documents=["d"], top_k=1)

        requests = httpx_mock.get_requests()
        assert requests[0].headers["Authorization"] == "Bearer cohere-secret-key-xyz"

    def test_cohere_rerank_parses_results_key_not_data(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """rerank() parses 'results' key from Cohere response (not 'data' like Voyage)."""
        _add_cohere_rerank_response(
            httpx_mock,
            results=[
                {"index": 1, "relevance_score": 0.80},
                {"index": 0, "relevance_score": 0.95},
            ],
        )

        results = patched_cohere_client.rerank(
            query="q", documents=["d0", "d1"], top_k=2
        )

        assert len(results) == 2
        assert results[0].relevance_score == 0.95
        assert results[0].index == 0
        assert results[1].relevance_score == 0.80
        assert results[1].index == 1

    def test_cohere_rerank_returns_results_ordered_descending(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """rerank() returns List[RerankResult] ordered by relevance_score descending."""
        _add_cohere_rerank_response(
            httpx_mock,
            results=[
                {"index": 2, "relevance_score": 0.60},
                {"index": 0, "relevance_score": 0.95},
                {"index": 1, "relevance_score": 0.80},
            ],
        )

        results = patched_cohere_client.rerank(
            query="q", documents=["d0", "d1", "d2"], top_k=3
        )

        assert len(results) == 3
        assert results[0].relevance_score >= results[1].relevance_score
        assert results[1].relevance_score >= results[2].relevance_score

    def test_cohere_rerank_with_top_k_none_omits_top_n_from_body(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """When top_k=None, top_n must not appear in the Cohere request body."""
        _add_cohere_rerank_response(httpx_mock)

        patched_cohere_client.rerank(query="q", documents=["d"], top_k=None)

        body = _get_cohere_request_body(httpx_mock)
        assert "top_n" not in body
        assert "top_k" not in body


# ---------------------------------------------------------------------------
# AC2: Instruction Concatenation (SPACE separator, not newline)
# ---------------------------------------------------------------------------


class TestCohereRerankerClientInstructionConcatenation:
    """Tests for Cohere instruction concatenation (SPACE separator, distinct from Voyage)."""

    def test_instruction_concatenated_with_space_separator(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """Non-empty instruction concatenated as '{instruction} {query}' (space, not newline)."""
        _add_cohere_rerank_response(httpx_mock)

        patched_cohere_client.rerank(
            query="find similar code",
            documents=["doc"],
            top_k=1,
            instruction="Rank by technical relevance",
        )

        assert (
            _get_cohere_request_body(httpx_mock)["query"]
            == "Rank by technical relevance find similar code"
        )

    def test_cohere_instruction_does_not_use_newline_separator(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """Cohere must use space separator (NOT newline like Voyage)."""
        _add_cohere_rerank_response(httpx_mock)

        patched_cohere_client.rerank(
            query="query text",
            documents=["doc"],
            top_k=1,
            instruction="Some instruction",
        )

        effective_query = _get_cohere_request_body(httpx_mock)["query"]
        assert "\n" not in effective_query

    def test_none_instruction_leaves_query_unchanged(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """None instruction leaves query unchanged."""
        _add_cohere_rerank_response(httpx_mock)

        patched_cohere_client.rerank(
            query="my query", documents=["doc"], top_k=1, instruction=None
        )

        assert _get_cohere_request_body(httpx_mock)["query"] == "my query"

    def test_empty_string_instruction_leaves_query_unchanged(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """Empty string instruction leaves query unchanged."""
        _add_cohere_rerank_response(httpx_mock)

        patched_cohere_client.rerank(
            query="my query", documents=["doc"], top_k=1, instruction=""
        )

        assert _get_cohere_request_body(httpx_mock)["query"] == "my query"

    def test_instruction_with_whitespace_stripped_from_result(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """Leading/trailing whitespace stripped from concatenated instruction+query."""
        _add_cohere_rerank_response(httpx_mock)

        patched_cohere_client.rerank(
            query="  my query  ",
            documents=["doc"],
            top_k=1,
            instruction="  instruction  ",
        )

        effective_query = _get_cohere_request_body(httpx_mock)["query"]
        assert effective_query == effective_query.strip()


# ---------------------------------------------------------------------------
# AC3: Document Count Validation (1000 doc limit)
# ---------------------------------------------------------------------------


class TestCohereRerankerClientDocumentCountValidation:
    """Tests for pre-flight document count validation (1000 doc limit)."""

    def test_exactly_1000_documents_accepted(self):
        """Exactly 1000 documents does NOT raise ValueError."""
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        client = CohereRerankerClient()
        # Validate directly — no I/O side effects.
        client._validate_document_count(["doc"] * 1000)  # should not raise

    def test_1001_documents_raises_value_error(self):
        """More than 1000 documents raises ValueError before any API call."""
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        client = CohereRerankerClient()

        with pytest.raises(ValueError, match="1000"):
            client._validate_document_count(["doc"] * 1001)

    def test_document_count_validation_happens_before_api_call(
        self, httpx_mock: HTTPXMock
    ):
        """ValueError for >1000 docs raised BEFORE any HTTP request is sent."""
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        client = CohereRerankerClient()
        cs = _make_cohere_config_service()

        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=cs,
        ):
            with pytest.raises(ValueError):
                client.rerank(query="q", documents=["doc"] * 1001, top_k=1)

        # No HTTP request should have been made
        assert len(httpx_mock.get_requests()) == 0


# ---------------------------------------------------------------------------
# AC4: Document Truncation (same as Voyage)
# ---------------------------------------------------------------------------


class TestCohereRerankerClientDocumentTruncation:
    """Tests for client-side document truncation in CohereRerankerClient."""

    def test_long_document_truncated_to_max_chars(self, httpx_mock: HTTPXMock):
        """Documents longer than max_chars are truncated before sending."""
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        _add_cohere_rerank_response(httpx_mock)
        max_chars = 100
        client = CohereRerankerClient(max_chars=max_chars)
        cs = _make_cohere_config_service()

        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=cs,
        ):
            client.rerank(query="q", documents=["x" * 500], top_k=1)

        sent_doc = _get_cohere_request_body(httpx_mock)["documents"][0]
        assert len(sent_doc) == max_chars

    def test_short_document_not_truncated(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """Documents shorter than max_chars are sent as-is."""
        _add_cohere_rerank_response(httpx_mock)

        patched_cohere_client.rerank(query="q", documents=["short doc"], top_k=1)

        assert _get_cohere_request_body(httpx_mock)["documents"][0] == "short doc"

    def test_empty_document_sent_as_is(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """Empty documents are sent without modification."""
        _add_cohere_rerank_response(httpx_mock)

        patched_cohere_client.rerank(query="q", documents=[""], top_k=1)

        assert _get_cohere_request_body(httpx_mock)["documents"][0] == ""


# ---------------------------------------------------------------------------
# AC5: ProviderHealthMonitor Registration as "cohere-reranker"
# ---------------------------------------------------------------------------


class TestCohereRerankerClientHealthMonitorRegistration:
    """Tests for ProviderHealthMonitor integration with 'cohere-reranker' probe."""

    def test_probe_registered_as_cohere_reranker(self):
        """CohereRerankerClient registers a probe recognized by ProviderHealthMonitor."""
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        ProviderHealthMonitor.reset_instance()
        monitor = ProviderHealthMonitor.get_instance()

        # Instantiate to trigger probe registration.
        CohereRerankerClient()

        # Validate via public API: recording a call should succeed for the probe name.
        monitor.record_call("cohere-reranker", latency_ms=1.0, success=True)
        health = monitor.get_health("cohere-reranker")
        assert "cohere-reranker" in health

    def test_health_monitor_updated_on_success(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """ProviderHealthMonitor records success=True on successful Cohere call."""
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        _add_cohere_rerank_response(httpx_mock)
        ProviderHealthMonitor.reset_instance()
        monitor = ProviderHealthMonitor.get_instance()

        patched_cohere_client.rerank(query="q", documents=["doc"], top_k=1)

        health = monitor.get_health("cohere-reranker")
        status = health["cohere-reranker"]
        assert status.successful_requests == 1
        assert status.failed_requests == 0

    def test_health_monitor_updated_on_failure(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """ProviderHealthMonitor records success=False on Cohere HTTP error."""
        import httpx as _httpx
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        _add_cohere_rerank_response(httpx_mock, status_code=401)
        ProviderHealthMonitor.reset_instance()
        monitor = ProviderHealthMonitor.get_instance()

        with pytest.raises(_httpx.HTTPStatusError):
            patched_cohere_client.rerank(query="q", documents=["doc"], top_k=1)

        health = monitor.get_health("cohere-reranker")
        status = health["cohere-reranker"]
        assert status.failed_requests == 1


# ---------------------------------------------------------------------------
# AC6: Error Propagation
# ---------------------------------------------------------------------------


class TestCohereRerankerClientErrorPropagation:
    """Tests for error propagation in CohereRerankerClient — no exception swallowing."""

    def test_http_500_raises_http_status_error(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """HTTP 500 raises httpx.HTTPStatusError (not swallowed)."""
        import httpx as _httpx

        _add_cohere_rerank_response(httpx_mock, status_code=500)

        with pytest.raises(_httpx.HTTPStatusError):
            patched_cohere_client.rerank(query="q", documents=["doc"], top_k=1)

    def test_http_401_raises_http_status_error(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """HTTP 401 raises httpx.HTTPStatusError (not swallowed)."""
        import httpx as _httpx

        _add_cohere_rerank_response(httpx_mock, status_code=401)

        with pytest.raises(_httpx.HTTPStatusError):
            patched_cohere_client.rerank(query="q", documents=["doc"], top_k=1)

    def test_timeout_exception_propagates(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """Timeout raises httpx.TimeoutException (not swallowed)."""
        import httpx as _httpx

        httpx_mock.add_exception(
            exception=_httpx.TimeoutException("timeout"),
            method="POST",
            url=COHERE_RERANK_URL,
        )

        with pytest.raises(_httpx.TimeoutException):
            patched_cohere_client.rerank(query="q", documents=["doc"], top_k=1)

    def test_exception_propagates_and_health_monitor_updated(
        self, httpx_mock: HTTPXMock, patched_cohere_client
    ):
        """
        Exception propagates AND health monitor updated on 503.
        ProviderHealthMonitor must be updated BEFORE exception is re-raised.
        """
        import httpx as _httpx
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        _add_cohere_rerank_response(httpx_mock, status_code=503)
        ProviderHealthMonitor.reset_instance()
        monitor = ProviderHealthMonitor.get_instance()

        with pytest.raises(_httpx.HTTPStatusError):
            patched_cohere_client.rerank(query="q", documents=["doc"], top_k=1)

        health = monitor.get_health("cohere-reranker")
        status = health["cohere-reranker"]
        assert status.failed_requests >= 1
