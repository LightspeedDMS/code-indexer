"""
Unit tests for env-var fallback in VoyageRerankerClient and CohereRerankerClient.

Bug #928: CLI reranker silently degrades to base order because _get_api_key()
consults get_config_service() (server-only global) instead of reading
VOYAGE_API_KEY / CO_API_KEY from env when the global config has no key.

Fix (Option B): env-var fallback in _get_api_key() of both client classes.

Test matrix (7 tests):
  1. Voyage falls back to VOYAGE_API_KEY env var when config returns None
  2. Cohere falls back to CO_API_KEY env var when config returns None
  3. Voyage config key wins over env var when both present (server-mode regression guard)
  4. Cohere config key wins over env var when both present (server-mode regression guard)
  5. Voyage raises ValueError mentioning VOYAGE_API_KEY and Web UI when neither source set
  6. Cohere raises ValueError mentioning CO_API_KEY and Web UI when neither source set
  7. Voyage negative assertion: old server-only fragment absent from new error message
"""

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_config_service(
    voyageai_api_key=None,
    cohere_api_key=None,
) -> MagicMock:
    """Return a MagicMock config service with specified API keys."""
    mock_config = MagicMock()
    mock_config.claude_integration_config.voyageai_api_key = voyageai_api_key
    mock_config.claude_integration_config.cohere_api_key = cohere_api_key
    mock_config.rerank_config.voyage_reranker_model = None
    mock_config.rerank_config.cohere_reranker_model = None
    mock_cs = MagicMock()
    mock_cs.get_config.return_value = mock_config
    return mock_cs


# ---------------------------------------------------------------------------
# VoyageRerankerClient env-var fallback tests
# ---------------------------------------------------------------------------


class TestVoyageGetApiKeyEnvFallback:
    """Tests for VoyageRerankerClient._get_api_key() env-var fallback (Bug #928)."""

    def test_voyage_get_api_key_falls_back_to_env_when_config_missing(
        self, monkeypatch
    ):
        """
        Test 1: When global config returns None for voyageai_api_key,
        _get_api_key() must fall back to VOYAGE_API_KEY env var.
        """
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        monkeypatch.setenv("VOYAGE_API_KEY", "env-voyage-key-abc")
        config_service_no_key = _make_config_service(voyageai_api_key=None)

        client = VoyageRerankerClient()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=config_service_no_key,
        ):
            result = client._get_api_key()

        assert result == "env-voyage-key-abc"

    def test_voyage_config_wins_over_env_when_both_set(self, monkeypatch):
        """
        Test 3: When config has a key AND VOYAGE_API_KEY env var is also set,
        the config key must win (server-mode regression guard).
        """
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        monkeypatch.setenv("VOYAGE_API_KEY", "env-voyage-key")
        config_service_with_key = _make_config_service(
            voyageai_api_key="config-voyage-key"
        )

        client = VoyageRerankerClient()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=config_service_with_key,
        ):
            result = client._get_api_key()

        assert result == "config-voyage-key"

    def test_voyage_raises_when_neither_config_nor_env(self, monkeypatch):
        """
        Test 5: When both config and VOYAGE_API_KEY env var are absent,
        _post() raises ValueError whose message mentions VOYAGE_API_KEY and Web UI.
        """
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        config_service_no_key = _make_config_service(voyageai_api_key=None)

        client = VoyageRerankerClient()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=config_service_no_key,
        ):
            with pytest.raises(ValueError) as exc_info:
                client._post(
                    {"model": "rerank-2.5", "query": "test", "documents": ["doc"]}
                )

        error_msg = str(exc_info.value)
        assert "VOYAGE_API_KEY" in error_msg, (
            f"Error must mention VOYAGE_API_KEY for CLI users, got: {error_msg!r}"
        )
        assert "web ui" in error_msg.lower(), (
            f"Error must also mention Web UI for server users, got: {error_msg!r}"
        )

    def test_voyage_error_message_no_longer_says_only_web_ui(self, monkeypatch):
        """
        Test 7: Negative assertion — the old server-only fragment
        'Configure it via the server Web UI under API Keys' must not appear
        verbatim in the new error message.  The new message must include CLI
        guidance so users are not sent to a Web UI that does not exist in CLI mode.
        """
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        config_service_no_key = _make_config_service(voyageai_api_key=None)

        client = VoyageRerankerClient()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=config_service_no_key,
        ):
            with pytest.raises(ValueError) as exc_info:
                client._post(
                    {"model": "rerank-2.5", "query": "test", "documents": ["doc"]}
                )

        error_msg = str(exc_info.value)
        old_server_only_fragment = "Configure it via the server Web UI under API Keys"
        assert old_server_only_fragment not in error_msg, (
            f"Old server-only guidance fragment must be removed; "
            f"got message still containing it: {error_msg!r}"
        )


# ---------------------------------------------------------------------------
# CohereRerankerClient env-var fallback tests
# ---------------------------------------------------------------------------


class TestCohereGetApiKeyEnvFallback:
    """Tests for CohereRerankerClient._get_api_key() env-var fallback (Bug #928)."""

    def test_cohere_get_api_key_falls_back_to_env_when_config_missing(
        self, monkeypatch
    ):
        """
        Test 2: When global config returns None for cohere_api_key,
        _get_api_key() must fall back to CO_API_KEY env var.
        """
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        monkeypatch.setenv("CO_API_KEY", "env-cohere-key-xyz")
        config_service_no_key = _make_config_service(cohere_api_key=None)

        client = CohereRerankerClient()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=config_service_no_key,
        ):
            result = client._get_api_key()

        assert result == "env-cohere-key-xyz"

    def test_cohere_config_wins_over_env_when_both_set(self, monkeypatch):
        """
        Test 4: When config has a key AND CO_API_KEY env var is also set,
        the config key must win (server-mode regression guard).
        """
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        monkeypatch.setenv("CO_API_KEY", "env-cohere-key")
        config_service_with_key = _make_config_service(
            cohere_api_key="config-cohere-key"
        )

        client = CohereRerankerClient()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=config_service_with_key,
        ):
            result = client._get_api_key()

        assert result == "config-cohere-key"

    def test_cohere_raises_when_neither_config_nor_env(self, monkeypatch):
        """
        Test 6: When both config and CO_API_KEY env var are absent,
        _post() raises ValueError whose message mentions CO_API_KEY and Web UI.
        """
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        monkeypatch.delenv("CO_API_KEY", raising=False)
        config_service_no_key = _make_config_service(cohere_api_key=None)

        client = CohereRerankerClient()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=config_service_no_key,
        ):
            with pytest.raises(ValueError) as exc_info:
                client._post(
                    {"model": "rerank-v3.5", "query": "test", "documents": ["doc"]}
                )

        error_msg = str(exc_info.value)
        assert "CO_API_KEY" in error_msg, (
            f"Error must mention CO_API_KEY for CLI users, got: {error_msg!r}"
        )
        assert "web ui" in error_msg.lower(), (
            f"Error must also mention Web UI for server users, got: {error_msg!r}"
        )
