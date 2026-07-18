"""Tests for InProcessAsyncWriter's optional flush_interval_provider (Story
#1418 Phase 3).

Component 6 requires the in-process writer to read flush_interval_seconds
LIVE (re-checked every cycle) via get_config_service(). The mechanism is an
OPTIONAL constructor parameter, ``flush_interval_provider``: a zero-arg
callable returning the current interval. When provided, the background loop
calls it each cycle instead of using the static ``flush_interval_seconds``
value; when absent (the default), behavior is byte identical to before this
story (static value, resolved once).
"""

import time


class _StubBackend:
    def __init__(self):
        self.batches = []

    def insert_batch(self, records: list) -> None:
        self.batches.append(list(records))


def _make_call():
    from code_indexer.server.services.embedding_call_stats import EmbeddingCallRecord

    return EmbeddingCallRecord(
        provider="voyageai",
        call_type="embed",
        model="voyage-code-3",
        item_count=1,
        token_count=10,
        batch_size=1,
        purpose="query",
        success=True,
        latency_ms=5,
        occurred_at=time.time(),
    )


class TestFlushIntervalProviderDefaultsToNone:
    def test_no_provider_uses_static_interval(self) -> None:
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        writer = InProcessAsyncWriter(_StubBackend(), flush_interval_seconds=5.0)
        assert writer._flush_interval_provider is None
        assert writer._resolve_flush_interval() == 5.0


class TestFlushIntervalProviderIsUsedWhenPresent:
    def test_resolve_flush_interval_calls_provider(self) -> None:
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        writer = InProcessAsyncWriter(
            _StubBackend(),
            flush_interval_seconds=30.0,
            flush_interval_provider=lambda: 0.01,
        )
        assert writer._resolve_flush_interval() == 0.01

    def test_periodic_loop_honors_a_fast_provider_value(self) -> None:
        """The background loop must actually USE the provider's value for
        its sleep cadence, not just expose it via _resolve_flush_interval."""
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        backend = _StubBackend()
        writer = InProcessAsyncWriter(
            backend,
            flush_interval_seconds=30.0,  # would never flush in time if used
            flush_interval_provider=lambda: 0.02,
        )
        writer.start()
        writer.record(_make_call())
        time.sleep(0.3)
        writer.stop(timeout=2.0)

        assert sum(len(b) for b in backend.batches) == 1


class TestFlushIntervalProviderFailOpen:
    def test_provider_exception_falls_back_to_static_value(self) -> None:
        """Fail-open: a raising provider must not crash _resolve_flush_interval
        -- falls back to the static flush_interval_seconds."""
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        def _broken_provider():
            raise RuntimeError("boom")

        writer = InProcessAsyncWriter(
            _StubBackend(),
            flush_interval_seconds=7.0,
            flush_interval_provider=_broken_provider,
        )
        assert writer._resolve_flush_interval() == 7.0

    def test_background_loop_survives_raising_provider_and_still_flushes(
        self,
    ) -> None:
        """End-to-end fail-open coverage: a raising provider must not crash
        the background daemon thread. The loop must fall back to the
        static flush_interval_seconds and continue draining records."""
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        def _broken_provider():
            raise RuntimeError("boom")

        backend = _StubBackend()
        writer = InProcessAsyncWriter(
            backend,
            flush_interval_seconds=0.05,
            flush_interval_provider=_broken_provider,
        )
        writer.start()
        writer.record(_make_call())
        time.sleep(0.3)
        writer.stop(timeout=2.0)

        assert writer._thread is not None
        assert not writer._thread.is_alive()  # stopped cleanly, never crashed
        assert sum(len(b) for b in backend.batches) == 1


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
