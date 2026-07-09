"""Tests for store_batch on PayloadCacheSqliteBackend and PayloadCache facade (Bug #1181).

Bug #1181: Perf Fix #1 - batch per-query payload_cache stores into ONE transaction.

Root cause: _apply_rest_semantic_truncation calls payload_cache.store() once per
result, causing N fsync'd COMMITs per query at concurrency.

Fix: store_batch(contents) -> List[str] inserts all rows in ONE transaction,
returns handles IN ORDER, each immediately retrievable.

TDD: tests written BEFORE implementation.
"""

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# SQLite backend: store_batch
# ---------------------------------------------------------------------------


class TestPayloadCacheSqliteBackendStoreBatch:
    """Tests for store_batch on PayloadCacheSqliteBackend (Bug #1181)."""

    @pytest.fixture
    def db_path(self, tmp_path):
        return str(tmp_path / "test_batch.db")

    @pytest.fixture
    def backend(self, db_path):
        from code_indexer.server.storage.sqlite_backends import (
            PayloadCacheSqliteBackend,
        )

        b = PayloadCacheSqliteBackend(db_path)
        yield b
        b.close()

    def test_store_batch_accepts_entries_and_returns_none(self, backend):
        """store_batch(entries) must accept pre-built tuples and return None."""
        import uuid

        entries = [
            (str(uuid.uuid4()), "content A", "conte", 300),
            (str(uuid.uuid4()), "content B", "conte", 300),
            (str(uuid.uuid4()), "content C", "conte", 300),
        ]
        result = backend.store_batch(entries=entries)
        assert result is None, f"Expected None return, got {result!r}"

    def test_store_batch_each_entry_is_retrievable(self, backend):
        """Each entry stored via store_batch must be immediately retrievable."""
        import uuid

        contents = ["Alpha content", "Beta content", "Gamma content"]
        entries = [(str(uuid.uuid4()), c, c[:5], 300) for c in contents]
        handles = [e[0] for e in entries]
        backend.store_batch(entries=entries)
        for i, handle in enumerate(handles):
            row = backend.retrieve(handle)
            assert row is not None, f"Handle {handle} (index {i}) not found"
            assert row["content"] == contents[i]

    def test_store_batch_preview_stored_as_provided(self, backend):
        """store_batch stores exactly the preview passed in each tuple."""
        import uuid

        h0, h1 = str(uuid.uuid4()), str(uuid.uuid4())
        entries = [
            (h0, "Hello World Extended", "Hello", 300),
            (h1, "Short", "Short", 300),
        ]
        backend.store_batch(entries=entries)
        row0 = backend.retrieve(h0)
        assert row0 is not None
        assert row0["preview"] == "Hello"

        row1 = backend.retrieve(h1)
        assert row1 is not None
        assert row1["preview"] == "Short"

    def test_store_batch_empty_list_is_noop(self, backend):
        """store_batch([]) must return None without error."""
        result = backend.store_batch(entries=[])
        assert result is None

    def test_store_batch_uses_single_execute_atomic_call(self, db_path):
        """store_batch must call execute_atomic exactly ONCE for N items."""
        import uuid

        from code_indexer.server.storage.sqlite_backends import (
            PayloadCacheSqliteBackend,
        )

        backend = PayloadCacheSqliteBackend(db_path)
        atomic_call_count = [0]
        original_execute_atomic = backend._conn_manager.execute_atomic

        def counting_execute_atomic(fn):
            atomic_call_count[0] += 1
            return original_execute_atomic(fn)

        backend._conn_manager.execute_atomic = counting_execute_atomic

        entries = [
            (str(uuid.uuid4()), f"item_{i}" * 10, f"item_{i}"[:10], 300)
            for i in range(5)
        ]
        backend.store_batch(entries=entries)

        assert atomic_call_count[0] == 1, (
            f"Expected 1 execute_atomic call for 5-item batch, got {atomic_call_count[0]}"
        )
        backend.close()

    def test_store_batch_n_items_inserted_in_db(self, backend):
        """store_batch must insert exactly N rows for N entries."""
        import uuid

        contents = [f"content_{i}" for i in range(7)]
        entries = [(str(uuid.uuid4()), c, c[:8], 300) for c in contents]
        handles = [e[0] for e in entries]
        backend.store_batch(entries=entries)

        conn = backend._conn_manager.get_connection()
        placeholders = ",".join("?" * len(handles))
        cursor = conn.execute(
            f"SELECT COUNT(*) FROM payload_cache WHERE cache_handle IN ({placeholders})",
            handles,
        )
        row = cursor.fetchone()
        assert row[0] == 7, f"Expected 7 rows in DB, got {row[0]}"


