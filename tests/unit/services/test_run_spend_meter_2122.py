"""Story #2122: unit behaviour of the per-run provider-input meter and of its
charge points (instrument_call and the provider clients).

The provider HTTP endpoint is the only thing replaced (httpx.MockTransport,
counting every request): the meter, instrument_call and the clients are real.
"""

from __future__ import annotations

import os
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, cast
from unittest.mock import patch

import httpx
import pytest

from code_indexer.config import CohereConfig, VoyageAIConfig
from code_indexer.services.run_spend_meter import (
    RunSpendDefect,
    get_run_spend_meter,
    input_key,
)


@pytest.fixture(autouse=True)
def _fresh_meter() -> Iterator[None]:
    meter = get_run_spend_meter()
    meter.arm()
    meter.disarm()
    yield
    meter.arm()
    meter.disarm()


@pytest.fixture
def voyage_key() -> Iterator[None]:
    with patch.dict(os.environ, {"VOYAGE_API_KEY": "PLACEHOLDER"}):
        yield


def _route_httpx(
    monkeypatch, handler: Callable[[httpx.Request], httpx.Response]
) -> Dict[str, int]:
    """Route every httpx.Client through MockTransport; count requests."""
    calls = {"n": 0}
    real_client_cls = httpx.Client

    def counting(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return handler(request)

    def _fake_client(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client_cls(*args, transport=httpx.MockTransport(counting), **kwargs)

    monkeypatch.setattr(httpx, "Client", _fake_client)
    return calls


def _voyage_multimodal_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"embedding": [0.1] * 1024}]})


def _cohere_multimodal_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"embeddings": {"float": [[0.1] * 1536]}})


class TestMultimodalCharges:
    def test_voyage_multimodal_registered_text_is_sent_and_counted(
        self, monkeypatch, voyage_key
    ) -> None:
        from code_indexer.services.voyage_multimodal import VoyageMultimodalClient

        calls = _route_httpx(monkeypatch, _voyage_multimodal_ok)
        client = VoyageMultimodalClient(VoyageAIConfig(model="voyage-multimodal-3"))
        meter = get_run_spend_meter()
        meter.arm()
        meter.register_misses([input_key("diagram chunk")], max_retries=0)

        client.get_multimodal_embedding("diagram chunk", image_paths=[])

        assert calls["n"] == 1
        snap = meter.snapshot()
        assert (snap.attempts, snap.inputs_sent) == (1, 1)

    def test_voyage_multimodal_unregistered_text_never_reaches_http(
        self, monkeypatch, voyage_key
    ) -> None:
        from code_indexer.services.voyage_multimodal import VoyageMultimodalClient

        calls = _route_httpx(monkeypatch, _voyage_multimodal_ok)
        client = VoyageMultimodalClient(VoyageAIConfig(model="voyage-multimodal-3"))
        get_run_spend_meter().arm()

        with pytest.raises(RunSpendDefect):
            client.get_multimodal_embedding("never registered", image_paths=[])

        assert calls["n"] == 0

    def test_cohere_multimodal_registered_text_is_sent_and_counted(
        self, monkeypatch
    ) -> None:
        from code_indexer.services.cohere_multimodal import CohereMultimodalClient

        calls = _route_httpx(monkeypatch, _cohere_multimodal_ok)
        client = CohereMultimodalClient(
            CohereConfig(api_key="test-key", model="embed-v4.0", retry_delay=0.01)
        )
        meter = get_run_spend_meter()
        meter.arm()
        meter.register_misses([input_key("diagram chunk")], max_retries=0)

        client._make_request([{"content": [{"type": "text", "text": "diagram chunk"}]}])

        assert calls["n"] == 1
        snap = meter.snapshot()
        assert (snap.attempts, snap.inputs_sent) == (1, 1)


class _RegularBatchStub:
    """VectorCalculationManager stand-in for the regular (text) batches:
    returns one embedding per text without any provider call."""

    def __init__(self, max_retries: int) -> None:
        import threading

        self.cancellation_event = threading.Event()
        self.embedding_provider = _ProviderSettings(max_retries)
        self.batches: List[List[str]] = []

    def submit_batch_task(self, chunk_texts, metadata) -> "Future[Any]":
        from code_indexer.services.vector_calculation_manager import VectorResult

        self.batches.append(list(chunk_texts))
        future: "Future[Any]" = Future()
        future.set_result(
            VectorResult(
                task_id=f"batch_{len(self.batches)}",
                embeddings=tuple((0.1,) * 8 for _ in chunk_texts),
                metadata=dict(metadata),
                processing_time=0.0,
                error=None,
            )
        )
        return future


