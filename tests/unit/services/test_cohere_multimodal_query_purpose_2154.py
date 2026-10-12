"""Bug #2154: a Cohere multimodal QUERY embedding must be recorded with
purpose "query", never the generic indexing label "index".

The server's query path reaches the provider through
``EmbeddingCoalescer`` (``get_embeddings_batch(texts, retry=False,
embedding_purpose="query")``); the CLI / vector-store path calls
``get_embedding(text)``. Both funnel into ``_make_request``, which wraps the
HTTP attempt in ``instrument_call``. A wrong "index" label books query spend
as index spend, makes the refresh volume alarm "blind" (index rows with no
alias) and charges an armed run-spend meter.

Only the provider HTTP endpoint is replaced (httpx.MockTransport); the
client, instrument_call, the stats writer hand-off and the run-spend meter
are real.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterator, List, cast

import httpx
import pytest

from code_indexer.config import CohereConfig
from code_indexer.services.cohere_multimodal import CohereMultimodalClient
from code_indexer.services.run_spend_meter import (
    RunSpendDefect,
    get_run_spend_meter,
    input_key,
)


class _RecordingWriter:
    def __init__(self) -> None:
        self.records: List = []

    def record(self, call) -> None:
        self.records.append(call)

    def flush(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolated_state() -> Iterator[None]:
    from code_indexer.server.services.embedding_call_instrumentation import (
        clear_process_default_attribution,
    )
    from code_indexer.server.services.embedding_stats_writer import (
        EmbeddingStatsWriter,
    )

    meter = get_run_spend_meter()
    meter.arm()
    meter.disarm()
    EmbeddingStatsWriter._active = None
    clear_process_default_attribution()
    yield
    clear_process_default_attribution()
    EmbeddingStatsWriter._active = None
    meter.arm()
    meter.disarm()


@pytest.fixture
def writer() -> _RecordingWriter:
    from code_indexer.server.services.embedding_stats_writer import (
        EmbeddingStatsWriter,
    )

    w = _RecordingWriter()
    # Duck-typed stand-in: only record()/flush() are called on the writer.
    EmbeddingStatsWriter.set_active(cast(Any, w))
    return w


def _route_httpx(monkeypatch) -> List[Dict]:
    """Route every httpx.Client through MockTransport; capture request bodies."""
    import json

    bodies: List[Dict] = []
    real_client_cls = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        n = len(body["inputs"])
        return httpx.Response(200, json={"embeddings": {"float": [[0.1] * 8] * n}})

    def _fake_client(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client_cls(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", _fake_client)
    return bodies


def _client() -> CohereMultimodalClient:
    return CohereMultimodalClient(
        CohereConfig(
            api_key="test-key",
            model="embed-v4.0",
            max_retries=0,
            retry_delay=0.01,
            default_dimension=8,
        )
    )


QueryCall = Callable[[CohereMultimodalClient], object]

_QUERY_ENTRY_POINTS: Dict[str, QueryCall] = {
    # EmbeddingCoalescer._dispatch's exact call (server query path).
    "coalescer_batch": lambda c: c.get_embeddings_batch(
        ["novel query text"], retry=False, embedding_purpose="query"
    ),
    # vector_store.search / CLI query path.
    "get_embedding": lambda c: c.get_embedding(
        "novel query text", embedding_purpose="query"
    ),
}


class TestQueryEmbeddingRecordsQueryPurpose:
    @pytest.mark.parametrize("entry", sorted(_QUERY_ENTRY_POINTS))
    def test_query_call_records_purpose_query(
        self, entry: str, writer: _RecordingWriter, monkeypatch
    ) -> None:
        bodies = _route_httpx(monkeypatch)

        _QUERY_ENTRY_POINTS[entry](_client())

        assert [b["input_type"] for b in bodies] == ["search_query"]
        assert [r.purpose for r in writer.records] == ["query"]
        assert writer.records[0].call_type == "embed_multimodal"

    def test_query_purpose_kept_inside_a_refresh_child(
        self, writer: _RecordingWriter, monkeypatch
    ) -> None:
        """A refresh attribution re-labels only the generic "index" tag."""
        from code_indexer.server.services.embedding_call_instrumentation import (
            set_process_default_attribution,
        )
        from code_indexer.server.services.embedding_stats_attribution import (
            StatsAttribution,
        )

        _route_httpx(monkeypatch)
        set_process_default_attribution(
            StatsAttribution(
                golden_repo_alias="example-repo",
                job_id="job-1",
                node_id="node-a",
                purpose="refresh",
            )
        )

        _client().get_embedding("novel query text")

        assert [r.purpose for r in writer.records] == ["query"]


class TestIndexingEmbeddingKeepsIndexPurpose:
    def test_document_batch_records_index(
        self, writer: _RecordingWriter, monkeypatch
    ) -> None:
        bodies = _route_httpx(monkeypatch)

        _client().get_multimodal_embeddings_batch(
            [{"text": "chunk one"}, {"text": "chunk two"}], input_type="document"
        )

        assert [b["input_type"] for b in bodies] == ["search_document"]
        assert [r.purpose for r in writer.records] == ["index"]

    def test_document_batch_inside_refresh_child_records_refresh(
        self, writer: _RecordingWriter, monkeypatch
    ) -> None:
        from code_indexer.server.services.embedding_call_instrumentation import (
            set_process_default_attribution,
        )
        from code_indexer.server.services.embedding_stats_attribution import (
            StatsAttribution,
        )

        _route_httpx(monkeypatch)
        set_process_default_attribution(
            StatsAttribution(
                golden_repo_alias="example-repo",
                job_id="job-1",
                node_id="node-a",
                purpose="refresh",
            )
        )

        _client().get_embeddings_batch(["chunk"], embedding_purpose="document")

        assert [(r.purpose, r.golden_repo_alias) for r in writer.records] == [
            ("refresh", "example-repo")
        ]


class TestRunSpendMeter:
    @pytest.mark.parametrize("entry", sorted(_QUERY_ENTRY_POINTS))
    def test_query_call_passes_armed_meter_uncharged(
        self, entry: str, monkeypatch
    ) -> None:
        bodies = _route_httpx(monkeypatch)
        meter = get_run_spend_meter()
        meter.arm()

        _QUERY_ENTRY_POINTS[entry](_client())

        assert len(bodies) == 1
        assert meter.tripped is None
        snap = meter.snapshot()
        assert (snap.attempts, snap.inputs_sent) == (0, 0)

    def test_unregistered_document_call_still_refused_by_armed_meter(
        self, monkeypatch
    ) -> None:
        bodies = _route_httpx(monkeypatch)
        get_run_spend_meter().arm()

        with pytest.raises(RunSpendDefect):
            _client().get_multimodal_embeddings_batch(
                [{"text": "never registered"}], input_type="document"
            )

        assert bodies == []

    def test_registered_document_call_is_charged(self, monkeypatch) -> None:
        _route_httpx(monkeypatch)
        meter = get_run_spend_meter()
        meter.arm()
        meter.register_misses([input_key("chunk")], max_retries=0)

        _client().get_multimodal_embeddings_batch(
            [{"text": "chunk"}], input_type="document"
        )

        snap = meter.snapshot()
        assert (snap.attempts, snap.inputs_sent) == (1, 1)


class TestUnknownInputTypeFailsLoudly:
    def test_make_request_rejects_unknown_cohere_input_type(
        self, writer: _RecordingWriter, monkeypatch
    ) -> None:
        bodies = _route_httpx(monkeypatch)

        with pytest.raises(ValueError, match="input_type"):
            _client()._make_request(
                [{"content": [{"type": "text", "text": "x"}]}],
                input_type="classification",
            )

        assert bodies == []
        assert writer.records == []
