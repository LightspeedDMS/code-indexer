"""Story #1295 (Epic #1288 final): audit-persistence flush race, discovered
via this story's mandatory front-door E2E against a real isolated server.

Bug reproduced live: a sampled audit's ``update_audit_by_key`` UPDATE ran
SYNCHRONOUSLY immediately after the original decision event was enqueued via
``emit_embed_event()`` -- but ``SearchEmbedEventWriter``'s background drain
thread only flushes the queue into the DB every 5 seconds (or on an explicit
``flush()`` call). Within a single HTTP request, the audit fires milliseconds
after the original event is enqueued, so the UPDATE's WHERE clause
(correlation_id, embed_key) matched ZERO rows -- the original INSERT had not
yet landed in the table. Server log evidence:
    "SearchEmbedEventSqliteBackend: update_audit_by_key matched 0 rows
     (expected 1) for correlation_id=... embed_key=..."

Fix: ``_record_audit_metrics`` must flush the writer's queue BEFORE calling
``update_audit_by_key``, guaranteeing the original row is durably present.

This test uses REAL components throughout -- no mocking of the writer, the
backend, or the SQLite file -- to reproduce the exact race and prove the fix.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _make_real_writer(tmp_path: Path):
    from code_indexer.server.services.search_embed_event_writer import (
        SearchEmbedEventSqliteBackend,
        SearchEmbedEventWriter,
    )

    db_path = str(tmp_path / "search_embed_event_race.db")
    backend = SearchEmbedEventSqliteBackend(db_path)
    writer = SearchEmbedEventWriter(backend)
    # Deliberately do NOT call writer.start() -- the background drain thread
    # must NEVER be relied upon to make the race disappear by chance; the
    # fix must flush synchronously regardless of the drain thread's state.
    return writer, backend


class TestAuditUpdateFlushesBeforeQuery:
    """_record_audit_metrics must flush the writer's queue before issuing
    the keyed UPDATE, so a same-request sampled audit reliably finds the
    row it is trying to stamp."""

    def test_sampled_audit_populates_row_without_manual_flush(
        self, tmp_path, monkeypatch
    ):
        """Reproduces the live E2E race: enqueue the original decision event
        (NOT flushed), immediately call _record_audit_metrics with the SAME
        (correlation_id, embed_key) -- the row must end up stamped, proving
        _record_audit_metrics flushes internally before the UPDATE.
        """
        from code_indexer.server.services.embedding_cache_audit import (
            _record_audit_metrics,
        )
        from code_indexer.server.services.search_embed_event_emit import (
            set_search_embed_event_writer,
            clear_search_embed_event_writer,
        )
        from code_indexer.server.services.search_embed_event_writer import (
            SearchEmbedEventRecord,
        )
        from code_indexer.server.telemetry.correlation_bridge import (
            set_current_correlation_id,
        )

        writer, backend = _make_real_writer(tmp_path)
        set_search_embed_event_writer(writer)
        correlation_id = "race-test-correlation-id"
        embed_key = "s:d:race-test-query"
        try:
            set_current_correlation_id(correlation_id)

            # Enqueue the ORIGINAL decision event (mirrors emit_embed_event's
            # real production call) -- deliberately NOT flushed, exactly like
            # the live race: the background drain thread has not run yet.
            record = SearchEmbedEventRecord(
                timestamp=0.0,
                correlation_id=correlation_id,
                node_id="test-node",
                provider="voyage-ai",
                model="voyage-code-3",
                config_digest="d",
                cache_mode="shadow",
                outcome="shadow_hit",
                role="owner",
                live_batch_id=None,
                embed_key=embed_key,
                long_key=False,
                latency_ms=10,
                shadow_cosine=None,
            )
            writer.enqueue(record)

            # Immediately (no manual flush!) run the audit -- this must
            # internally flush before its UPDATE for the row to be found.
            _record_audit_metrics(
                primary_candidate_ids=["a", "b", "c"],
                second_ids=["a", "b", "d"],
                provider_name="voyage-ai",
                mode="shadow",
                embed_key=embed_key,
            )

            events, _total = backend.query(correlation_id=correlation_id)
            assert len(events) == 1, (
                f"expected exactly 1 row for correlation_id={correlation_id}, "
                f"got {len(events)}"
            )
            row = events[0]
            assert row["audit_sampled"] == 1, (
                "audit_sampled must be stamped True on the row -- if this is "
                "0/None, _record_audit_metrics's UPDATE ran before the "
                "original INSERT was flushed to the DB (the race)"
            )
            assert row["audit_cosine"] is not None, (
                "audit_cosine must be populated -- same race as audit_sampled"
            )
            assert abs(row["audit_cosine"] - (2 / 3)) < 1e-9, (
                f"expected overlap 2/3, got {row['audit_cosine']}"
            )
        finally:
            clear_search_embed_event_writer()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
