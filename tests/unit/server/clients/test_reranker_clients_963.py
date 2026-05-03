"""
Unit tests for Bug #963 fix: empty document strings replaced with single space.

Bug #963: Voyage and Cohere rerank clients forward empty document strings to
their APIs. Voyage returns HTTP 400: "Input cannot contain empty strings".

Fix: Both _truncate_documents methods replace empty strings with " " (single
space), preserving index alignment without triggering API rejections.
"""

import json
from unittest.mock import MagicMock, patch

from pytest_httpx import HTTPXMock

VOYAGE_RERANK_URL = "https://api.voyageai.com/v1/rerank"
COHERE_RERANK_URL = "https://api.cohere.com/v2/rerank"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_voyage_config_service(api_key: str = "test-voyage-key") -> MagicMock:
    """Return a MagicMock config service with voyageai_api_key set."""
    mock_config = MagicMock()
    mock_config.claude_integration_config.voyageai_api_key = api_key
    mock_config.rerank_config.voyage_reranker_model = None
    mock_cs = MagicMock()
    mock_cs.get_config.return_value = mock_config
    return mock_cs


def _make_cohere_config_service(api_key: str = "test-cohere-key") -> MagicMock:
    """Return a MagicMock config service with cohere_api_key set."""
    mock_config = MagicMock()
    mock_config.claude_integration_config.cohere_api_key = api_key
    mock_config.rerank_config.cohere_reranker_model = None
    mock_cs = MagicMock()
    mock_cs.get_config.return_value = mock_config
    return mock_cs


# ---------------------------------------------------------------------------
# Voyage _truncate_documents — unit tests (no HTTP)
# ---------------------------------------------------------------------------


class TestVoyageTruncateDocuments:
    """Unit tests for VoyageRerankerClient._truncate_documents (Bug #963)."""

    def _make_client(self, max_chars: int = 4000):
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        return VoyageRerankerClient(max_chars=max_chars)

    def test_voyage_truncate_replaces_empty_with_space(self):
        """Empty string in documents list is replaced with ' ' (single space)."""
        client = self._make_client()
        result = client._truncate_documents(["hello", "", "world"])
        assert result == ["hello", " ", "world"]

    def test_voyage_truncate_all_empty_docs(self):
        """All-empty document list is replaced with all single spaces."""
        client = self._make_client()
        result = client._truncate_documents(["", "", ""])
        assert result == [" ", " ", " "]

    def test_voyage_truncate_non_empty_docs_still_truncated(self):
        """Non-empty docs exceeding max_chars are still truncated (regression guard)."""
        client = self._make_client(max_chars=5)
        result = client._truncate_documents(["abcdefgh", "xy"])
        assert result == ["abcde", "xy"]

    def test_voyage_truncate_mixed_empty_and_long(self):
        """Mix of empty, short, and long docs: empties become ' ', longs get cut."""
        client = self._make_client(max_chars=3)
        result = client._truncate_documents(["abc", "", "toolong"])
        assert result == ["abc", " ", "too"]

    def test_voyage_truncate_preserves_whitespace_only_docs(self):
        """Docs that contain only whitespace (but are non-empty) are NOT replaced."""
        client = self._make_client()
        result = client._truncate_documents(["   ", "\t", "\n"])
        assert result == ["   ", "\t", "\n"]

    def test_voyage_truncate_single_empty_doc(self):
        """Single empty doc list returns single-space list."""
        client = self._make_client()
        result = client._truncate_documents([""])
        assert result == [" "]

    def test_voyage_truncate_no_empty_docs_unchanged(self):
        """Documents with no empty strings pass through unchanged (no regressions)."""
        client = self._make_client()
        result = client._truncate_documents(["hello", "world"])
        assert result == ["hello", "world"]


# ---------------------------------------------------------------------------
# Cohere _truncate_documents — unit tests (no HTTP)
# ---------------------------------------------------------------------------


class TestCohereTruncateDocuments:
    """Unit tests for CohereRerankerClient._truncate_documents (Bug #963)."""

    def _make_client(self, max_chars: int = 4000):
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        return CohereRerankerClient(max_chars=max_chars)

    def test_cohere_truncate_replaces_empty_with_space(self):
        """Empty string in documents list is replaced with ' ' (single space)."""
        client = self._make_client()
        result = client._truncate_documents(["hello", "", "world"])
        assert result == ["hello", " ", "world"]

    def test_cohere_truncate_all_empty_docs(self):
        """All-empty document list is replaced with all single spaces."""
        client = self._make_client()
        result = client._truncate_documents(["", "", ""])
        assert result == [" ", " ", " "]

    def test_cohere_truncate_non_empty_docs_still_truncated(self):
        """Non-empty docs exceeding max_chars are still truncated (regression guard)."""
        client = self._make_client(max_chars=5)
        result = client._truncate_documents(["abcdefgh", "xy"])
        assert result == ["abcde", "xy"]

    def test_cohere_truncate_mixed_empty_and_long(self):
        """Mix of empty, short, and long docs: empties become ' ', longs get cut."""
        client = self._make_client(max_chars=3)
        result = client._truncate_documents(["abc", "", "toolong"])
        assert result == ["abc", " ", "too"]

    def test_cohere_truncate_preserves_whitespace_only_docs(self):
        """Docs that contain only whitespace (but are non-empty) are NOT replaced."""
        client = self._make_client()
        result = client._truncate_documents(["   ", "\t", "\n"])
        assert result == ["   ", "\t", "\n"]

    def test_cohere_truncate_single_empty_doc(self):
        """Single empty doc list returns single-space list."""
        client = self._make_client()
        result = client._truncate_documents([""])
        assert result == [" "]

    def test_cohere_truncate_no_empty_docs_unchanged(self):
        """Documents with no empty strings pass through unchanged (no regressions)."""
        client = self._make_client()
        result = client._truncate_documents(["hello", "world"])
        assert result == ["hello", "world"]


