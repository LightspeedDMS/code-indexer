"""Unit tests for instrument_call -- Story #1418 Phase 2 of 3, Component 4.

instrument_call() wraps the smallest unit of work that includes BOTH the
outbound HTTP call AND its status validation (raise_for_status or
equivalent) so a vendor 4xx/5xx is correctly recorded as success=False, not
success=True. It records exactly one EmbeddingCallRecord per real call
attempt via EmbeddingStatsWriter.get_active().record(), and NEVER lets a
stats-recording failure mask or replace the original result/exception from
the wrapped call (fail-open, observability-only).
"""

from __future__ import annotations

import time

import pytest


class _RecordingWriter:
    def __init__(self):
        self.records = []

    def record(self, call) -> None:
        self.records.append(call)

    def flush(self) -> None:
        pass


class _RaisingWriter:
    """Simulates a writer whose record() itself raises -- must never
    propagate out of instrument_call and must never mask the wrapped
    call's own result/exception."""

    def record(self, call) -> None:
        raise RuntimeError("simulated stats-writer failure")

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


def _install(writer) -> None:
    from code_indexer.server.services.embedding_stats_writer import (
        EmbeddingStatsWriter,
    )

    EmbeddingStatsWriter.set_active(writer)


class TestInstrumentCallSuccess:
    def test_records_success_true_and_returns_wrapped_result(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call,
        )

        writer = _RecordingWriter()
        _install(writer)

        result = instrument_call(
            provider="voyageai",
            call_type="embed",
            model="voyage-code-3",
            item_count=3,
            token_count=42,
            batch_size=3,
            purpose="index",
            fn=lambda: {"data": "ok"},
        )

        assert result == {"data": "ok"}
        assert len(writer.records) == 1
        rec = writer.records[0]
        assert rec.provider == "voyageai"
        assert rec.call_type == "embed"
        assert rec.model == "voyage-code-3"
        assert rec.item_count == 3
        assert rec.token_count == 42
        assert rec.batch_size == 3
        assert rec.purpose == "index"
        assert rec.success is True
        assert rec.latency_ms >= 0

    def test_optional_correlation_fields_are_forwarded(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call,
        )

        writer = _RecordingWriter()
        _install(writer)

        instrument_call(
            provider="cohere",
            call_type="rerank",
            model="rerank-v3.5",
            item_count=1,
            token_count=0,
            batch_size=1,
            purpose="query",
            fn=lambda: None,
            golden_repo_alias="my-repo-global",
            job_id="job-123",
            node_id="node-1",
        )

        rec = writer.records[0]
        assert rec.golden_repo_alias == "my-repo-global"
        assert rec.job_id == "job-123"
        assert rec.node_id == "node-1"


class TestInstrumentCallFailure:
    def test_records_success_false_and_reraises_original_exception(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call,
        )

        writer = _RecordingWriter()
        _install(writer)

        def _boom():
            raise ValueError("vendor 4xx/5xx via raise_for_status")

        with pytest.raises(ValueError, match="vendor 4xx/5xx"):
            instrument_call(
                provider="voyageai",
                call_type="rerank",
                model="rerank-2.5",
                item_count=5,
                token_count=0,
                batch_size=5,
                purpose="query",
                fn=_boom,
            )

        assert len(writer.records) == 1
        assert writer.records[0].success is False


class TestInstrumentCallStatsWriterFailOpen:
    def test_writer_record_exception_does_not_mask_success_result(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call,
        )

        _install(_RaisingWriter())

        result = instrument_call(
            provider="voyageai",
            call_type="embed",
            model="voyage-code-3",
            item_count=1,
            token_count=1,
            batch_size=1,
            purpose="index",
            fn=lambda: "real-result",
        )

        assert result == "real-result"

    def test_writer_record_exception_does_not_mask_original_wrapped_exception(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call,
        )

        _install(_RaisingWriter())

        def _boom():
            raise RuntimeError("the real vendor error")

        with pytest.raises(RuntimeError, match="the real vendor error"):
            instrument_call(
                provider="voyageai",
                call_type="embed",
                model="voyage-code-3",
                item_count=1,
                token_count=1,
                batch_size=1,
                purpose="index",
                fn=_boom,
            )


class TestInstrumentCallDefaultsToNoOpWhenNoWriterInstalled:
    def test_no_writer_installed_defaults_to_noop_and_still_returns_result(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call,
        )

        result = instrument_call(
            provider="voyageai",
            call_type="embed",
            model="voyage-code-3",
            item_count=1,
            token_count=1,
            batch_size=1,
            purpose="index",
            fn=lambda: 42,
        )
        assert result == 42


