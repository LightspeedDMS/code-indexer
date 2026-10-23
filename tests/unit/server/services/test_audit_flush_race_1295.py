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
from typing import Any

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


def _event_record(correlation_id: str):
    import time

    from code_indexer.server.services.search_embed_event_writer import (
        SearchEmbedEventRecord,
    )

    return SearchEmbedEventRecord(
        timestamp=time.time(),
        correlation_id=correlation_id,
        node_id="test-node",
        provider="voyage-ai",
        model="voyage-code-3",
        config_digest="d",
        cache_mode="on",
        outcome="hit",
        role="owner",
        live_batch_id=None,
        embed_key=f"s:d:{correlation_id}",
        long_key=False,
        latency_ms=1,
        shadow_cosine=None,
    )


class _LockEntrySignal:
    """The writer's drain lock, setting *attempted* when a thread starts to
    acquire it: the test's view of flush() blocking on the lock."""

    def __init__(self, lock: Any, attempted: Any) -> None:
        self._lock = lock
        self._attempted = attempted

    def __enter__(self) -> "_LockEntrySignal":
        self._attempted.set()
        self._lock.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._lock.release()


class TestFlushWaitsForInFlightDrain:
    """flush() must also wait for a batch the background drain already took
    off the queue: an empty queue does not mean the rows are committed."""

    def test_flush_returns_only_after_a_concurrent_drain_committed(self, tmp_path):
        import threading

        from code_indexer.server.services.search_embed_event_writer import (
            SearchEmbedEventSqliteBackend,
            SearchEmbedEventWriter,
        )

        inserting = threading.Event()
        release = threading.Event()
        flusher_waiting_or_done = threading.Event()

        class _GatedBackend(SearchEmbedEventSqliteBackend):
            # Real SQLite insert, held open until the test releases it.
            def insert_batch(self, records):
                inserting.set()
                assert release.wait(timeout=10), "insert never released"
                super().insert_batch(records)

        backend = _GatedBackend(str(tmp_path / "flush_in_flight.db"))
        writer = SearchEmbedEventWriter(backend)
        writer.enqueue(_event_record("in-flight-drain"))
        background = threading.Thread(target=writer._drain)  # the 5 s loop's drain
        background.start()
        assert inserting.wait(timeout=10), "background drain never took the record"
        # The background drain already holds the real lock; flush() meets the
        # signalling wrapper around that same lock.
        # ^ why getattr: a writer without the lock must fail on behaviour, not on the seam
        writer._drain_lock = _LockEntrySignal(  # type: ignore[assignment]
            getattr(writer, "_drain_lock", threading.Lock()), flusher_waiting_or_done
        )

        seen_after_flush = []

        def _flush_then_count() -> None:
            writer.flush()
            seen_after_flush.append(
                len(backend.query(correlation_id="in-flight-drain")[0])
            )
            flusher_waiting_or_done.set()

        flusher = threading.Thread(target=_flush_then_count)
        flusher.start()
        assert flusher_waiting_or_done.wait(timeout=10), "flush() never started"
        release.set()
        background.join(timeout=10)
        flusher.join(timeout=10)
        assert seen_after_flush == [1], (
            "flush() returned before the background drain committed its batch"
        )


class TestFlushWritesOnlyWhatWasPendingAtEntry:
    """A sampled audit flushes on the search request path: flush() writes the
    records pending when it started and returns, however fast others enqueue."""

    def test_flush_ends_after_the_records_pending_at_entry_under_continuous_enqueue(
        self, tmp_path
    ):
        import threading

        from code_indexer.server.services.search_embed_event_writer import (
            SearchEmbedEventSqliteBackend,
            SearchEmbedEventWriter,
        )

        pending_at_entry = 1200
        late_per_insert = 600
        max_producing_inserts = 20
        produce = threading.Event()
        produced = threading.Event()
        stop = threading.Event()
        written: list[str] = []
        insert_calls: list[int] = []

        class _CountingBackend(SearchEmbedEventSqliteBackend):
            # Real SQLite insert; while it runs, the producer enqueues more.
            def insert_batch(self, records):
                super().insert_batch(records)
                written.extend(r.correlation_id for r in records)
                insert_calls.append(len(records))
                if len(insert_calls) <= max_producing_inserts:
                    produced.clear()
                    produce.set()
                    assert produced.wait(timeout=10), "producer never enqueued"

        writer = SearchEmbedEventWriter(_CountingBackend(str(tmp_path / "busy.db")))
        for i in range(pending_at_entry):
            writer.enqueue(_event_record(f"early-{i}"))

        def _producer() -> None:
            sent = 0
            for _ in range(max_producing_inserts):
                if not produce.wait(timeout=10) or stop.is_set():
                    return
                produce.clear()
                for _ in range(late_per_insert):
                    writer.enqueue(_event_record(f"late-{sent}"))
                    sent += 1
                produced.set()

        producer = threading.Thread(target=_producer)
        producer.start()
        try:
            writer.flush()
        finally:
            stop.set()
            produce.set()
            producer.join(timeout=10)

        assert written == [f"early-{i}" for i in range(pending_at_entry)], (
            f"flush() wrote {len(written)} records in {len(insert_calls)} inserts; "
            f"expected exactly the {pending_at_entry} pending at entry"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