class _ProviderSettings:
    def __init__(self, max_retries: int) -> None:
        self._max_retries = max_retries

    def request_max_retries(self) -> int:
        return self._max_retries

    def _get_model_token_limit(self) -> int:
        return 120000

    def get_current_model(self) -> str:
        return "voyage-code-3"


class _EmptyStore:
    """vector_store_client stand-in: nothing stored, so every chunk misses."""

    def __init__(self) -> None:
        self.upserted: List[Dict[str, Any]] = []

    def get_existing_content_hashes(self, *args, **kwargs) -> Dict[str, Any]:
        return {}

    def collection_exists(self, collection_name: str) -> bool:
        return True

    def create_collection(self, collection_name: str, vector_size: int) -> bool:
        return True

    def upsert_points(self, points, collection_name=None) -> bool:
        self.upserted.extend(points)
        return True


class TestFileChunkingManagerRegistration:
    def test_multimodal_chunk_is_registered_before_send(
        self, monkeypatch, voyage_key, tmp_path: Path
    ) -> None:
        """The resolver registers every regular miss (1 + max_retries each)
        and the multimodal path registers the chunk it sends; an armed meter
        then lets that multimodal input through to HTTP exactly once."""
        from code_indexer.services.clean_slot_tracker import CleanSlotTracker
        from code_indexer.services.file_chunking_manager import FileChunkingManager
        from code_indexer.services.voyage_multimodal import VoyageMultimodalClient

        calls = _route_httpx(monkeypatch, _voyage_multimodal_ok)
        (tmp_path / "diagram.png").write_bytes(b"placeholder-image-bytes")
        doc = tmp_path / "docs.md"
        doc.write_text("# Architecture\n\n![Diagram](diagram.png)\n\nText.")
        chunker = _StaticChunker(
            [
                _chunk(0, "# Architecture\n\n![Diagram](diagram.png)", ["diagram.png"]),
                _chunk(1, "regular chunk one payload", []),
                _chunk(2, "regular chunk two payload", []),
            ]
        )
        multimodal = VoyageMultimodalClient(VoyageAIConfig(model="voyage-multimodal-3"))
        meter = get_run_spend_meter()
        meter.arm()

        with FileChunkingManager(
            vector_manager=_RegularBatchStub(max_retries=2),  # type: ignore[arg-type]
            chunker=chunker,  # type: ignore[arg-type]
            vector_store_client=_EmptyStore(),
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=tmp_path,
            multimodal_client=multimodal,
        ) as manager:
            metadata = {
                "project_id": "proj",
                "file_hash": "sha256:abc123",
                "collection_name": "col",
                "git_available": False,
            }
            result = manager.submit_file_for_processing(doc, metadata, None).result(
                timeout=10.0
            )

        assert result.success is True, result.error
        assert calls["n"] == 1
        snap = meter.snapshot()
        assert snap.registered_misses == 3
        # 2 regular misses * (1 + 2) + 1 multimodal * (1 + 0)
        assert snap.allowance == 7
        assert (snap.attempts, snap.inputs_sent) == (1, 1)


def _instrumented(purpose: str, inputs, sent: List[int]) -> None:
    from code_indexer.server.services.embedding_call_instrumentation import (
        instrument_call,
    )

    def _fn() -> None:
        sent.append(1)

    instrument_call(
        provider="voyageai",
        call_type="embed",
        model="voyage-code-3",
        item_count=len(inputs or []),
        token_count=0,
        batch_size=len(inputs or []),
        purpose=purpose,
        fn=_fn,
        inputs=inputs,
    )


class TestQueryPathIsNotMetered:
    def test_query_purpose_passes_an_armed_meter_uncounted(self) -> None:
        meter = get_run_spend_meter()
        meter.arm()
        sent: List[int] = []

        _instrumented("query", ["a query nobody registered"], sent)

        assert sent == [1]
        assert meter.tripped is None
        assert (meter.snapshot().attempts, meter.snapshot().inputs_sent) == (0, 0)

    def test_unarmed_process_index_purpose_passes_uncounted(self) -> None:
        """The server process never arms the meter."""
        sent: List[int] = []

        _instrumented("index", ["anything"], sent)

        assert sent == [1]
        assert get_run_spend_meter().snapshot().attempts == 0

    def test_multimodal_query_embedding_reaches_http_with_armed_meter(
        self, monkeypatch, voyage_key
    ) -> None:
        from code_indexer.services.voyage_multimodal import VoyageMultimodalClient

        calls = _route_httpx(monkeypatch, _voyage_multimodal_ok)
        client = VoyageMultimodalClient(VoyageAIConfig(model="voyage-multimodal-3"))
        get_run_spend_meter().arm()

        client.get_embedding("server-side query text")

        assert calls["n"] == 1
        assert get_run_spend_meter().tripped is None


