"""Story #2118 slice 1a: embedding_call_stats rows produced by server-started
indexing carry golden_repo_alias, job_id and node_id.

The attribution reaches ``_record_call`` (embedding_call_instrumentation.py)
two ways: a PROCESS-WIDE default (set once by a `cidx index` child from its
``--stats-attribution`` argument; embedding runs on VectorCalculationManager
pool threads, which no contextvar set on the main thread reaches) and a
scoped contextvar (``stats_attribution``) for in-process server indexing.

Rows are written through the real InProcessAsyncWriter into a real
EmbeddingCallStatsSqliteBackend and read back with its own query().
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, List

import pytest

from code_indexer.server.services import embedding_call_instrumentation as eci
from code_indexer.server.services.embedding_call_stats import (
    EmbeddingCallRecord,
    EmbeddingCallStatsSqliteBackend,
)
from code_indexer.server.services.embedding_stats_attribution import (
    StatsAttribution,
)
from code_indexer.server.services.embedding_stats_writer import (
    EmbeddingStatsWriter,
    InProcessAsyncWriter,
    NoOpWriter,
)

_REFRESH = StatsAttribution(
    golden_repo_alias="example-repo-global",
    job_id="job-1234",
    node_id="node-a",
    purpose="refresh",
)


@pytest.fixture
def stats_rows(tmp_path: Path) -> Iterator["_Rows"]:
    backend = EmbeddingCallStatsSqliteBackend(str(tmp_path / "stats.db"))
    writer = InProcessAsyncWriter(backend)
    EmbeddingStatsWriter.set_active(writer)
    try:
        yield _Rows(backend, writer)
    finally:
        EmbeddingStatsWriter.set_active(NoOpWriter())
        eci.clear_process_default_attribution()


class _Rows:
    def __init__(
        self, backend: EmbeddingCallStatsSqliteBackend, writer: InProcessAsyncWriter
    ) -> None:
        self._backend = backend
        self._writer = writer

    def all(self) -> List[EmbeddingCallRecord]:
        self._writer.flush()
        return self._backend.query(limit=1000)


def _embed(purpose: str, **kwargs: str) -> None:
    eci.instrument_call(
        provider="voyageai",
        call_type="embed",
        model="voyage-code-3",
        item_count=3,
        tokens_from_result=lambda _r: 0,
        batch_size=3,
        purpose=purpose,
        fn=lambda: None,
        **kwargs,
    )


def test_process_default_attribution_fills_unattributed_index_rows(
    stats_rows: _Rows,
) -> None:
    eci.set_process_default_attribution(_REFRESH)

    _embed("index")

    [row] = stats_rows.all()
    assert (row.golden_repo_alias, row.job_id, row.node_id, row.purpose) == (
        "example-repo-global",
        "job-1234",
        "node-a",
        "refresh",
    )


def _key(row: EmbeddingCallRecord) -> tuple:
    return (row.golden_repo_alias, row.job_id, row.node_id, row.purpose)


def test_query_rows_and_explicit_attribution_are_left_unchanged(
    stats_rows: _Rows,
) -> None:
    """Acceptance 4: a query-path call keeps purpose "query"; a call that
    passes its own alias/job/node keeps them."""
    eci.set_process_default_attribution(_REFRESH)

    _embed("query")
    _embed("key_test", golden_repo_alias="pinned-global", job_id="j", node_id="n")

    rows = sorted(stats_rows.all(), key=lambda r: r.occurred_at)
    assert [_key(r) for r in rows] == [
        ("example-repo-global", "job-1234", "node-a", "query"),
        ("pinned-global", "j", "n", "key_test"),
    ]


@pytest.mark.parametrize(
    ("attribution_purpose", "stored_purpose"),
    [("refresh", "temporal_refresh"), ("index", "temporal")],
)
def test_contextual_temporal_call_purpose_follows_attribution(
    stats_rows: _Rows,
    monkeypatch: pytest.MonkeyPatch,
    attribution_purpose: str,
    stored_purpose: str,
) -> None:
    """A temporal (voyage-context-4) call in a REFRESH child is countable
    apart from registration-time temporal spend: `temporal_refresh` vs
    `temporal`. Drives the real VoyageAIClient contextual request (real
    httpx client over a MockTransport) exactly as a child's temporal
    indexer does after installing its --stats-attribution."""
    import httpx

    from code_indexer.config import VoyageAIConfig
    from code_indexer.services.voyage_ai import VoyageAIClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 0, "data": [{"index": 0, "embedding": [0.1] * 1024}]}
                ]
            },
        )

    created: List[httpx.Client] = []

    class _Factory:
        def create_sync_client(self, **_kwargs: object) -> httpx.Client:
            client = httpx.Client(transport=httpx.MockTransport(handler))
            created.append(client)
            return client

    monkeypatch.setenv("VOYAGE_API_KEY", "issue-2118-fake-key")
    eci.set_process_default_attribution(
        StatsAttribution(
            golden_repo_alias="example-repo-global",
            job_id="job-t1",
            node_id="node-a",
            purpose=attribution_purpose,
        )
    )
    voyage = VoyageAIClient(
        VoyageAIConfig(model="voyage-context-4", max_retries=1, retry_delay=0.01),
        http_client_factory=_Factory(),
    )
    try:
        voyage._make_sync_contextualized_request(
            [["chunk1"]], input_type="document", output_dimension=1024, retry=False
        )
    finally:
        for client in created:
            client.close()

    [row] = stats_rows.all()
    assert _key(row) == ("example-repo-global", "job-t1", "node-a", stored_purpose)


def test_no_attribution_leaves_rows_unattributed(stats_rows: _Rows) -> None:
    _embed("query")

    [row] = stats_rows.all()
    assert _key(row) == (None, None, None, "query")


def test_attribution_json_round_trips() -> None:
    assert StatsAttribution.from_json(_REFRESH.to_json()) == _REFRESH


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not-json",
        "[]",
        '{"golden_repo_alias":"a","job_id":"j","node_id":"n"}',
        '{"golden_repo_alias":"a","job_id":"j","node_id":"n","purpose":"index",'
        '"extra":"x"}',
        '{"golden_repo_alias":"","job_id":"j","node_id":"n","purpose":"index"}',
        '{"golden_repo_alias":"a","job_id":7,"node_id":"n","purpose":"index"}',
        '{"golden_repo_alias":"a","job_id":"j","node_id":"n","purpose":"query"}',
    ],
)
def test_malformed_attribution_json_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        StatsAttribution.from_json(raw)
