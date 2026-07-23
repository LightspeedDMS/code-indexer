"""Story #1418 Phase 2 of 3: embedding-stats instrumentation for
VoyageMultimodalClient (injection points 7 and 8).

Method-boundary wrap sufficient -- get_multimodal_embedding() (point 7,
single-item) and _submit_multimodal_batch() (point 8, batch) each have NO
internal retry loop (single httpx.Client(...).post() + raise_for_status()
call), so wrapping the method boundary is equivalent to per-attempt.

VoyageMultimodalClient constructs `httpx.Client(...)` directly (no factory
injection), so these tests monkeypatch `httpx.Client` globally to route
through `httpx.MockTransport`.
"""

from __future__ import annotations

import os
from typing import Callable, List
from unittest.mock import patch

import httpx
import pytest

from code_indexer.config import VoyageAIConfig
from code_indexer.services.voyage_multimodal import VoyageMultimodalClient


class _RecordingWriter:
    def __init__(self):
        self.records: List = []

    def record(self, call) -> None:
        self.records.append(call)

    def flush(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_active_writer():
    from code_indexer.server.services.embedding_stats_writer import (
        EmbeddingStatsWriter,
    )

    EmbeddingStatsWriter._active = None
    yield
    EmbeddingStatsWriter._active = None


def _install_writer() -> _RecordingWriter:
    from code_indexer.server.services.embedding_stats_writer import (
        EmbeddingStatsWriter,
    )

    writer = _RecordingWriter()
    EmbeddingStatsWriter.set_active(writer)
    return writer


def _patch_httpx_client_with_mock_transport(
    monkeypatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    real_client_cls = httpx.Client

    def _fake_client(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client_cls(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", _fake_client)


@pytest.fixture
def mock_api_key():
    with patch.dict(os.environ, {"VOYAGE_API_KEY": "PLACEHOLDER"}):
        yield "PLACEHOLDER"


class TestGetMultimodalEmbeddingSingleItem:
    """Injection point 7: single-item multimodal embed method."""

    def test_success_records_one_row(self, monkeypatch, mock_api_key):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"embedding": [0.1] * 1024}]})

        _patch_httpx_client_with_mock_transport(monkeypatch, handler)

        config = VoyageAIConfig(model="voyage-multimodal-3")
        client = VoyageMultimodalClient(config)

        client.get_multimodal_embedding("hello", image_paths=[])

        assert len(writer.records) == 1
        rec = writer.records[0]
        assert rec.provider == "voyageai"
        assert rec.call_type == "embed_multimodal"
        assert rec.purpose == "index"
        assert rec.success is True

    def test_query_purpose_used_when_called_as_get_embedding(
        self, monkeypatch, mock_api_key
    ):
        """get_embedding() delegates to get_multimodal_embedding() with
        input_type='query' for recall -- purpose should reflect the query
        path, not indexing."""
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"embedding": [0.1] * 1024}]})

        _patch_httpx_client_with_mock_transport(monkeypatch, handler)

        config = VoyageAIConfig(model="voyage-multimodal-3")
        client = VoyageMultimodalClient(config)

        client.get_embedding("hello")

        assert len(writer.records) == 1
        assert writer.records[0].purpose == "query"

    def test_vendor_4xx_recorded_as_success_false(self, monkeypatch, mock_api_key):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "bad request"})

        _patch_httpx_client_with_mock_transport(monkeypatch, handler)

        config = VoyageAIConfig(model="voyage-multimodal-3")
        client = VoyageMultimodalClient(config)

        with pytest.raises(httpx.HTTPStatusError):
            client.get_multimodal_embedding("hello", image_paths=[])

        assert len(writer.records) == 1
        assert writer.records[0].success is False


class TestSubmitMultimodalBatch:
    """Injection point 8: batch multimodal embed method."""

    def test_success_records_one_row(self, monkeypatch, mock_api_key):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"embedding": [0.1] * 1024},
                        {"embedding": [0.2] * 1024},
                    ]
                },
            )

        _patch_httpx_client_with_mock_transport(monkeypatch, handler)

        config = VoyageAIConfig(model="voyage-multimodal-3")
        client = VoyageMultimodalClient(config)

        client._submit_multimodal_batch(
            [{"text": "a", "image_paths": []}, {"text": "b", "image_paths": []}]
        )

        assert len(writer.records) == 1
        rec = writer.records[0]
        assert rec.call_type == "embed_multimodal"
        assert rec.purpose == "index"
        assert rec.item_count == 2
        assert rec.success is True

    def test_vendor_4xx_recorded_as_success_false(self, monkeypatch, mock_api_key):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "bad request"})

        _patch_httpx_client_with_mock_transport(monkeypatch, handler)

        config = VoyageAIConfig(model="voyage-multimodal-3")
        client = VoyageMultimodalClient(config)

        with pytest.raises(httpx.HTTPStatusError):
            client._submit_multimodal_batch([{"text": "a", "image_paths": []}])

        assert len(writer.records) == 1
        assert writer.records[0].success is False