class TestMeterRules:
    def test_unregistered_input_refused_and_meter_stays_tripped(self) -> None:
        meter = get_run_spend_meter()
        meter.arm()
        meter.register_misses([input_key("planned")], max_retries=3)
        sent: List[int] = []

        with pytest.raises(RunSpendDefect, match="not registered"):
            _instrumented("index", ["planned", "unplanned"], sent)
        # Tripped: even a registered input is refused from now on.
        with pytest.raises(RunSpendDefect):
            _instrumented("index", ["planned"], sent)

        assert sent == []
        assert meter.tripped is not None
        assert meter.snapshot().attempts == 0

    def test_attempts_beyond_misses_times_one_plus_retries_refused(self) -> None:
        meter = get_run_spend_meter()
        meter.arm()
        meter.register_misses([input_key("k")], max_retries=2)
        sent: List[int] = []

        for _ in range(3):
            _instrumented("index", ["k"], sent)
        with pytest.raises(RunSpendDefect, match="exceeds the run allowance"):
            _instrumented("index", ["k"], sent)

        assert len(sent) == 3
        assert meter.snapshot().attempts == 3

    def test_inputs_beyond_the_allowance_refused(self) -> None:
        """Two registered misses with no retries allow two input sends: one
        batch of both, then any resend is refused (attempts 2 <= 2, inputs
        4 > 2)."""
        meter = get_run_spend_meter()
        meter.arm()
        meter.register_misses([input_key("a"), input_key("b")], max_retries=0)
        sent: List[int] = []

        _instrumented("index", ["a", "b"], sent)
        with pytest.raises(RunSpendDefect, match="inputs exceed"):
            _instrumented("index", ["a", "b"], sent)

        assert sent == [1]

    def test_call_without_inputs_refused_when_armed(self) -> None:
        get_run_spend_meter().arm()
        sent: List[int] = []

        with pytest.raises(RunSpendDefect, match="no meterable inputs"):
            _instrumented("index", None, sent)

        assert sent == []

    def test_registration_ignored_while_unarmed(self) -> None:
        meter = get_run_spend_meter()
        meter.register_misses([input_key("x")], max_retries=1)
        meter.arm()

        assert meter.snapshot().registered_misses == 0

    @pytest.mark.parametrize("bad", [-1, True, 1.5, "3"])
    def test_invalid_max_retries_rejected(self, bad) -> None:
        meter = get_run_spend_meter()
        meter.arm()

        with pytest.raises((TypeError, ValueError)):
            meter.register_misses([input_key("x")], max_retries=bad)

    def test_arm_resets_a_previous_run(self) -> None:
        meter = get_run_spend_meter()
        meter.arm()
        with pytest.raises(RunSpendDefect):
            _instrumented("index", ["x"], [])

        meter.arm()

        assert meter.tripped is None
        meter.raise_if_tripped()
        assert meter.snapshot().allowance == 0

    def test_disarm_ends_the_run_completely(self) -> None:
        """After the CLI's run ends (disarm), the process is back to an
        unmetered state: no trip leaks into a later exit-code decision."""
        from code_indexer.services.index_failure_exit_codes import (
            index_failure_exit_code,
        )

        meter = get_run_spend_meter()
        meter.arm()
        with pytest.raises(RunSpendDefect):
            _instrumented("index", ["x"], [])

        meter.disarm()

        assert meter.tripped is None
        assert index_failure_exit_code(RuntimeError("later failure")) == 1
        _instrumented("index", ["unmetered again"], [])

    def test_refused_charge_writes_no_stats_record(self) -> None:
        """No attempt happened, so no embedding_call_stats row is written."""
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
        )

        records: List[Any] = []

        class _Writer:
            def record(self, call) -> None:
                records.append(call)

            def flush(self) -> None:
                pass

        previous_writer = EmbeddingStatsWriter._active
        # Duck-typed writer (record/flush), like the #1418 tests' writers.
        EmbeddingStatsWriter.set_active(cast(EmbeddingStatsWriter, _Writer()))
        try:
            get_run_spend_meter().arm()
            with pytest.raises(RunSpendDefect):
                _instrumented("index", ["x"], [])
        finally:
            EmbeddingStatsWriter._active = previous_writer

        assert records == []