# ---------------------------------------------------------------------------
# Voyage rerank() integration — does not raise on empty doc
# ---------------------------------------------------------------------------


class TestVoyageRerankWithEmptyDocs:
    """Integration tests: Voyage rerank() must not raise when docs contain ''."""

    def test_voyage_rerank_does_not_raise_on_empty_doc(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """
        rerank() with an empty document string must succeed without raising.

        The empty string is replaced with ' ' before the API call, so no
        HTTP 400 from Voyage.
        """
        httpx_mock.add_response(
            method="POST",
            url=VOYAGE_RERANK_URL,
            json={"data": [{"index": 0, "relevance_score": 0.9}]},
            status_code=200,
        )
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        client = VoyageRerankerClient()
        mock_cs = _make_voyage_config_service()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=mock_cs,
        ):
            # Should NOT raise — previously would send "" to API and get HTTP 400
            results = client.rerank("my query", ["valid doc", ""])
        assert len(results) == 1

    def test_voyage_rerank_sends_space_not_empty_string(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """
        The actual HTTP request body sent to Voyage must contain ' ' not ''.
        """
        httpx_mock.add_response(
            method="POST",
            url=VOYAGE_RERANK_URL,
            json={"data": [{"index": 0, "relevance_score": 0.8}]},
            status_code=200,
        )
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        client = VoyageRerankerClient()
        mock_cs = _make_voyage_config_service()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=mock_cs,
        ):
            client.rerank("query", ["doc", ""])

        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        body = json.loads(requests[0].content)
        assert "" not in body["documents"], "Empty string must not appear in request"
        assert " " in body["documents"], "Single space placeholder must be in request"


# ---------------------------------------------------------------------------
# Cohere rerank() integration — does not raise on empty doc
# ---------------------------------------------------------------------------


class TestCohereRerankWithEmptyDocs:
    """Integration tests: Cohere rerank() must not raise when docs contain ''."""

    def test_cohere_rerank_does_not_raise_on_empty_doc(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """
        rerank() with an empty document string must succeed without raising.
        """
        httpx_mock.add_response(
            method="POST",
            url=COHERE_RERANK_URL,
            json={"results": [{"index": 0, "relevance_score": 0.9}]},
            status_code=200,
        )
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        client = CohereRerankerClient()
        mock_cs = _make_cohere_config_service()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=mock_cs,
        ):
            results = client.rerank("my query", ["valid doc", ""])
        assert len(results) == 1

    def test_cohere_rerank_sends_space_not_empty_string(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """
        The actual HTTP request body sent to Cohere must contain ' ' not ''.
        """
        httpx_mock.add_response(
            method="POST",
            url=COHERE_RERANK_URL,
            json={"results": [{"index": 0, "relevance_score": 0.8}]},
            status_code=200,
        )
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        client = CohereRerankerClient()
        mock_cs = _make_cohere_config_service()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=mock_cs,
        ):
            client.rerank("query", ["doc", ""])

        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        body = json.loads(requests[0].content)
        assert "" not in body["documents"], "Empty string must not appear in request"
        assert " " in body["documents"], "Single space placeholder must be in request"


# ---------------------------------------------------------------------------
# Index alignment preservation
# ---------------------------------------------------------------------------


class TestIndexAlignmentPreservation:
    """Index alignment must be preserved when empty docs are replaced with ' '."""

    def test_voyage_index_alignment_preserved_when_empty_doc(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """
        When doc at index 2 is empty (replaced with ' '), the RerankResult from
        the API response correctly maps back to original document list position.

        The placeholder is at index 2 in the sent list; the API returns index=2
        in its response. The caller receives RerankResult(index=2) which correctly
        refers to position 2 in the original documents list.
        """
        # API returns result for index=2 (the space placeholder)
        httpx_mock.add_response(
            method="POST",
            url=VOYAGE_RERANK_URL,
            json={
                "data": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 2, "relevance_score": 0.3},
                ]
            },
            status_code=200,
        )
        from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

        client = VoyageRerankerClient()
        mock_cs = _make_voyage_config_service()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=mock_cs,
        ):
            results = client.rerank("query", ["first doc", "second doc", ""])

        # Results ordered by score descending
        assert results[0].index == 0
        assert results[1].index == 2  # maps to original position 2 (was empty)

    def test_cohere_index_alignment_preserved_when_empty_doc(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """
        Same index alignment test for Cohere.
        """
        httpx_mock.add_response(
            method="POST",
            url=COHERE_RERANK_URL,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 2, "relevance_score": 0.3},
                ]
            },
            status_code=200,
        )
        from code_indexer.server.clients.reranker_clients import CohereRerankerClient

        client = CohereRerankerClient()
        mock_cs = _make_cohere_config_service()
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=mock_cs,
        ):
            results = client.rerank("query", ["first doc", "second doc", ""])

        assert results[0].index == 0
        assert results[1].index == 2
