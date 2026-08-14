"""Integration test for EVO-66113 (MR !47 review MAJOR finding): prove the
fix works through VectorCalculationManager's REAL ThreadPoolExecutor, not
just via direct contextvars manipulation.

Before the fix, golden_repo_alias/job_id/node_id set via
set_process_embed_stats_tags() in the main thread (as `cidx index` does
once at child-process startup) never reached instrument_call() inside
VectorCalculationManager's worker threads, so EmbeddingCallRecord rows for
the concurrent indexing path always had these fields NULL. The fix wraps
submit_chunk()/submit_batch_task()'s executor.submit() calls with
contextvars.copy_context().run().
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from code_indexer.services.embedding_provider import EmbeddingProvider
from code_indexer.services.vector_calculation_manager import VectorCalculationManager


class _RecordingWriter:
    def __init__(self):
        self.records = []

    def record(self, call) -> None:
        self.records.append(call)

    def flush(self) -> None:
        pass


class _InstrumentedFakeProvider(EmbeddingProvider):
    """Mimics real providers (voyage_ai.py / cohere_embedding.py):
    get_embeddings_batch() is instrumented via instrument_call(), passing
    None for golden_repo_alias/job_id/node_id -- exactly what every real
    embed-provider call site does today (relies entirely on the
    process-level fallback resolved inside instrument_call/_record_call)."""

    def get_embedding(self, text, model=None, embedding_purpose=None):
        return self.get_embeddings_batch([text])[0]

    def get_embeddings_batch(self, texts: List[str], model: Optional[str] = None):
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call,
        )

        return instrument_call(
            provider="voyageai",
            call_type="embed",
            model="voyage-code-3",
            item_count=len(texts),
            token_count=0,
            batch_size=len(texts),
            purpose="index",
            fn=lambda: [[0.1, 0.2] for _ in texts],
        )

    def get_embedding_with_metadata(self, text, model=None):
        raise NotImplementedError

    def get_embeddings_batch_with_metadata(self, texts, model=None):
        raise NotImplementedError

    def health_check(self, *, test_api: bool = False) -> bool:
        return True

    def get_model_info(self) -> Dict[str, Any]:
        return {}

    def get_provider_name(self) -> str:
        return "voyageai"

    def get_current_model(self) -> str:
        return "voyage-code-3"

    def supports_batch_processing(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _reset_stats_state():
    from code_indexer.server.services import embedding_call_instrumentation as m
    from code_indexer.server.services.embedding_stats_writer import (
        EmbeddingStatsWriter,
    )

    EmbeddingStatsWriter._active = None
    m._stats_golden_repo_alias.set(None)
    m._stats_job_id.set(None)
    m._stats_node_id.set(None)
    yield
    EmbeddingStatsWriter._active = None
    m._stats_golden_repo_alias.set(None)
    m._stats_job_id.set(None)
    m._stats_node_id.set(None)


class TestVectorCalculationManagerEmbedStatsPropagation:
    def test_submit_chunk_propagates_process_level_tags_into_worker_thread(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            set_process_embed_stats_tags,
        )
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
        )

        writer = _RecordingWriter()
        EmbeddingStatsWriter.set_active(writer)

        # As done once at `cidx index` child-process startup, in the MAIN
        # thread, BEFORE any VectorCalculationManager task is submitted.
        set_process_embed_stats_tags(
            golden_repo_alias="my-golden-repo",
            job_id="job-77",
            node_id="node-3",
        )

        manager = VectorCalculationManager(
            embedding_provider=_InstrumentedFakeProvider(), thread_count=2
        )
        manager.start()
        try:
            future = manager.submit_chunk("some chunk text", metadata={})
            result = future.result(timeout=5)
            assert result.error is None
        finally:
            manager.shutdown(wait=True)

        assert (
            len(writer.records) == 1
        ), "instrument_call must have recorded exactly one call"
        rec = writer.records[0]
        assert rec.golden_repo_alias == "my-golden-repo", (
            "EVO-66113 regression: golden_repo_alias fallback did not "
            "survive into the VectorCalculationManager ThreadPoolExecutor "
            "worker"
        )
        assert rec.job_id == "job-77"
        assert rec.node_id == "node-3"

    def test_submit_batch_task_propagates_process_level_tags_into_worker_thread(
        self,
    ):
        from code_indexer.server.services.embedding_call_instrumentation import (
            set_process_embed_stats_tags,
        )
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
        )

        writer = _RecordingWriter()
        EmbeddingStatsWriter.set_active(writer)

        set_process_embed_stats_tags(
            golden_repo_alias="batch-repo", job_id="job-88", node_id="node-4"
        )

        manager = VectorCalculationManager(
            embedding_provider=_InstrumentedFakeProvider(), thread_count=2
        )
        manager.start()
        try:
            future = manager.submit_batch_task(["chunk one", "chunk two"], metadata={})
            result = future.result(timeout=5)
            assert result.error is None
        finally:
            manager.shutdown(wait=True)

        assert len(writer.records) == 1
        rec = writer.records[0]
        assert rec.golden_repo_alias == "batch-repo"
        assert rec.job_id == "job-88"
        assert rec.node_id == "node-4"
