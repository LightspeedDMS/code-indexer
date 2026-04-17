"""
Tests for Bug #740: HTTP error response body captured and logged.

Verifies that VoyageRerankerClient and CohereRerankerClient log the HTTP
response body when the API returns a 4xx error, to aid debugging.

Uses pytest_httpx for real HTTP mocking at the transport layer.
"""

import logging
from typing import Tuple
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


# ---------------------------------------------------------------------------
# Client factory helpers — each returns (client, config_service) ready to use
# ---------------------------------------------------------------------------


def _voyage_client_factory() -> Tuple[VoyageRerankerClient, MagicMock]:
    mock_config = MagicMock()
    mock_config.claude_integration_config.voyageai_api_key = "test-voyage-key"
    mock_config.rerank_config.voyage_reranker_model = "rerank-2.5"
    cs = MagicMock()
    cs.get_config.return_value = mock_config
    return VoyageRerankerClient(), cs


def _cohere_client_factory() -> Tuple[CohereRerankerClient, MagicMock]:
    mock_config = MagicMock()
    mock_config.claude_integration_config.cohere_api_key = "test-cohere-key"
    mock_config.rerank_config.cohere_reranker_model = "rerank-v3.5"
    cs = MagicMock()
    cs.get_config.return_value = mock_config
    return CohereRerankerClient(), cs


# ---------------------------------------------------------------------------
# Bug #740: parametrized over both providers and multiple 4xx codes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_factory,url,status_code,response_body,expected_fragment",
    [
        # Voyage — 401 unauthorized
        (
            _voyage_client_factory,
            VOYAGE_RERANK_URL,
            401,
            '{"detail": "Invalid API key"}',
            "Invalid API key",
        ),
        # Voyage — 429 rate limit
        (
            _voyage_client_factory,
            VOYAGE_RERANK_URL,
            429,
            '{"detail": "Rate limit exceeded"}',
            "Rate limit exceeded",
        ),
        # Voyage — 422 bad request
        (
            _voyage_client_factory,
            VOYAGE_RERANK_URL,
            422,
            '{"detail": "Bad request payload"}',
            "Bad request payload",
        ),
        # Cohere — 401 unauthorized
        (
            _cohere_client_factory,
            COHERE_RERANK_URL,
            401,
            '{"message": "invalid api token"}',
            "invalid api token",
        ),
        # Cohere — 422 validation error
        (
            _cohere_client_factory,
            COHERE_RERANK_URL,
            422,
            '{"message": "invalid model name"}',
            "invalid model name",
        ),
        # Cohere — 429 rate limit
        (
            _cohere_client_factory,
            COHERE_RERANK_URL,
            429,
            '{"message": "too many requests"}',
            "too many requests",
        ),
    ],
)
def test_reranker_http_4xx_logs_response_body_and_status_code(
    httpx_mock: HTTPXMock,
    caplog,
    client_factory,
    url: str,
    status_code: int,
    response_body: str,
    expected_fragment: str,
):
    """Bug #740: both Voyage and Cohere clients must log HTTP response body and
    status code in a WARNING when the API returns a 4xx error."""
    httpx_mock.add_response(
        method="POST",
        url=url,
        status_code=status_code,
        text=response_body,
    )

    client, cs = client_factory()

    with patch(
        "code_indexer.server.clients.reranker_clients.get_config_service",
        return_value=cs,
    ):
        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.server.clients.reranker_clients",
        ):
            with pytest.raises(httpx.HTTPStatusError):
                client.rerank(query="test query", documents=["doc1"])

    warning_messages = [
        r.message for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert any(expected_fragment in msg for msg in warning_messages), (
        f"Expected '{expected_fragment}' in warning log for HTTP {status_code}. "
        f"Warning messages: {warning_messages}"
    )
    assert any(str(status_code) in msg for msg in warning_messages), (
        f"Expected status code '{status_code}' in warning log. "
        f"Warning messages: {warning_messages}"
    )