class TestProviderContract:
    def test_provider_without_request_max_retries_cannot_be_instantiated(
        self,
    ) -> None:
        """Every EmbeddingProvider must state its retry budget: an armed run
        registers misses with it, so a provider lacking it is refused at
        construction, never per file mid-run."""
        from code_indexer.services.embedding_provider import EmbeddingProvider

        class _NoRetryBudget(EmbeddingProvider):
            def get_embedding(self, text, model=None, embedding_purpose=None):
                return [0.0]

            def get_embeddings_batch(self, texts, model=None):
                return [[0.0] for _ in texts]

            def get_embedding_with_metadata(self, text, model=None):
                raise NotImplementedError

            def get_embeddings_batch_with_metadata(self, texts, model=None):
                raise NotImplementedError

            def health_check(self, *, test_api: bool = False) -> bool:
                return True

            def get_model_info(self):
                return {}

            def get_provider_name(self) -> str:
                return "none"

            def get_current_model(self) -> str:
                return "none"

            def supports_batch_processing(self) -> bool:
                return True

        with pytest.raises(TypeError, match="request_max_retries"):
            _NoRetryBudget()  # type: ignore[abstract]


class TestRetryBudgets:
    def test_request_max_retries_follow_each_client(self, voyage_key) -> None:
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider
        from code_indexer.services.cohere_multimodal import CohereMultimodalClient
        from code_indexer.services.voyage_ai import VoyageAIClient
        from code_indexer.services.voyage_multimodal import VoyageMultimodalClient

        assert VoyageAIClient(VoyageAIConfig(max_retries=5)).request_max_retries() == 5
        cohere = CohereConfig(api_key="test-key", max_retries=4)
        assert CohereEmbeddingProvider(cohere).request_max_retries() == 4
        assert CohereMultimodalClient(cohere).request_max_retries() == 4
        # No retry loop: one attempt per request.
        multimodal = VoyageMultimodalClient(VoyageAIConfig(max_retries=5))
        assert multimodal.request_max_retries() == 0


