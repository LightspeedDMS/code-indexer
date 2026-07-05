"""Bug #1181 Perf Fix #2: Async/coalescing last_used touch for QueryEmbeddingCache.

Tests cover:
- record_hit does NOT call backend.touch_last_used synchronously (non-blocking)
- record_hit buffers the touch in memory (coalescing dict)
- Coalescing: N record_hit for the SAME key collapses to ONE entry (latest ts)
- Flush batches+coalesces: multiple distinct keys yields one touch_last_used_batch call
- Lifecycle: start() launches thread; stop() joins + final-flushes remaining touches
- Bounded buffer: exceeding cap triggers early flush (not unbounded growth)
- Fail-open: record_hit never raises; flush/backend failure logs WARNING, no crash
- Approximate-LRU: after record_hit + flush, last_used reflects the touch (eventually)
- Lock-not-held: _touch_buffer_lock is FREE during touch_last_used_batch on periodic flush

Backend/Protocol/PG-specific tests are in
test_query_embedding_cache_async_touch_backends_1181.py.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List, Tuple
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Global-state isolation fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_globals():
    """Clear process-global singletons before and after every test."""
    from code_indexer.server.services import governed_call
    from code_indexer.server.services.coalescer_registry import clear_coalescer_registry
    from code_indexer.server.services.config_service import reset_config_service

    governed_call.clear_query_embedding_cache()
    clear_coalescer_registry()
    reset_config_service()
    yield
    governed_call.clear_query_embedding_cache()
    clear_coalescer_registry()
    reset_config_service()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_qualifier(
    provider: str = "voyage-ai", model: str = "voyage-code-3", dim: int = 4
):
    from code_indexer.server.services.query_embedding_cache import CacheQualifier

    return CacheQualifier(provider=provider, model=model, dimension=dim)


def _make_cache(tmp_path: Path, voyage_mode: str = "shadow"):
    from code_indexer.server.services.query_embedding_cache import QueryEmbeddingCache
    from code_indexer.server.storage.sqlite_backends import (
        QueryEmbeddingCacheSqliteBackend,
    )

    backend = QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec.db"))
    return QueryEmbeddingCache(
        backend=backend,
        enabled=True,
        voyage_mode=voyage_mode,
        cohere_mode=voyage_mode,
        max_entries=10000,
    )


# ---------------------------------------------------------------------------
# 1. record_hit is NON-BLOCKING: does NOT call backend.touch_last_used inline
# ---------------------------------------------------------------------------


class TestRecordHitIsNonBlocking:
    """record_hit MUST NOT invoke backend.touch_last_used synchronously."""

    def test_record_hit_does_not_call_sync_touch(self) -> None:
        """Calling record_hit must NOT trigger backend.touch_last_used inline."""
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        spy_backend = MagicMock()
        spy_backend.total_entries.return_value = 0

        cache = QueryEmbeddingCache(backend=spy_backend, enabled=True)
        qualifier = _make_qualifier()

        cache.record_hit("some-key", qualifier)

        spy_backend.touch_last_used.assert_not_called()

    def test_record_hit_adds_to_buffer(self) -> None:
        """After record_hit, the touch must reside in the internal buffer."""
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        spy_backend = MagicMock()
        spy_backend.total_entries.return_value = 0

        cache = QueryEmbeddingCache(backend=spy_backend, enabled=True)
        qualifier = _make_qualifier()

        cache.record_hit("buf-key", qualifier)

        assert len(cache._touch_buffer) == 1
        assert ("buf-key", "voyage-ai", "voyage-code-3", 4) in cache._touch_buffer

    def test_record_hit_never_raises(self) -> None:
        """record_hit must NEVER raise, even when internal flush would fail."""
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        bad_backend = MagicMock()
        bad_backend.total_entries.return_value = 0
        bad_backend.touch_last_used_batch.side_effect = RuntimeError("DB down")

        cache = QueryEmbeddingCache(backend=bad_backend, enabled=True)
        qualifier = _make_qualifier()

        # Must not raise regardless of backend state
        cache.record_hit("key", qualifier)

    def test_record_hit_source_does_not_call_sync_backend_touch(self) -> None:
        """Source-text guard: record_hit must not call self._backend.touch_last_used directly."""
        import inspect

        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        source = inspect.getsource(QueryEmbeddingCache.record_hit)
        assert "self._backend.touch_last_used(" not in source, (
            "record_hit must not call self._backend.touch_last_used() directly "
            "(the touch must be buffered for async flush)"
        )


# ---------------------------------------------------------------------------
# 2. Coalescing: N hits for same key collapses to ONE buffer entry (latest ts)
# ---------------------------------------------------------------------------


class TestCoalescing:
    """Multiple record_hit calls for the same key must coalesce to one entry."""

    def test_same_key_coalesces_to_one_buffer_entry(self) -> None:
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        spy_backend = MagicMock()
        spy_backend.total_entries.return_value = 0

        cache = QueryEmbeddingCache(backend=spy_backend, enabled=True)
        qualifier = _make_qualifier()

        for _ in range(5):
            cache.record_hit("repeated-key", qualifier)

        assert len(cache._touch_buffer) == 1, (
            "5 hits on the same key must coalesce to 1 buffer entry"
        )

    def test_same_key_buffer_holds_latest_timestamp(self) -> None:
        """The coalesced entry must hold the LATEST timestamp."""
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        spy_backend = MagicMock()
        spy_backend.total_entries.return_value = 0

        cache = QueryEmbeddingCache(backend=spy_backend, enabled=True)
        qualifier = _make_qualifier()

        t_first = 1000.0
        t_last = 2000.0

        with patch("time.time", side_effect=[t_first, t_last]):
            cache.record_hit("key-ts", qualifier)
            cache.record_hit("key-ts", qualifier)

        buf_key = ("key-ts", "voyage-ai", "voyage-code-3", 4)
        stored_ts = cache._touch_buffer[buf_key]
        assert stored_ts == t_last, f"Expected latest ts={t_last}, got {stored_ts}"

    def test_different_keys_produce_separate_buffer_entries(self) -> None:
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        spy_backend = MagicMock()
        spy_backend.total_entries.return_value = 0

        cache = QueryEmbeddingCache(backend=spy_backend, enabled=True)
        qualifier = _make_qualifier()

        cache.record_hit("key-A", qualifier)
        cache.record_hit("key-B", qualifier)
        cache.record_hit("key-C", qualifier)

        assert len(cache._touch_buffer) == 3, (
            "3 distinct keys must yield 3 buffer entries"
        )


# ---------------------------------------------------------------------------
# 3. Flush batches and coalesces: single touch_last_used_batch call
# ---------------------------------------------------------------------------


class TestFlushBatching:
    """_flush_touches() must call touch_last_used_batch ONCE with all buffered items."""

    def test_flush_calls_touch_last_used_batch_once(self) -> None:
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        spy_backend = MagicMock()
        spy_backend.total_entries.return_value = 0

        cache = QueryEmbeddingCache(backend=spy_backend, enabled=True)
        qualifier_v = _make_qualifier("voyage-ai", "voyage-code-3", 4)
        qualifier_c = _make_qualifier("cohere", "embed-v4.0", 4)

        cache.record_hit("k1", qualifier_v)
        cache.record_hit("k2", qualifier_v)
        cache.record_hit("k3", qualifier_c)

        cache._flush_touches()

        spy_backend.touch_last_used_batch.assert_called_once()
        items = spy_backend.touch_last_used_batch.call_args[0][0]
        assert len(items) == 3

    def test_flush_clears_the_buffer(self) -> None:
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        spy_backend = MagicMock()
        spy_backend.total_entries.return_value = 0

        cache = QueryEmbeddingCache(backend=spy_backend, enabled=True)
        qualifier = _make_qualifier()

        cache.record_hit("k1", qualifier)
        cache.record_hit("k2", qualifier)
        cache._flush_touches()

        assert len(cache._touch_buffer) == 0, "buffer must be empty after flush"

    def test_flush_with_empty_buffer_skips_backend(self) -> None:
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        spy_backend = MagicMock()
        spy_backend.total_entries.return_value = 0

        cache = QueryEmbeddingCache(backend=spy_backend, enabled=True)
        cache._flush_touches()

        spy_backend.touch_last_used_batch.assert_not_called()

    def test_flush_items_contain_correct_pk_fields(self) -> None:
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        spy_backend = MagicMock()
        spy_backend.total_entries.return_value = 0

        cache = QueryEmbeddingCache(backend=spy_backend, enabled=True)
        qualifier = _make_qualifier("voyage-ai", "voyage-code-3", 1024)

        t_fixed = 12345.0
        with patch("time.time", return_value=t_fixed):
            cache.record_hit("test-key", qualifier)

        cache._flush_touches()

        items: List[Tuple] = spy_backend.touch_last_used_batch.call_args[0][0]
        assert len(items) == 1
        cache_key, provider, model, dimension, ts = items[0]
        assert cache_key == "test-key"
        assert provider == "voyage-ai"
        assert model == "voyage-code-3"
        assert dimension == 1024
        assert ts == t_fixed


# ---------------------------------------------------------------------------
# 4. Lifecycle: start() / stop() with final flush
# ---------------------------------------------------------------------------


class TestLifecycle:
    """start() launches flush thread; stop() joins + final-flushes pending touches."""

    def test_start_launches_background_thread(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        try:
            cache.start()
            assert cache._flush_thread is not None
            assert cache._flush_thread.is_alive(), (
                "flush thread must be alive after start()"
            )
        finally:
            cache.stop(timeout=5.0)

    def test_stop_joins_thread(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.start()
        cache.stop(timeout=5.0)
        assert not cache._flush_thread.is_alive(), (
            "flush thread must be dead after stop()"
        )

    def test_stop_flushes_remaining_touches(self, tmp_path: Path) -> None:
        """Touches buffered before stop() must be flushed to the DB on stop."""
        import sqlite3

        import numpy as np

        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        db_path = str(tmp_path / "qec_stop.db")
        backend = QueryEmbeddingCacheSqliteBackend(db_path)

        t0 = 1000.0
        blob = np.asarray([1.0, 2.0], dtype="<f4").tobytes()
        backend.upsert("stop-key", "voyage-ai", "vcode3", 2, blob, t0, t0)

        cache = QueryEmbeddingCache(backend=backend, enabled=True)
        qualifier = _make_qualifier("voyage-ai", "vcode3", 2)

        t_hit = 9999.0
        with patch("time.time", return_value=t_hit):
            cache.record_hit("stop-key", qualifier)

        cache.start()
        cache.stop(timeout=5.0)

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT last_used FROM query_embedding_cache WHERE cache_key='stop-key'"
            ).fetchone()

        assert row is not None
        assert row[0] == pytest.approx(t_hit, abs=1e-6), (
            f"Expected last_used={t_hit} after stop() final flush, got {row[0]}"
        )

    def test_start_is_idempotent(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        try:
            cache.start()
            thread1 = cache._flush_thread
            cache.start()
            assert cache._flush_thread is thread1, (
                "start() must be idempotent (no new thread)"
            )
        finally:
            cache.stop(timeout=5.0)


# ---------------------------------------------------------------------------
# 5. Bounded buffer guard
# ---------------------------------------------------------------------------


class TestBoundedBuffer:
    """The touch buffer must not grow unboundedly; cap triggers early flush."""

    def test_buffer_cap_triggers_early_flush(self) -> None:
        """When buffer reaches cap, adding one more entry must trigger an early flush."""
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        flushed_items: List[List[Tuple]] = []

        class SpyBackend(MagicMock):
            def touch_last_used_batch(self, items):
                flushed_items.append(list(items))

        spy = SpyBackend()
        spy.total_entries.return_value = 0

        cache = QueryEmbeddingCache(backend=spy, enabled=True)
        cap = cache._touch_buffer_max_size

        # Fill buffer to cap with distinct keys (no coalescing)
        for i in range(cap):
            qual = _make_qualifier("voyage-ai", "vcode3", i + 1)
            cache.record_hit(f"key-{i}", qual)

        # One more entry beyond cap must trigger a flush
        qual_extra = _make_qualifier("voyage-ai", "vcode3", cap + 1)
        cache.record_hit("key-extra", qual_extra)

        assert len(flushed_items) >= 1, (
            "An early flush must have been triggered when the buffer reached its cap"
        )
        assert len(cache._touch_buffer) <= cap, (
            f"Buffer must not exceed cap={cap}, has {len(cache._touch_buffer)}"
        )


# ---------------------------------------------------------------------------
# 6. Fail-open: flush errors log WARNING, do not crash the thread
# ---------------------------------------------------------------------------


class TestFailOpen:
    """Backend errors during flush must be logged as WARNING and not crash the thread."""

    def test_flush_failure_logs_warning_and_does_not_raise(self, caplog) -> None:
        import logging

        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        bad_backend = MagicMock()
        bad_backend.total_entries.return_value = 0
        bad_backend.touch_last_used_batch.side_effect = RuntimeError("WAL locked")

        cache = QueryEmbeddingCache(backend=bad_backend, enabled=True)
        qualifier = _make_qualifier()
        cache.record_hit("k", qualifier)

        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.server.services.query_embedding_cache",
        ):
            cache._flush_touches()  # must not raise

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) >= 1, (
            "A WARNING must be logged when touch_last_used_batch fails"
        )

    def test_flush_thread_survives_backend_error(self, tmp_path: Path) -> None:
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        bad_backend = MagicMock()
        bad_backend.total_entries.return_value = 0
        bad_backend.touch_last_used_batch.side_effect = RuntimeError("WAL locked")

        cache = QueryEmbeddingCache(backend=bad_backend, enabled=True)
        qualifier = _make_qualifier()
        cache.record_hit("k", qualifier)

        cache.start()
        try:
            time.sleep(0.3)
            assert cache._flush_thread.is_alive(), (
                "Flush thread must stay alive after backend error (fail-open)"
            )
        finally:
            cache.stop(timeout=5.0)


# ---------------------------------------------------------------------------
# 7. Approximate-LRU: after record_hit + flush, last_used reflects the touch
# ---------------------------------------------------------------------------


class TestApproximateLRU:
    """After record_hit + _flush_touches(), DB row must reflect the updated last_used."""

    def test_last_used_updated_after_flush(self, tmp_path: Path) -> None:
        import sqlite3

        import numpy as np

        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        db_path = str(tmp_path / "qec_lru.db")
        backend = QueryEmbeddingCacheSqliteBackend(db_path)

        t0 = 1000.0
        blob = np.asarray([1.0, 2.0], dtype="<f4").tobytes()
        backend.upsert("lru-key", "voyage-ai", "vcode3", 2, blob, t0, t0)

        cache = QueryEmbeddingCache(backend=backend, enabled=True)
        qualifier = _make_qualifier("voyage-ai", "vcode3", 2)

        t_hit = 5000.0
        with patch("time.time", return_value=t_hit):
            cache.record_hit("lru-key", qualifier)

        cache._flush_touches()

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT last_used FROM query_embedding_cache WHERE cache_key='lru-key'"
            ).fetchone()

        assert row is not None
        assert row[0] == pytest.approx(t_hit, abs=1e-6), (
            f"Expected last_used={t_hit} after flush, got {row[0]}"
        )


# ---------------------------------------------------------------------------
# 8. Lock-not-held: _touch_buffer_lock is FREE during touch_last_used_batch
#    on the periodic/final flush path (_flush_touches).
#
#    This is the regression guard for the Bug #1181 HIGH-severity defect
#    found in code review: the original _flush_touches_locked() held the
#    buffer lock ACROSS the backend DB write, re-serializing the hot path.
#
#    Mechanism: a fake backend whose touch_last_used_batch() probes the lock
#    with acquire(blocking=False) and records whether it succeeded.
#    - On the DEFECTIVE code: the lock is held -> acquire fails -> test FAILS.
#    - On the FIXED code: lock released before backend call -> acquire succeeds.
# ---------------------------------------------------------------------------


class TestLockNotHeldDuringBackendWrite:
    """_touch_buffer_lock must NOT be held during touch_last_used_batch on periodic flush."""

    def test_lock_free_during_backend_write_on_periodic_flush(self) -> None:
        """The buffer lock must be released BEFORE touch_last_used_batch is called.

        Uses a fake backend that attempts a non-blocking acquire of the cache's
        _touch_buffer_lock inside touch_last_used_batch.  If the lock is still
        held (defective path), acquire(blocking=False) returns False and the
        assertion fails.  On the fixed path the lock is free and acquire succeeds.
        """
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        lock_was_free_during_backend_call: List[bool] = []

        class LockProbingBackend:
            """Fake backend that probes whether the cache's buffer lock is free."""

            _cache_ref: "QueryEmbeddingCache | None" = None

            def touch_last_used_batch(self, items):
                if self._cache_ref is not None:
                    # Non-blocking acquire: succeeds only if lock is NOT held
                    acquired = self._cache_ref._touch_buffer_lock.acquire(
                        blocking=False
                    )
                    lock_was_free_during_backend_call.append(acquired)
                    if acquired:
                        # Must release what we just acquired
                        self._cache_ref._touch_buffer_lock.release()

            def total_entries(self):
                return 0

            # Stub remaining Protocol methods (not called in this test)
            def lookup(self, *a, **kw):
                return None

            def upsert(self, *a, **kw):
                pass

            def touch_last_used(self, *a, **kw):
                pass

            def prune_to_max(self, *a, **kw):
                pass

            def close(self):
                pass

        probing_backend = LockProbingBackend()
        cache = QueryEmbeddingCache(backend=probing_backend, enabled=True)  # type: ignore[arg-type]
        probing_backend._cache_ref = cache

        qualifier = _make_qualifier()
        cache.record_hit("probe-key", qualifier)

        # Call _flush_touches() — this is the periodic/final flush path
        cache._flush_touches()

        assert len(lock_was_free_during_backend_call) == 1, (
            "touch_last_used_batch must have been called exactly once "
            f"(called {len(lock_was_free_during_backend_call)} times)"
        )
        assert lock_was_free_during_backend_call[0], (
            "_touch_buffer_lock was HELD during touch_last_used_batch on the periodic "
            "flush path (_flush_touches). This re-serializes every concurrent record_hit "
            "call against the DB write latency — the exact defect this fix must eliminate. "
            "Fix: snapshot+clear under the lock, then call the backend OUTSIDE the lock."
        )
