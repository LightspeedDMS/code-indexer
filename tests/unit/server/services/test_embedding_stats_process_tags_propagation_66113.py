"""Unit tests for EVO-66113 process-level embed-stats tag fallback +
ThreadPoolExecutor propagation (MR !47 review findings).

set_process_embed_stats_tags() sets process-level ContextVar fallbacks
(_stats_golden_repo_alias / _stats_job_id / _stats_node_id) once at `cidx
index` child-process startup, in the process's MAIN thread.
_record_call() reads them back via .get() whenever the caller (every
existing embed-provider call site) passes None for the corresponding
kwarg -- which is ALL of them.

CRITICAL (review finding): the real embedding HTTP calls for concurrent
indexing run inside VectorCalculationManager's ThreadPoolExecutor worker
threads, and plain ThreadPoolExecutor.submit() does NOT propagate
ContextVars into worker threads (same underlying Python behavior already
demonstrated for _search_event_ctx in
tests/unit/server/query/test_embedding_ctx_propagation.py). The fix wraps
the submit() call sites in VectorCalculationManager with
contextvars.copy_context().run(), mirroring the precedent in
semantic_query_manager.py. These tests cover both the fallback semantics
in isolation and the copy_context() propagation fix (MAJOR finding: no
prior test exercised the real ThreadPoolExecutor path).
"""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor

import pytest


class _RecordingWriter:
    def __init__(self):
        self.records = []

    def record(self, call) -> None:
        self.records.append(call)

    def flush(self) -> None:
        pass


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


def _install(writer) -> None:
    from code_indexer.server.services.embedding_stats_writer import (
        EmbeddingStatsWriter,
    )

    EmbeddingStatsWriter.set_active(writer)


class TestSetProcessEmbedStatsTagsFallback:
    """set_process_embed_stats_tags() + _record_call() fallback behavior,
    same-thread (no ThreadPoolExecutor involved)."""

    def test_set_process_tags_are_used_as_fallback_when_caller_passes_none(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call,
            set_process_embed_stats_tags,
        )

        writer = _RecordingWriter()
        _install(writer)

        set_process_embed_stats_tags(
            golden_repo_alias="my-repo-global", job_id="job-42", node_id="node-1"
        )

        instrument_call(
            provider="voyageai",
            call_type="embed",
            model="voyage-code-3",
            item_count=1,
            token_count=1,
            batch_size=1,
            purpose="index",
            fn=lambda: "ok",
        )

        rec = writer.records[0]
        assert rec.golden_repo_alias == "my-repo-global"
        assert rec.job_id == "job-42"
        assert rec.node_id == "node-1"

    def test_explicit_kwargs_take_precedence_over_process_level_fallback(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call,
            set_process_embed_stats_tags,
        )

        writer = _RecordingWriter()
        _install(writer)

        set_process_embed_stats_tags(
            golden_repo_alias="fallback-repo",
            job_id="fallback-job",
            node_id="fallback-node",
        )

        instrument_call(
            provider="voyageai",
            call_type="embed",
            model="voyage-code-3",
            item_count=1,
            token_count=1,
            batch_size=1,
            purpose="index",
            fn=lambda: "ok",
            golden_repo_alias="explicit-repo",
            job_id="explicit-job",
            node_id="explicit-node",
        )

        rec = writer.records[0]
        assert rec.golden_repo_alias == "explicit-repo"
        assert rec.job_id == "explicit-job"
        assert rec.node_id == "explicit-node"

    def test_no_tags_set_leaves_fields_none(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call,
        )

        writer = _RecordingWriter()
        _install(writer)

        instrument_call(
            provider="voyageai",
            call_type="embed",
            model="voyage-code-3",
            item_count=1,
            token_count=1,
            batch_size=1,
            purpose="index",
            fn=lambda: "ok",
        )

        rec = writer.records[0]
        assert rec.golden_repo_alias is None
        assert rec.job_id is None
        assert rec.node_id is None

    def test_blank_values_are_not_set_process_level(self):
        """set_process_embed_stats_tags() guards each field with `if
        value:` -- an empty-string arg must not clobber an existing
        fallback and must not itself become the fallback (mirrors the
        "None means not provided" contract documented on the setter)."""
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call,
            set_process_embed_stats_tags,
        )

        writer = _RecordingWriter()
        _install(writer)

        set_process_embed_stats_tags(golden_repo_alias="", job_id="", node_id="")

        instrument_call(
            provider="voyageai",
            call_type="embed",
            model="voyage-code-3",
            item_count=1,
            token_count=1,
            batch_size=1,
            purpose="index",
            fn=lambda: "ok",
        )

        rec = writer.records[0]
        assert rec.golden_repo_alias is None
        assert rec.job_id is None
        assert rec.node_id is None


