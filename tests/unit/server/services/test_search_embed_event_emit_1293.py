"""Tests for the Story #1293 shared emit helper — emit_embed_event().

Algorithm 3: emit_embed_event(meta) is driven ENTIRELY by the returned
enriched EmbeddingCacheMetadata. It is a documented no-op when:
  - meta.role or meta.outcome is None (not yet classified -- e.g. the Path A
    coalescer construction sites, wired in Story #1293 S1b), or
  - no writer is installed (CLI / solo / pre-lifespan).

When it DOES emit, correlation_id is NEVER null: it reads
get_current_correlation_id() and falls back to a fresh UUID when that
returns None (the MCP wrong-import bug fix + fallback, Story #1293 AC-B1/B2).
"""

from unittest.mock import MagicMock, patch


class TestEmitEmbedEventNoOpBranches:
    def test_noop_when_role_is_none(self):
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
        from code_indexer.server.services.search_embed_event_emit import (
            emit_embed_event,
            set_search_embed_event_writer,
            clear_search_embed_event_writer,
        )

        mock_writer = MagicMock()
        set_search_embed_event_writer(mock_writer)
        try:
            meta = EmbeddingCacheMetadata(outcome="miss", role=None)
            emit_embed_event(meta)
        finally:
            clear_search_embed_event_writer()

        mock_writer.enqueue.assert_not_called()

    def test_noop_when_outcome_is_none(self):
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
        from code_indexer.server.services.search_embed_event_emit import (
            emit_embed_event,
            set_search_embed_event_writer,
            clear_search_embed_event_writer,
        )

        mock_writer = MagicMock()
        set_search_embed_event_writer(mock_writer)
        try:
            meta = EmbeddingCacheMetadata(outcome=None, role="direct")
            emit_embed_event(meta)
        finally:
            clear_search_embed_event_writer()

        mock_writer.enqueue.assert_not_called()

    def test_noop_when_no_writer_installed(self):
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
        from code_indexer.server.services.search_embed_event_emit import (
            emit_embed_event,
            clear_search_embed_event_writer,
        )

        clear_search_embed_event_writer()  # ensure None
        meta = EmbeddingCacheMetadata(
            outcome="miss", role="direct", provider="voyage-ai"
        )
        # Must not raise even though there's nowhere to write.
        emit_embed_event(meta)


class TestEmitEmbedEventCorrelationIdFallback:
    def test_uses_current_correlation_id_when_present(self):
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
        from code_indexer.server.services.search_embed_event_emit import (
            emit_embed_event,
            set_search_embed_event_writer,
            clear_search_embed_event_writer,
        )

        mock_writer = MagicMock()
        set_search_embed_event_writer(mock_writer)
        try:
            with patch(
                "code_indexer.server.services.search_embed_event_emit."
                "get_current_correlation_id",
                return_value="corr-real-123",
            ):
                meta = EmbeddingCacheMetadata(
                    outcome="miss", role="direct", provider="voyage-ai"
                )
                emit_embed_event(meta)
        finally:
            clear_search_embed_event_writer()

        assert mock_writer.enqueue.call_count == 1
        record = mock_writer.enqueue.call_args[0][0]
        assert record.correlation_id == "corr-real-123"

    def test_falls_back_to_uuid_when_correlation_id_is_none(self):
        """Never a null correlation_id — this is the CORE Story #1293 invariant."""
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
        from code_indexer.server.services.search_embed_event_emit import (
            emit_embed_event,
            set_search_embed_event_writer,
            clear_search_embed_event_writer,
        )

        mock_writer = MagicMock()
        set_search_embed_event_writer(mock_writer)
        try:
            with patch(
                "code_indexer.server.services.search_embed_event_emit."
                "get_current_correlation_id",
                return_value=None,
            ):
                meta = EmbeddingCacheMetadata(
                    outcome="miss", role="direct", provider="voyage-ai"
                )
                emit_embed_event(meta)
        finally:
            clear_search_embed_event_writer()

        record = mock_writer.enqueue.call_args[0][0]
        assert record.correlation_id is not None
        assert record.correlation_id != ""
        # UUID4 canonical string length check (36 chars incl. hyphens).
        assert len(record.correlation_id) == 36


class TestEmitEmbedEventRecordFields:
    def test_record_fields_driven_by_meta(self):
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
        from code_indexer.server.services.search_embed_event_emit import (
            emit_embed_event,
            set_search_embed_event_writer,
            clear_search_embed_event_writer,
        )

        mock_writer = MagicMock()
        set_search_embed_event_writer(mock_writer)
        try:
            with patch(
                "code_indexer.server.services.search_embed_event_emit."
                "get_current_correlation_id",
                return_value="corr-abc",
            ):
                meta = EmbeddingCacheMetadata(
                    outcome="hit",
                    role="warm_hit",
                    provider="cohere",
                    model="embed-v4.0",
                    config_digest="digest-1",
                    cache_mode="on",
                    embed_key="s:digest-1:hello",
                    long_key=False,
                    live_batch_id=None,
                    provider_latency_ms=12,
                    shadow_cosine=0.99,
                )
                emit_embed_event(meta)
        finally:
            clear_search_embed_event_writer()

        record = mock_writer.enqueue.call_args[0][0]
        assert record.outcome == "hit"
        assert record.role == "warm_hit"
        assert record.provider == "cohere"
        assert record.model == "embed-v4.0"
        assert record.config_digest == "digest-1"
        assert record.cache_mode == "on"
        assert record.embed_key == "s:digest-1:hello"
        assert record.long_key is False
        assert record.live_batch_id is None
        assert record.latency_ms == 12
        assert record.shadow_cosine == 0.99
        assert record.correlation_id == "corr-abc"
        assert isinstance(record.node_id, str) and record.node_id != ""
        assert isinstance(record.timestamp, float)
