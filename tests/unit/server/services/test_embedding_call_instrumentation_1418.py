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
from collections import OrderedDict

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
            tokens_from_result=lambda _r: 42,
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
            tokens_from_result=lambda _r: 0,
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
                tokens_from_result=lambda _r: 0,
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
            tokens_from_result=lambda _r: 1,
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
                tokens_from_result=lambda _r: 1,
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
            tokens_from_result=lambda _r: 1,
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
            tokens_from_result=lambda _r: 0,
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
                tokens_from_result=lambda _r: 0,
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
            tokens_from_result=lambda _r: 0,
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
                tokens_from_result=lambda _r: 1,
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
            tokens_from_result=lambda _r: 1,
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
            tokens_from_result=lambda _r: 1,
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
                tokens_from_result=lambda _r: 1,
                batch_size=1,
                purpose="query",
                fn=_fn,
            )

        assert writer.records[0].purpose == "cache_shadow_audit"


_ECI_LOGGER = "code_indexer.server.services.embedding_call_instrumentation"


def _sync_call(fn, tokens_from_result):
    from code_indexer.server.services.embedding_call_instrumentation import (
        instrument_call,
    )

    return instrument_call(
        provider="voyageai",
        call_type="embed",
        model="voyage-code-3",
        item_count=1,
        tokens_from_result=tokens_from_result,
        batch_size=1,
        purpose="index",
        fn=fn,
    )


async def _async_call(fn, tokens_from_result):
    from code_indexer.server.services.embedding_call_instrumentation import (
        instrument_call_async,
    )

    async def _afn():
        return fn()

    return await instrument_call_async(
        provider="voyageai",
        call_type="embed",
        model="voyage-code-3",
        item_count=1,
        tokens_from_result=tokens_from_result,
        batch_size=1,
        purpose="key_test",
        fn=_afn,
    )


def _raise_vendor_error():
    raise RuntimeError("vendor error")


def _broken_extractor(_result):
    raise KeyError("usage")


def _recording_extractor(seen):
    def _extract(result):
        seen.append(result)
        return 23

    return _extract


class TestTokensFromResultContract:
    """Issue #2173: the vendor token count is read from fn()'s result AFTER
    a successful call; a failed call records 0 without calling the
    extractor; an extractor error records 0, warns and never raises."""

    def test_success_records_extractor_result(self):
        writer = _RecordingWriter()
        _install(writer)
        seen: list = []

        result = _sync_call(lambda: "resp", _recording_extractor(seen))

        assert result == "resp"
        assert seen == ["resp"]
        assert [(r.success, r.token_count) for r in writer.records] == [(True, 23)]

    def test_fn_raises_records_zero_and_skips_extractor(self):
        writer = _RecordingWriter()
        _install(writer)
        seen: list = []

        with pytest.raises(RuntimeError, match="vendor error"):
            _sync_call(_raise_vendor_error, _recording_extractor(seen))

        assert seen == []
        assert [(r.success, r.token_count) for r in writer.records] == [(False, 0)]

    def test_extractor_raises_returns_result_records_zero_and_warns(self, caplog):
        writer = _RecordingWriter()
        _install(writer)

        with caplog.at_level("WARNING", logger=_ECI_LOGGER):
            result = _sync_call(lambda: "resp", _broken_extractor)

        assert result == "resp"
        assert [(r.success, r.token_count) for r in writer.records] == [(True, 0)]
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "voyageai" in warnings[0].getMessage()
        assert "KeyError" in warnings[0].getMessage()

    @pytest.mark.asyncio
    async def test_async_success_records_extractor_result(self):
        writer = _RecordingWriter()
        _install(writer)

        result = await _async_call(lambda: "resp", lambda r: 23)

        assert result == "resp"
        assert [(r.success, r.token_count) for r in writer.records] == [(True, 23)]

    @pytest.mark.asyncio
    async def test_async_fn_raises_records_zero_and_skips_extractor(self):
        writer = _RecordingWriter()
        _install(writer)
        seen: list = []

        with pytest.raises(RuntimeError, match="vendor error"):
            await _async_call(_raise_vendor_error, _recording_extractor(seen))

        assert seen == []
        assert [(r.success, r.token_count) for r in writer.records] == [(False, 0)]

    @pytest.mark.asyncio
    async def test_async_extractor_raises_returns_result_records_zero_and_warns(
        self, caplog
    ):
        writer = _RecordingWriter()
        _install(writer)

        with caplog.at_level("WARNING", logger=_ECI_LOGGER):
            result = await _async_call(lambda: "resp", _broken_extractor)

        assert result == "resp"
        assert [(r.success, r.token_count) for r in writer.records] == [(True, 0)]
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "KeyError" in warnings[0].getMessage()


