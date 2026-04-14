"""Tests for Bug #678: sin-bin checking and timeout updates in reranker clients."""

import time
from contextlib import contextmanager
from typing import Generator, Iterator
from unittest.mock import MagicMock, patch

import httpx
import pytest

# Named constant for sin-bin duration used in tests (seconds).
SINBIN_DURATION_SECONDS = 60


# ---------------------------------------------------------------------------
# Internal helpers (not fixtures — called by fixtures)
# ---------------------------------------------------------------------------


def _fake_voyage_response(scores: list) -> MagicMock:
    """Build a fake httpx.Response for Voyage rerank API."""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = {
        "data": [{"index": i, "relevance_score": s} for i, s in enumerate(scores)]
    }
    resp.raise_for_status = MagicMock()
    return resp


def _fake_cohere_response(scores: list) -> MagicMock:
    """Build a fake httpx.Response for Cohere rerank API."""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = {
        "results": [{"index": i, "relevance_score": s} for i, s in enumerate(scores)]
    }
    resp.raise_for_status = MagicMock()
    return resp


@contextmanager
def _mock_reranker_call(
    api_key_attr: str,
    response: object = None,
    side_effect: object = None,
) -> Iterator[MagicMock]:
    """Patch get_config_service and httpx.Client for a reranker call.

    Args:
        api_key_attr: Config attribute name for the API key
            (e.g. "voyageai_api_key" or "cohere_api_key").
        response: Return value for httpx_client.post (used when side_effect is None).
        side_effect: Exception or callable to raise/call from httpx_client.post.
    """
    mock_cfg = MagicMock()
    setattr(mock_cfg.claude_integration_config, api_key_attr, "test-key")
    mock_cfg.rerank_config = None

    mock_http_client = MagicMock()
    mock_http_client.__enter__ = MagicMock(return_value=mock_http_client)
    mock_http_client.__exit__ = MagicMock(return_value=False)
    if side_effect is not None:
        mock_http_client.post.side_effect = side_effect
    else:
        mock_http_client.post.return_value = response

    with patch(
        "code_indexer.server.clients.reranker_clients.get_config_service"
    ) as mock_cfg_svc:
        mock_cfg_svc.return_value.get_config.return_value = mock_cfg
        with patch("httpx.Client", return_value=mock_http_client):
            yield mock_http_client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_health_monitor() -> Generator[None, None, None]:
    """Reset ProviderHealthMonitor singleton before and after every test."""
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    ProviderHealthMonitor.reset_instance()
    yield
    ProviderHealthMonitor.reset_instance()


@pytest.fixture()
def mock_voyage_http() -> Iterator[MagicMock]:
    """Fixture: patch Voyage config + httpx for a successful single-result call."""
    with _mock_reranker_call(
        "voyageai_api_key", response=_fake_voyage_response([0.9])
    ) as m:
        yield m


@pytest.fixture()
def mock_cohere_http() -> Iterator[MagicMock]:
    """Fixture: patch Cohere config + httpx for a successful single-result call."""
    with _mock_reranker_call(
        "cohere_api_key", response=_fake_cohere_response([0.8])
    ) as m:
        yield m


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRerankerSinbinnedException:
    def test_exception_contains_provider_name(self):
        from code_indexer.server.clients.reranker_clients import (
            RerankerSinbinnedException,
        )

        exc = RerankerSinbinnedException("voyage-reranker")
        assert exc.provider == "voyage-reranker"
        assert "voyage-reranker" in str(exc)


class TestVoyageRerankerSinbin:
    def test_default_timeout_is_15(self):
        """Bug #678: Default reranker timeout updated from 5.0 to 15.0."""
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        client = VoyageRerankerClient()
        assert client.timeout == 15.0

    def test_raises_if_sinbinned(self):
        from code_indexer.server.clients.reranker_clients import (
            _PROVIDER_NAME,
            RerankerSinbinnedException,
            VoyageRerankerClient,
        )
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        monitor = ProviderHealthMonitor.get_instance()
        monitor._sinbin_until[_PROVIDER_NAME] = (
            time.monotonic() + SINBIN_DURATION_SECONDS
        )
        client = VoyageRerankerClient()
        with pytest.raises(RerankerSinbinnedException) as exc_info:
            client.rerank("test query", ["doc1", "doc2"])
        assert exc_info.value.provider == _PROVIDER_NAME

    def test_proceeds_when_not_sinbinned(self, mock_voyage_http: MagicMock):
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        client = VoyageRerankerClient()
        results = client.rerank("test query", ["doc1"])
        assert len(results) == 1


class TestCohereRerankerSinbin:
    def test_default_timeout_is_15(self):
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        client = CohereRerankerClient()
        assert client.timeout == 15.0

    def test_raises_if_sinbinned(self):
        from code_indexer.server.clients.reranker_clients import (
            _COHERE_PROVIDER_NAME,
            CohereRerankerClient,
            RerankerSinbinnedException,
        )
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        monitor = ProviderHealthMonitor.get_instance()
        monitor._sinbin_until[_COHERE_PROVIDER_NAME] = (
            time.monotonic() + SINBIN_DURATION_SECONDS
        )
        client = CohereRerankerClient()
        with pytest.raises(RerankerSinbinnedException) as exc_info:
            client.rerank("test query", ["doc1", "doc2"])
        assert exc_info.value.provider == _COHERE_PROVIDER_NAME

    def test_proceeds_when_not_sinbinned(self, mock_cohere_http: MagicMock):
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        client = CohereRerankerClient()
        results = client.rerank("test query", ["doc1"])
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Parametrized health recording tests
# ---------------------------------------------------------------------------

_PROVIDER_PARAMS = [
    pytest.param(
        ("voyage", "voyageai_api_key", "voyage-reranker"),
        id="voyage",
    ),
    pytest.param(
        ("cohere", "cohere_api_key", "cohere-reranker"),
        id="cohere",
    ),
]


def _make_client(provider_short: str):
    """Instantiate the reranker client for the given short provider name."""
    from code_indexer.server.clients.reranker_clients import (
        CohereRerankerClient,
        VoyageRerankerClient,
    )

    return (
        VoyageRerankerClient() if provider_short == "voyage" else CohereRerankerClient()
    )


def _make_success_response(provider_short: str) -> MagicMock:
    return (
        _fake_voyage_response([0.9])
        if provider_short == "voyage"
        else _fake_cohere_response([0.8])
    )


class TestRerankerHealthRecording:
    @pytest.mark.parametrize("params", _PROVIDER_PARAMS)
    def test_success_records_health_metric(self, params):
        provider_short, api_key_attr, health_key = params
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        client = _make_client(provider_short)
        with _mock_reranker_call(
            api_key_attr, response=_make_success_response(provider_short)
        ):
            client.rerank("test query", ["doc1"])
        status = (
            ProviderHealthMonitor.get_instance().get_health(health_key).get(health_key)
        )
        assert status is not None
        assert status.successful_requests >= 1

    @pytest.mark.parametrize("params", _PROVIDER_PARAMS)
    def test_failure_records_health_metric(self, params):
        provider_short, api_key_attr, health_key = params
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        client = _make_client(provider_short)
        with _mock_reranker_call(
            api_key_attr, side_effect=httpx.TimeoutException("timeout")
        ):
            with pytest.raises(httpx.TimeoutException):
                client.rerank("test query", ["doc1"])
        status = (
            ProviderHealthMonitor.get_instance().get_health(health_key).get(health_key)
        )
        assert status is not None
        assert status.failed_requests >= 1
