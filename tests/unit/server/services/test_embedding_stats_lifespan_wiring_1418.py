"""Tests for embedding_stats_lifespan_wiring.py (Story #1418 Phase 3).

Extracted, independently-testable helpers for the live-server-process
InProcessAsyncWriter lifecycle -- lifespan.py calls these as thin glue
(construct+start at startup, stop at shutdown), mirroring how
install_embedding_stats_writer_from_bootstrap.py is the testable unit for
the child-subprocess side (Phase 2) rather than testing the giant
lifespan() function directly.
"""

_STOP_TIMEOUT_SECONDS = 2.0
_SLOW_INITIAL_FLUSH_INTERVAL_SECONDS = 30.0
_FAST_UPDATED_FLUSH_INTERVAL_SECONDS = 0.02


class _StubBackend:
    def __init__(self):
        self.batches = []

    def insert_batch(self, records: list) -> None:
        self.batches.append(list(records))


class _MutableStubConfigService:
    """A config_service stub whose flush_interval_seconds can be changed
    AFTER construction -- proves the writer re-reads it live on every loop
    cycle rather than caching it once at startup."""

    def __init__(self, flush_interval_seconds: float):
        self.flush_interval_seconds = flush_interval_seconds

    def get_config(self):
        import types

        es = types.SimpleNamespace(flush_interval_seconds=self.flush_interval_seconds)
        return types.SimpleNamespace(embedding_stats_config=es)


class TestStartInProcessEmbeddingStatsWriter:
    def test_returns_a_started_in_process_async_writer(self) -> None:
        from code_indexer.server.services.embedding_stats_lifespan_wiring import (
            start_in_process_embedding_stats_writer,
        )
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        writer = None
        try:
            backend = _StubBackend()
            config_service = _MutableStubConfigService(
                _SLOW_INITIAL_FLUSH_INTERVAL_SECONDS
            )
            writer = start_in_process_embedding_stats_writer(backend, config_service)
            assert isinstance(writer, InProcessAsyncWriter)
            assert writer._thread is not None and writer._thread.is_alive()
        finally:
            if writer is not None:
                writer.stop(timeout=_STOP_TIMEOUT_SECONDS)

    def test_installs_writer_as_the_active_writer(self) -> None:
        from code_indexer.server.services.embedding_stats_lifespan_wiring import (
            start_in_process_embedding_stats_writer,
        )
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
        )

        writer = None
        try:
            backend = _StubBackend()
            config_service = _MutableStubConfigService(
                _SLOW_INITIAL_FLUSH_INTERVAL_SECONDS
            )
            writer = start_in_process_embedding_stats_writer(backend, config_service)
            assert EmbeddingStatsWriter._active is writer
        finally:
            if writer is not None:
                writer.stop(timeout=_STOP_TIMEOUT_SECONDS)
            EmbeddingStatsWriter._active = None


class TestFlushIntervalIsReReadLiveEachCycle:
    def test_provider_closure_reflects_config_mutations_after_construction(
        self,
    ) -> None:
        """Component 6: the in-process writer's flush_interval_provider must
        be a LIVE closure over config_service -- calling it after mutating
        config_service.flush_interval_seconds must reflect the NEW value,
        proving the interval is re-resolved on demand (each loop cycle)
        rather than captured once at construction time. This is a
        deterministic unit-level check (bypasses background-thread sleep
        timing, which cannot be interrupted mid-wait by an external config
        mutation -- that is expected, correct behavior, not a bug)."""
        from code_indexer.server.services.embedding_stats_lifespan_wiring import (
            start_in_process_embedding_stats_writer,
        )

        writer = None
        try:
            backend = _StubBackend()
            config_service = _MutableStubConfigService(
                _SLOW_INITIAL_FLUSH_INTERVAL_SECONDS
            )
            writer = start_in_process_embedding_stats_writer(backend, config_service)
            assert writer._flush_interval_provider is not None

            assert writer._resolve_flush_interval() == (
                _SLOW_INITIAL_FLUSH_INTERVAL_SECONDS
            )
            config_service.flush_interval_seconds = _FAST_UPDATED_FLUSH_INTERVAL_SECONDS
            assert writer._resolve_flush_interval() == (
                _FAST_UPDATED_FLUSH_INTERVAL_SECONDS
            )
        finally:
            if writer is not None:
                writer.stop(timeout=_STOP_TIMEOUT_SECONDS)


class TestStopInProcessEmbeddingStatsWriter:
    def test_stops_the_writer_thread(self) -> None:
        from code_indexer.server.services.embedding_stats_lifespan_wiring import (
            start_in_process_embedding_stats_writer,
            stop_in_process_embedding_stats_writer,
        )

        config_service = _MutableStubConfigService(_SLOW_INITIAL_FLUSH_INTERVAL_SECONDS)
        writer = start_in_process_embedding_stats_writer(_StubBackend(), config_service)
        stop_in_process_embedding_stats_writer(writer)
        assert writer._thread is not None
        assert not writer._thread.is_alive()

    def test_none_writer_does_not_raise(self) -> None:
        from code_indexer.server.services.embedding_stats_lifespan_wiring import (
            stop_in_process_embedding_stats_writer,
        )

        stop_in_process_embedding_stats_writer(None)  # must not raise


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