@pytest.fixture(autouse=True)
def _fresh_usage_warning_throttle(monkeypatch):
    from code_indexer.server.services import embedding_call_instrumentation as eci

    monkeypatch.setattr(eci, "_usage_warning_last", OrderedDict(), raising=False)


def _warnings(caplog):
    return [r for r in caplog.records if r.levelname == "WARNING"]


class TestMissingUsage:
    """Issue #2173: a successful response without a readable usage figure
    records 0 and logs a WARNING throttled per (provider, call_type, model),
    so a vendor schema change cannot flood the indexing hot path."""

    def test_throttle_memory_is_bounded_across_many_distinct_keys(
        self, caplog, monkeypatch
    ):
        from code_indexer.server.services import embedding_call_instrumentation as eci

        monkeypatch.setattr(eci, "_USAGE_WARNING_MAX_KEYS", 8, raising=False)
        _install(_RecordingWriter())

        def _call(model: str) -> None:
            eci.instrument_call(
                provider="voyageai",
                call_type="embed",
                model=model,
                item_count=1,
                tokens_from_result=lambda _r: None,
                batch_size=1,
                purpose="index",
                fn=lambda: "resp",
            )

        with caplog.at_level("WARNING", logger=_ECI_LOGGER):
            for i in range(50):
                _call(f"example-model-{i}")
            _call("example-model-49")

        assert len(eci._usage_warning_last) <= 8
        assert len(_warnings(caplog)) == 50

    @pytest.mark.parametrize(
        "body, expected",
        [
            ({"usage": {"total_tokens": 12}}, 12),
            ({"usage": {"total_tokens": 0}}, 0),
            ({}, None),
            ({"usage": None}, None),
            ({"usage": {"total_tokens": True}}, None),
            ({"usage": {"total_tokens": -1}}, None),
            ({"usage": {"total_tokens": "12"}}, None),
            (["not", "a", "dict"], None),
        ],
    )
    def test_voyage_usage_tokens_reads_only_a_non_negative_int(self, body, expected):
        import httpx

        from code_indexer.server.services.embedding_call_instrumentation import (
            voyage_usage_tokens,
        )

        assert voyage_usage_tokens(httpx.Response(200, json=body)) == expected

    @pytest.mark.parametrize(
        "body, expected",
        [
            ({"meta": {"billed_units": {"input_tokens": 2, "image_tokens": 6}}}, 2),
            ({}, None),
            ({"meta": {}}, None),
            ({"meta": {"billed_units": {"search_units": 1}}}, None),
            ({"meta": {"billed_units": {"input_tokens": False}}}, None),
            ({"meta": {"billed_units": {"input_tokens": -3}}}, None),
        ],
    )
    def test_cohere_billed_input_tokens_reads_only_a_non_negative_int(
        self, body, expected
    ):
        import httpx

        from code_indexer.server.services.embedding_call_instrumentation import (
            cohere_billed_input_tokens,
        )

        assert cohere_billed_input_tokens(httpx.Response(200, json=body)) == expected

    @pytest.mark.parametrize(
        "billed_units, expected",
        [({"search_units": 1}, 0), ({"search_units": 1, "input_tokens": 3}, 3)],
    )
    def test_cohere_rerank_billed_tokens_is_zero_without_input_tokens(
        self, billed_units, expected
    ):
        import httpx

        from code_indexer.server.services.embedding_call_instrumentation import (
            cohere_rerank_billed_tokens,
        )

        response = httpx.Response(200, json={"meta": {"billed_units": billed_units}})
        assert cohere_rerank_billed_tokens(response) == expected

    def test_missing_usage_records_zero_and_warns_naming_the_call(self, caplog):
        writer = _RecordingWriter()
        _install(writer)

        with caplog.at_level("WARNING", logger=_ECI_LOGGER):
            result = _sync_call(lambda: "resp", lambda _r: None)

        assert result == "resp"
        assert [(r.success, r.token_count) for r in writer.records] == [(True, 0)]
        [warning] = _warnings(caplog)
        message = warning.getMessage()
        assert "missing" in message
        assert "voyageai" in message
        assert "embed" in message
        assert "voyage-code-3" in message

    def test_repeat_within_interval_is_not_warned_again(self, caplog):
        _install(_RecordingWriter())

        with caplog.at_level("WARNING", logger=_ECI_LOGGER):
            _sync_call(lambda: "resp", lambda _r: None)
            _sync_call(lambda: "resp", lambda _r: None)
            _sync_call(lambda: "resp", _broken_extractor)
            _sync_call(lambda: "resp", _broken_extractor)

        assert len(_warnings(caplog)) == 2

    def test_warns_again_once_the_interval_has_elapsed(self, caplog, monkeypatch):
        from code_indexer.server.services import embedding_call_instrumentation as eci

        monkeypatch.setattr(eci, "_USAGE_WARNING_INTERVAL_SECONDS", 0.0, raising=False)
        _install(_RecordingWriter())

        with caplog.at_level("WARNING", logger=_ECI_LOGGER):
            _sync_call(lambda: "resp", lambda _r: None)
            _sync_call(lambda: "resp", lambda _r: None)

        assert len(_warnings(caplog)) == 2

    @pytest.mark.asyncio
    async def test_async_missing_usage_records_zero_and_warns(self, caplog):
        writer = _RecordingWriter()
        _install(writer)

        with caplog.at_level("WARNING", logger=_ECI_LOGGER):
            result = await _async_call(lambda: "resp", lambda _r: None)

        assert result == "resp"
        assert [(r.success, r.token_count) for r in writer.records] == [(True, 0)]
        assert len(_warnings(caplog)) == 1