# ---------------------------------------------------------------------------
# PayloadCache facade: store_batch
# ---------------------------------------------------------------------------


class TestPayloadCacheFacadeStoreBatch:
    """Tests for store_batch on PayloadCache facade (Bug #1181)."""

    @pytest.fixture
    def cache_with_sqlite(self, tmp_path):
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig(preview_size_chars=100, cache_ttl_seconds=300)
        cache = PayloadCache(db_path=tmp_path / "test_facade_batch.db", config=config)
        cache.initialize()
        yield cache
        cache.close()

    def test_facade_store_batch_returns_handles_in_order(self, cache_with_sqlite):
        """PayloadCache.store_batch must return handles in order, one per content."""
        contents = ["A" * 200, "B" * 200, "C" * 200]
        handles = cache_with_sqlite.store_batch(contents)
        assert len(handles) == 3
        assert len(set(handles)) == 3

    def test_facade_store_batch_each_handle_retrievable(self, cache_with_sqlite):
        """Each handle returned by facade.store_batch must be retrievable via retrieve()."""
        contents = ["A" * 200, "B" * 200]
        handles = cache_with_sqlite.store_batch(contents)
        for i, handle in enumerate(handles):
            result = cache_with_sqlite.retrieve(handle, page=0)
            assert result is not None
            # retrieve returns paginated CacheRetrievalResult; page 0 starts at byte 0
            assert result.content.startswith(contents[i][0])

    def test_facade_store_batch_empty_returns_empty(self, cache_with_sqlite):
        """facade.store_batch([]) must return []."""
        handles = cache_with_sqlite.store_batch([])
        assert handles == []

    def test_facade_store_batch_calls_backend_store_batch_once_not_store(
        self, tmp_path
    ):
        """With a backend, facade.store_batch must call backend.store_batch once, not store N times."""
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        mock_backend = MagicMock()
        # store_batch on backend takes entries list; facade generates handles
        mock_backend.store_batch.return_value = None

        config = PayloadCacheConfig(preview_size_chars=50, cache_ttl_seconds=300)
        cache = PayloadCache(
            db_path=tmp_path / "mock.db", config=config, storage_backend=mock_backend
        )
        cache.initialize()

        contents = ["content1" * 10, "content2" * 10, "content3" * 10]
        handles = cache.store_batch(contents)

        assert mock_backend.store_batch.call_count == 1, (
            f"Expected backend.store_batch called once, got {mock_backend.store_batch.call_count}"
        )
        assert mock_backend.store.call_count == 0, (
            f"Expected backend.store NOT called, got {mock_backend.store.call_count}"
        )
        assert len(handles) == 3

    def test_facade_store_batch_backend_receives_correct_entries(self, tmp_path):
        """Facade passes (handle, content, preview, ttl) tuples to backend.store_batch."""
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        stored_entries = []

        class CapturingBackend:
            def store_batch(self, entries, ttl_seconds=None):
                stored_entries.extend(entries)

            def store(self, *a, **kw):
                raise AssertionError(
                    "store() must not be called when store_batch is available"
                )

            def retrieve(self, handle):
                return None

            def cleanup_expired(self):
                return 0

            def close(self):
                pass

        config = PayloadCacheConfig(preview_size_chars=10, cache_ttl_seconds=300)
        cache = PayloadCache(
            db_path=tmp_path / "cap.db",
            config=config,
            storage_backend=CapturingBackend(),
        )
        cache.initialize()

        contents = ["Hello World Extended", "Short"]
        handles = cache.store_batch(contents)

        assert len(stored_entries) == 2
        h0, c0, p0, ttl0 = stored_entries[0]
        assert c0 == "Hello World Extended"
        assert p0 == "Hello Worl"  # first 10 chars
        assert ttl0 == 300

        h1, c1, p1, ttl1 = stored_entries[1]
        assert c1 == "Short"
        assert p1 == "Short"
        assert ttl1 == 300

        assert handles[0] == h0
        assert handles[1] == h1


# ---------------------------------------------------------------------------
# H1: Solo-server wiring — PayloadCache(storage_backend=PayloadCacheSqliteBackend)
# This is the exact lifespan solo-server wiring that Bug #1181 C1 silently broke.
# ---------------------------------------------------------------------------