class TestInstrumentCallAsyncSuccess:
    """instrument_call_async() mirrors instrument_call() for async callers
    (Story #1418 injection points 9/10: api_key_management.py's async
    connectivity-test methods)."""

    @pytest.mark.asyncio
    async def test_records_success_true_and_returns_wrapped_result(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call_async,
        )

        writer = _RecordingWriter()
        _install(writer)

        async def _fn():
            return {"ok": True}

        result = await instrument_call_async(
            provider="voyageai",
            call_type="embed",
            model="voyage-code-3",
            item_count=1,
            token_count=0,
            batch_size=1,
            purpose="key_test",
            fn=_fn,
        )

        assert result == {"ok": True}
        assert len(writer.records) == 1
        assert writer.records[0].success is True
        assert writer.records[0].purpose == "key_test"


class TestInstrumentCallAsyncFailure:
    @pytest.mark.asyncio
    async def test_records_success_false_and_reraises_original_exception(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call_async,
        )

        writer = _RecordingWriter()
        _install(writer)

        async def _boom():
            raise ValueError("vendor error")

        with pytest.raises(ValueError, match="vendor error"):
            await instrument_call_async(
                provider="cohere",
                call_type="embed",
                model="embed-v4.0",
                item_count=1,
                token_count=0,
                batch_size=1,
                purpose="key_test",
                fn=_boom,
            )

        assert len(writer.records) == 1
        assert writer.records[0].success is False

    @pytest.mark.asyncio
    async def test_writer_record_exception_does_not_mask_success_result(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call_async,
        )

        _install(_RaisingWriter())

        async def _fn():
            return "real-result"

        result = await instrument_call_async(
            provider="voyageai",
            call_type="embed",
            model="voyage-code-3",
            item_count=1,
            token_count=0,
            batch_size=1,
            purpose="key_test",
            fn=_fn,
        )
        assert result == "real-result"


class TestInstrumentCallStatsPurposeOverride:
    """LOW-2 (#1418 code review): embedding_cache_audit.py's "on"-mode
    cache-shadow-audit re-embed deliberately makes a real live vendor call
    via the ordinary query-embedding code path (purpose="query" hardcoded
    deep in voyage_ai.py/cohere_embedding.py, derived from an internal
    retry flag with no existing caller-supplied purpose channel). This
    context manager lets a caller several layers above instrument_call()
    override the recorded purpose WITHOUT touching either provider's
    call chain or the abstract EmbeddingProvider interface -- zero blast
    radius to the ordinary query/index call sites, which never activate it.
    """

    def test_override_context_replaces_purpose_kwarg(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call,
            stats_purpose_override,
        )

        writer = _RecordingWriter()
        _install(writer)

        with stats_purpose_override("cache_shadow_audit"):
            instrument_call(
                provider="voyageai",
                call_type="embed",
                model="voyage-code-3",
                item_count=1,
                token_count=1,
                batch_size=1,
                purpose="query",
                fn=lambda: "ok",
            )

        assert writer.records[0].purpose == "cache_shadow_audit"

    def test_no_active_override_keeps_original_purpose(self):
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
            purpose="query",
            fn=lambda: "ok",
        )

        assert writer.records[0].purpose == "query"

    def test_override_resets_after_context_exits(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call,
            stats_purpose_override,
        )

        writer = _RecordingWriter()
        _install(writer)

        with stats_purpose_override("cache_shadow_audit"):
            pass  # override active only inside this now-closed block

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

        assert writer.records[0].purpose == "index"

    @pytest.mark.asyncio
    async def test_async_override_context_replaces_purpose_kwarg(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call_async,
            stats_purpose_override,
        )

        writer = _RecordingWriter()
        _install(writer)

        async def _fn():
            return "ok"

        with stats_purpose_override("cache_shadow_audit"):
            await instrument_call_async(
                provider="cohere",
                call_type="embed",
                model="embed-v4.0",
                item_count=1,
                token_count=1,
                batch_size=1,
                purpose="query",
                fn=_fn,
            )

        assert writer.records[0].purpose == "cache_shadow_audit"


class TestInstrumentCallLatencyMeasurement:
    def test_latency_ms_reflects_actual_call_duration(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call,
        )

        writer = _RecordingWriter()
        _install(writer)

        def _slow():
            time.sleep(0.05)
            return "done"

        instrument_call(
            provider="voyageai",
            call_type="embed",
            model="voyage-code-3",
            item_count=1,
            token_count=1,
            batch_size=1,
            purpose="index",
            fn=_slow,
        )

        assert writer.records[0].latency_ms >= 40
