"""Story #1418 Phase 2 of 3: embedding-stats instrumentation for
CohereMultimodalClient._make_request (injection point 4).

Per-attempt wrap (internal retry loop with manual 429/5xx handling) -- each
loop iteration is a separate real, billable vendor call.

_make_request constructs `httpx.Client(...)` directly (no factory
injection), so these tests monkeypatch `httpx.Client` globally to route
through `httpx.MockTransport` -- a real httpx.Client, real request/response
cycle, fake network layer only.
"""

from __future__ import annotations

from typing import Callable, List

import httpx
import pytest

from code_indexer.config import CohereConfig
from code_indexer.services.cohere_multimodal import CohereMultimodalClient


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
    """Route every `httpx.Client(...)` construction through MockTransport,
    regardless of caller-supplied kwargs (mirrors _make_request's direct
    `httpx.Client(timeout=...)` construction with no transport param)."""
    real_client_cls = httpx.Client

    def _fake_client(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client_cls(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", _fake_client)


class TestCohereMultimodalMakeRequestSuccess:
    def test_success_records_one_row(self, monkeypatch):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"embeddings": {"float": [[0.1] * 1536]}})

        _patch_httpx_client_with_mock_transport(monkeypatch, handler)

        config = CohereConfig(
            api_key="test-key", model="embed-v4.0", max_retries=3, retry_delay=0.01
        )
        client = CohereMultimodalClient(config)

        client._make_request([{"content": [{"type": "text", "text": "hi"}]}])

        assert len(writer.records) == 1
        rec = writer.records[0]
        assert rec.provider == "cohere"
        assert rec.call_type == "embed_multimodal"
        assert rec.model == "embed-v4.0"
        assert rec.purpose == "index"
        assert rec.success is True


class TestCohereMultimodalMakeRequestRetryCounting:
    def test_transient_5xx_then_success_produces_two_rows(self, monkeypatch):
        writer = _install_writer()
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(500, json={"error": "server error"})
            return httpx.Response(200, json={"embeddings": {"float": [[0.1] * 1536]}})

        _patch_httpx_client_with_mock_transport(monkeypatch, handler)

        config = CohereConfig(
            api_key="test-key",
            model="embed-v4.0",
            max_retries=3,
            retry_delay=0.01,
            exponential_backoff=False,
        )
        client = CohereMultimodalClient(config)

        client._make_request([{"content": [{"type": "text", "text": "hi"}]}])

        assert call_count["n"] == 2
        assert len(writer.records) == 2
        assert writer.records[0].success is False
        assert writer.records[1].success is True

    def test_client_4xx_error_recorded_as_success_false(self, monkeypatch):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "bad request"})

        _patch_httpx_client_with_mock_transport(monkeypatch, handler)

        config = CohereConfig(
            api_key="test-key", model="embed-v4.0", max_retries=0, retry_delay=0.01
        )
        client = CohereMultimodalClient(config)

        with pytest.raises(Exception):
            client._make_request([{"content": [{"type": "text", "text": "hi"}]}])

        assert len(writer.records) == 1
        assert writer.records[0].success is False