class TestSoloServerWiringStoreBatch:
    """Test PayloadCache with real SQLite backend wired as storage_backend.

    This matches the solo-server lifespan wiring:
      service_init.py:341 builds BackendRegistry even for sqlite
      factory.py:232 -> PayloadCacheSqliteBackend
      lifespan.py:886 sets storage_backend = backend_registry.payload_cache

    Before the fix: store_batch() raises TypeError (wrong signature) -> swallowed
    by fail-open -> all cache_handles are None -> pagination dead.
    After the fix: store_batch() returns handles, each is retrievable.
    """

    @pytest.fixture
    def solo_server_cache(self, tmp_path):
        """Mimics the solo-server lifespan wiring exactly."""
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )
        from code_indexer.server.storage.sqlite_backends import (
            PayloadCacheSqliteBackend,
        )

        backend_db = str(tmp_path / "backend.db")
        backend = PayloadCacheSqliteBackend(backend_db)

        config = PayloadCacheConfig(preview_size_chars=100, cache_ttl_seconds=300)
        # storage_backend=backend is the solo-server wiring path
        cache = PayloadCache(
            db_path=tmp_path / "facade.db",
            config=config,
            storage_backend=backend,
        )
        cache.initialize()
        yield cache, backend
        cache.close()
        backend.close()

    def test_solo_server_store_batch_returns_two_handles(self, solo_server_cache):
        """store_batch returns 2 handles for 2 large contents — C1 regression."""
        cache, _ = solo_server_cache
        contents = ["x" * 5000, "y" * 5000]
        handles = cache.store_batch(contents)
        assert len(handles) == 2, (
            f"Expected 2 handles, got {len(handles)}. "
            "Signature mismatch between facade and SQLite backend still present."
        )
        assert len(set(handles)) == 2, "Handles must be unique"
        for h in handles:
            assert isinstance(h, str) and len(h) > 0

    def test_solo_server_store_batch_each_handle_retrievable(self, solo_server_cache):
        """Each handle from store_batch is immediately retrievable — C1 regression."""
        cache, backend = solo_server_cache
        contents = ["x" * 5000, "y" * 5000]
        handles = cache.store_batch(contents)
        for i, handle in enumerate(handles):
            row = backend.retrieve(handle)
            assert row is not None, (
                f"Handle {handle} (index {i}) not retrievable after store_batch. "
                "C1: TypeError was silently swallowed, handle was never stored."
            )
            assert row["content"] == contents[i]

    def test_solo_server_store_batch_preview_correct(self, solo_server_cache):
        """Preview stored correctly in SQLite backend via solo-server wiring."""
        cache, backend = solo_server_cache
        contents = ["Hello World Extended Content", "Short"]
        handles = cache.store_batch(contents)
        row0 = backend.retrieve(handles[0])
        assert row0 is not None
        assert row0["preview"] == "Hello World Extended Content"[:100]

        row1 = backend.retrieve(handles[1])
        assert row1 is not None
        assert row1["preview"] == "Short"

    def test_solo_server_store_batch_empty_is_noop(self, solo_server_cache):
        """store_batch([]) returns [] in solo-server wiring."""
        cache, _ = solo_server_cache
        handles = cache.store_batch([])
        assert handles == []

    def test_backend_signature_matches_protocol(self):
        """Both backends expose the same store_batch signature (no silent divergence)."""
        import inspect

        from code_indexer.server.storage.postgres.payload_cache_backend import (
            PayloadCachePostgresBackend,
        )
        from code_indexer.server.storage.sqlite_backends import (
            PayloadCacheSqliteBackend,
        )

        sqlite_sig = inspect.signature(PayloadCacheSqliteBackend.store_batch)
        pg_sig = inspect.signature(PayloadCachePostgresBackend.store_batch)

        sqlite_params = list(sqlite_sig.parameters.keys())
        pg_params = list(pg_sig.parameters.keys())

        # Both must have: self, entries, node_id
        assert "entries" in sqlite_params, (
            f"SQLite store_batch missing 'entries' param. Got: {sqlite_params}"
        )
        assert "entries" in pg_params, (
            f"PG store_batch missing 'entries' param. Got: {pg_params}"
        )
        assert "node_id" in sqlite_params, (
            f"SQLite store_batch missing 'node_id' param. Got: {sqlite_params}"
        )
        assert "node_id" in pg_params, (
            f"PG store_batch missing 'node_id' param. Got: {pg_params}"
        )