class _MockFactory:
    """http_client_factory double routing the provider's pooled client to a
    counting MockTransport (the provider endpoint is the only fake)."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.calls = 0
        self._handler = handler

    def create_sync_client(self, **kwargs) -> httpx.Client:
        def counting(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            return self._handler(request)

        return httpx.Client(transport=httpx.MockTransport(counting))


class TestHealthProbe:
    def test_health_probe_is_allowed_by_armed_meter(self, voyage_key) -> None:
        from code_indexer.services.voyage_ai import VoyageAIClient

        def ok(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"data": [{"embedding": [0.1] * 1024, "index": 0}]}
            )

        factory = _MockFactory(ok)
        client = VoyageAIClient(VoyageAIConfig(), http_client_factory=factory)
        get_run_spend_meter().arm()

        assert client.health_check(test_api=True) is True
        assert factory.calls == 1
        assert get_run_spend_meter().tripped is None
        assert get_run_spend_meter().snapshot().inputs_sent == 1

    def test_cohere_health_probe_is_allowed_by_armed_meter(self) -> None:
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

        factory = _MockFactory(
            lambda request: httpx.Response(
                200, json={"embeddings": {"float": [[0.1] * 1536]}}
            )
        )
        provider = CohereEmbeddingProvider(
            CohereConfig(api_key="test-key"), http_client_factory=factory
        )
        get_run_spend_meter().arm()

        assert provider.health_check(test_api=True) is True
        assert factory.calls == 1
        assert get_run_spend_meter().tripped is None
        assert get_run_spend_meter().snapshot().inputs_sent == 1

    def test_two_providers_in_one_armed_run_register_and_charge_independently(
        self, voyage_key
    ) -> None:
        """A multi-provider run: each provider registers its own misses with
        its own retry budget; the allowance is their sum and each provider's
        sends are charged against the keys it registered."""
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider
        from code_indexer.services.voyage_ai import VoyageAIClient

        voyage_http = _MockFactory(
            lambda request: httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.1] * 1024, "index": i} for i in range(2)]
                },
            )
        )
        cohere_http = _MockFactory(
            lambda request: httpx.Response(
                200, json={"embeddings": {"float": [[0.1] * 1536]}}
            )
        )
        voyage = VoyageAIClient(
            VoyageAIConfig(max_retries=3), http_client_factory=voyage_http
        )
        cohere = CohereEmbeddingProvider(
            CohereConfig(api_key="test-key", max_retries=1),
            http_client_factory=cohere_http,
        )
        meter = get_run_spend_meter()
        meter.arm()
        meter.register_misses(
            [input_key("v1"), input_key("v2")],
            max_retries=voyage.request_max_retries(),
        )
        meter.register_misses(
            [input_key("c1")], max_retries=cohere.request_max_retries()
        )

        voyage._make_sync_request(["v1", "v2"])
        cohere._make_sync_request(["c1"], "search_document")

        snap = meter.snapshot()
        assert snap.registered_misses == 3
        assert snap.allowance == 2 * (1 + 3) + 1 * (1 + 1)
        assert (snap.attempts, snap.inputs_sent) == (2, 3)
        assert (voyage_http.calls, cohere_http.calls) == (1, 1)
        with pytest.raises(RunSpendDefect):
            cohere._make_sync_request(["c2 never registered"], "search_document")
        assert cohere_http.calls == 1

    def test_cohere_unregistered_input_refused_before_http(self) -> None:
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

        factory = _MockFactory(lambda request: httpx.Response(500))
        provider = CohereEmbeddingProvider(
            CohereConfig(api_key="test-key", retry_delay=0.01),
            http_client_factory=factory,
        )
        get_run_spend_meter().arm()

        with pytest.raises(RunSpendDefect):
            provider._make_sync_request(["never registered"], "search_document")

        assert factory.calls == 0

    def test_contextualized_request_charges_flattened_chunks(self, voyage_key) -> None:
        from code_indexer.services.voyage_ai import VoyageAIClient

        factory = _MockFactory(lambda request: httpx.Response(500))
        client = VoyageAIClient(VoyageAIConfig(), http_client_factory=factory)
        meter = get_run_spend_meter()
        meter.arm()
        meter.register_misses([input_key("c1")], max_retries=0)

        with pytest.raises(RunSpendDefect, match="1 of 2"):
            client._make_sync_contextualized_request(
                [["c1", "c2"]], input_type="document"
            )

        assert factory.calls == 0


class TestExitCodeAndRecord:
    def test_defect_maps_to_88_and_is_no_chunk_store_kind(self) -> None:
        from code_indexer.services.index_failure_exit_codes import (
            EXIT_CODE_CHUNK_STORE_CORRUPTION,
            EXIT_CODE_CHUNK_STORE_ENVIRONMENT,
            EXIT_CODE_RUN_SPEND_DEFECT,
            chunk_store_failure_kind_for_exit_code,
            index_failure_exit_code,
        )

        assert EXIT_CODE_RUN_SPEND_DEFECT == 88
        assert EXIT_CODE_RUN_SPEND_DEFECT not in (
            EXIT_CODE_CHUNK_STORE_CORRUPTION,
            EXIT_CODE_CHUNK_STORE_ENVIRONMENT,
        )
        # The parent's classifier sees no chunk-store kind: loud failure.
        assert chunk_store_failure_kind_for_exit_code(88) is None
        assert index_failure_exit_code(RunSpendDefect("x")) == 88
        try:
            try:
                raise RunSpendDefect("inner")
            except RunSpendDefect as inner:
                raise RuntimeError("Batch embedding request failed") from inner
        except RuntimeError as wrapped:
            assert index_failure_exit_code(wrapped) == 88
        assert index_failure_exit_code(RuntimeError("unrelated")) == 1

    def test_tripped_meter_wins_over_the_surfaced_exception(self) -> None:
        from code_indexer.services.index_failure_exit_codes import (
            index_failure_exit_code,
        )

        get_run_spend_meter().arm()
        with pytest.raises(RunSpendDefect):
            _instrumented("index", ["x"], [])

        assert index_failure_exit_code(RuntimeError("All files failed")) == 88

    def test_final_record_shape_is_ignored_by_progress_parser(self, capsys) -> None:
        import json

        from code_indexer.services.progress_phase_allocator import (
            emit_run_spend_json,
            parse_progress_line,
        )

        emit_run_spend_json(12, 3)
        line = capsys.readouterr().out.strip()

        assert json.loads(line) == {
            "record": "run_spend",
            "inputs_sent": 12,
            "attempts": 3,
        }
        assert parse_progress_line(line) is None


class _StaticChunker:
    def __init__(self, chunks: List[Dict[str, Any]]) -> None:
        self._chunks = chunks

    def chunk_file(self, file_path, repo_root=None) -> List[Dict[str, Any]]:
        return [dict(chunk) for chunk in self._chunks]


def _chunk(index: int, text: str, images: List[str]) -> Dict[str, Any]:
    return {
        "text": text,
        "chunk_index": index,
        "total_chunks": 3,
        "file_extension": "md",
        "size": len(text),
        "line_start": index * 10 + 1,
        "line_end": index * 10 + 10,
        "file_path": None,
        "images": images,
    }