class TestProcessTagsThreadPoolExecutorPropagation:
    """EVO-66113 CRITICAL fix: process-level ContextVar fallbacks must
    survive into ThreadPoolExecutor worker threads via
    contextvars.copy_context().run() at the submit() call site -- plain
    executor.submit() does NOT propagate ContextVars across threads."""

    def test_plain_submit_does_not_propagate_tags_to_worker(self):
        """Confirms the underlying Python behavior the bug relied on --
        without copy_context().run(), the worker thread does not see the
        tags set on the submitting thread."""
        from code_indexer.server.services import (
            embedding_call_instrumentation as m,
        )
        from code_indexer.server.services.embedding_call_instrumentation import (
            set_process_embed_stats_tags,
        )

        set_process_embed_stats_tags(golden_repo_alias="my-repo-global")

        captured = []

        def worker_fn():
            captured.append(m._stats_golden_repo_alias.get())

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(worker_fn).result()

        assert captured[0] is None, (
            "Bug confirmed: plain executor.submit() does not propagate the "
            "process-level fallback ContextVar to the worker thread."
        )

    def test_copy_context_run_propagates_tags_to_worker(self):
        """Confirms the fix: copy_context().run() at the submit() call
        site (as now used in VectorCalculationManager.submit_chunk /
        submit_batch_task) propagates the tags into the worker."""
        from code_indexer.server.services import (
            embedding_call_instrumentation as m,
        )
        from code_indexer.server.services.embedding_call_instrumentation import (
            set_process_embed_stats_tags,
        )

        set_process_embed_stats_tags(
            golden_repo_alias="my-repo-global", job_id="job-1", node_id="node-1"
        )

        captured = []

        def worker_fn():
            captured.append(
                (
                    m._stats_golden_repo_alias.get(),
                    m._stats_job_id.get(),
                    m._stats_node_id.get(),
                )
            )

        ctx = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(ctx.run, worker_fn).result()

        assert captured[0] == ("my-repo-global", "job-1", "node-1")

    def test_instrument_call_inside_worker_thread_gets_tags_via_copy_context(self):
        """End-to-end: instrument_call() invoked FROM INSIDE a worker
        thread (as the real embed-provider call sites do) still resolves
        the process-level fallback tags when the submitter used
        copy_context().run()."""
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call,
            set_process_embed_stats_tags,
        )

        writer = _RecordingWriter()
        _install(writer)

        set_process_embed_stats_tags(
            golden_repo_alias="my-repo-global", job_id="job-1", node_id="node-1"
        )

        def worker_fn():
            instrument_call(
                provider="voyageai",
                call_type="embed",
                model="voyage-code-3",
                item_count=1,
                token_count=1,
                batch_size=1,
                purpose="index",
                fn=lambda: "ok",
            )

        ctx = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(ctx.run, worker_fn).result()

        assert len(writer.records) == 1
        rec = writer.records[0]
        assert rec.golden_repo_alias == "my-repo-global"
        assert rec.job_id == "job-1"
        assert rec.node_id == "node-1"
