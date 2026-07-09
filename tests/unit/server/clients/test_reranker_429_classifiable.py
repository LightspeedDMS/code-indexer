"""Reranker client 429s must propagate classifiable (Story #1079 Phase A).

Both VoyageRerankerClient and CohereRerankerClient re-raise the underlying
``httpx.HTTPStatusError`` intact on HTTP errors (no RuntimeError wrapping), so a
429 is already classifiable by ``is_rate_limited``. These tests verify and guard
that contract so a future refactor cannot silently mask a reranker rate-limit.

Uses pytest_httpx (real HTTP transport interception) per project conventions.
"""

from typing import Generator, Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest
from pytest_httpx import HTTPXMock

from code_indexer.server.clients.reranker_clients import (
    COHERE_RERANK_URL,
    VOYAGE_RERANK_URL,
    CohereRerankerClient,
    VoyageRerankerClient,
)
from code_indexer.services.provider_backoff import (
    get_http_status_error,
    is_rate_limited,
)


@pytest.fixture(autouse=True)
def reset_health_monitor() -> Generator[None, None, None]:
    """Reset the ProviderHealthMonitor singleton before and after every test.

    These tests deliberately drive failure paths (429/500) which record
    success=False calls on the shared singleton; without isolation the
    accumulated sin-bin state would leak into sibling reranker tests.
    """
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    ProviderHealthMonitor.reset_instance()
    yield
    ProviderHealthMonitor.reset_instance()


def _voyage_config_service(api_key: str = "test-voyage-key") -> MagicMock:
    mock_config = MagicMock()
    mock_config.claude_integration_config.voyageai_api_key = api_key
    mock_config.rerank_config.voyage_reranker_model = None
    mock_cs = MagicMock()
    mock_cs.get_config.return_value = mock_config
    return mock_cs


def _cohere_config_service(api_key: str = "test-cohere-key") -> MagicMock:
    mock_config = MagicMock()
    mock_config.claude_integration_config.cohere_api_key = api_key
    mock_config.rerank_config.cohere_reranker_model = None
    mock_cs = MagicMock()
    mock_cs.get_config.return_value = mock_config
    return mock_cs


def _add_status(
    httpx_mock: HTTPXMock, url: str, status_code: int, retry_after: Optional[str] = None
) -> None:
    headers = {"retry-after": retry_after} if retry_after is not None else None
    httpx_mock.add_response(
        method="POST", url=url, status_code=status_code, headers=headers
    )


class TestVoyageReranker429Classifiable:
    def test_429_propagates_classifiable(self, httpx_mock: HTTPXMock):
        _add_status(httpx_mock, VOYAGE_RERANK_URL, 429)
        client = VoyageRerankerClient()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=_voyage_config_service(),
        ):
            with pytest.raises(Exception) as exc_info:
                client.rerank(query="q", documents=["a", "b"], top_k=2)
        assert is_rate_limited(exc_info.value) is True

    def test_429_preserves_retry_after(self, httpx_mock: HTTPXMock):
        _add_status(httpx_mock, VOYAGE_RERANK_URL, 429, retry_after="6.0")
        client = VoyageRerankerClient()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=_voyage_config_service(),
        ):
            with pytest.raises(Exception) as exc_info:
                client.rerank(query="q", documents=["a", "b"], top_k=2)
        http_err = get_http_status_error(exc_info.value)
        assert http_err is not None
        assert http_err.response.headers.get("retry-after") == "6.0"

    def test_500_not_classifiable(self, httpx_mock: HTTPXMock):
        _add_status(httpx_mock, VOYAGE_RERANK_URL, 500)
        client = VoyageRerankerClient()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=_voyage_config_service(),
        ):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                client.rerank(query="q", documents=["a", "b"], top_k=2)
        assert is_rate_limited(exc_info.value) is False


class TestCohereReranker429Classifiable:
    def test_429_propagates_classifiable(self, httpx_mock: HTTPXMock):
        _add_status(httpx_mock, COHERE_RERANK_URL, 429)
        client = CohereRerankerClient()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=_cohere_config_service(),
        ):
            with pytest.raises(Exception) as exc_info:
                client.rerank(query="q", documents=["a", "b"], top_k=2)
        assert is_rate_limited(exc_info.value) is True

    def test_429_preserves_retry_after(self, httpx_mock: HTTPXMock):
        _add_status(httpx_mock, COHERE_RERANK_URL, 429, retry_after="2.0")
        client = CohereRerankerClient()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=_cohere_config_service(),
        ):
            with pytest.raises(Exception) as exc_info:
                client.rerank(query="q", documents=["a", "b"], top_k=2)
        http_err = get_http_status_error(exc_info.value)
        assert http_err is not None
        assert http_err.response.headers.get("retry-after") == "2.0"

    def test_500_not_classifiable(self, httpx_mock: HTTPXMock):
        _add_status(httpx_mock, COHERE_RERANK_URL, 500)
        client = CohereRerankerClient()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=_cohere_config_service(),
        ):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                client.rerank(query="q", documents=["a", "b"], top_k=2)
        assert is_rate_limited(exc_info.value) is False


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