def _slow_extractor(_result):
    time.sleep(0.3)
    return 5


class TestLatencyExcludesExtraction:
    """latency_ms measures the vendor call only; the post-call usage parse
    runs after the clock stops."""

    def test_latency_stops_when_fn_returns(self):
        writer = _RecordingWriter()
        _install(writer)

        _sync_call(lambda: "resp", _slow_extractor)

        [rec] = writer.records
        assert rec.token_count == 5
        assert rec.latency_ms < 150

    @pytest.mark.asyncio
    async def test_async_latency_stops_when_fn_returns(self):
        writer = _RecordingWriter()
        _install(writer)

        await _async_call(lambda: "resp", _slow_extractor)

        [rec] = writer.records
        assert rec.token_count == 5
        assert rec.latency_ms < 150


class TestNoOpWriterSkipsExtraction:
    """When nothing is recorded (stats disabled or a plain CLI process, i.e.
    the NoOp writer is active) the post-call usage parse is skipped."""

    def test_default_noop_writer_never_calls_extractor(self):
        seen: list = []

        assert _sync_call(lambda: "resp", _recording_extractor(seen)) == "resp"

        assert seen == []

    def test_installed_noop_writer_never_calls_extractor(self):
        from code_indexer.server.services.embedding_stats_writer import NoOpWriter

        _install(NoOpWriter())
        seen: list = []

        assert _sync_call(lambda: "resp", _recording_extractor(seen)) == "resp"

        assert seen == []

    @pytest.mark.asyncio
    async def test_async_noop_writer_never_calls_extractor(self):
        seen: list = []

        assert await _async_call(lambda: "resp", _recording_extractor(seen)) == "resp"

        assert seen == []


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
            tokens_from_result=lambda _r: 1,
            batch_size=1,
            purpose="index",
            fn=_slow,
        )

        assert writer.records[0].latency_ms >= 40
