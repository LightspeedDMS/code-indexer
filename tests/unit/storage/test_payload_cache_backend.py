"""
Tests for PayloadCacheBackend Protocol, PayloadCacheSqliteBackend, and wiring (Story #504).

TDD approach: tests written BEFORE implementation.

Covers:
- AC1: PayloadCacheBackend Protocol is runtime-checkable with correct methods
- AC2: PayloadCacheSqliteBackend satisfies Protocol and implements all methods correctly
        - store/retrieve round-trip
        - retrieve expired entry returns None
        - cleanup_expired returns count of deleted rows
        - node_id stored and retrievable
- AC3: BackendRegistry has payload_cache field; StorageFactory creates it in SQLite mode
- AC4: PostgreSQL backend module exists and satisfies Protocol (no live DB required)
"""

import os
import time

import pytest


# ---------------------------------------------------------------------------
# AC1: Protocol definition
# ---------------------------------------------------------------------------


class TestPayloadCacheBackendProtocol:
    """Tests for the PayloadCacheBackend Protocol definition (AC1)."""

    def test_payload_cache_backend_protocol_is_runtime_checkable(self):
        """PayloadCacheBackend must be decorated with @runtime_checkable."""
        from code_indexer.server.storage.protocols import PayloadCacheBackend

        # A protocol decorated with @runtime_checkable allows isinstance() checks.
        assert hasattr(PayloadCacheBackend, "__protocol_attrs__") or hasattr(
            PayloadCacheBackend, "_is_protocol"
        ), "PayloadCacheBackend must be a Protocol"

        # Verify isinstance check works (does not raise TypeError).
        class NotABackend:
            pass

        try:
            isinstance(NotABackend(), PayloadCacheBackend)
        except TypeError:
            pytest.fail(
                "isinstance() raised TypeError — PayloadCacheBackend is not @runtime_checkable"
            )

    def test_payload_cache_backend_protocol_has_required_methods(self):
        """PayloadCacheBackend Protocol must declare all required methods."""
        from code_indexer.server.storage.protocols import PayloadCacheBackend

        protocol_methods = dir(PayloadCacheBackend)

        assert "store" in protocol_methods, "PayloadCacheBackend must have store()"
        assert (
            "retrieve" in protocol_methods
        ), "PayloadCacheBackend must have retrieve()"
        assert (
            "cleanup_expired" in protocol_methods
        ), "PayloadCacheBackend must have cleanup_expired()"
        assert "close" in protocol_methods, "PayloadCacheBackend must have close()"


# ---------------------------------------------------------------------------
# AC2: SQLite Backend Implementation
# ---------------------------------------------------------------------------


class TestPayloadCacheSqliteBackend:
    """Tests for PayloadCacheSqliteBackend implementation (AC2)."""

    @pytest.fixture
    def db_path(self, tmp_path):
        """Provide a temp directory path for the test database."""
        return str(tmp_path / "test_payload_cache.db")

    @pytest.fixture
    def backend(self, db_path):
        """Create a fresh PayloadCacheSqliteBackend for each test."""
        from code_indexer.server.storage.sqlite_backends import (
            PayloadCacheSqliteBackend,
        )

        b = PayloadCacheSqliteBackend(db_path)
        yield b
        b.close()

    def test_payload_cache_sqlite_backend_satisfies_protocol(self, db_path):
        """isinstance(PayloadCacheSqliteBackend(...), PayloadCacheBackend) must be True."""
        from code_indexer.server.storage.sqlite_backends import (
            PayloadCacheSqliteBackend,
        )
        from code_indexer.server.storage.protocols import PayloadCacheBackend

        backend = PayloadCacheSqliteBackend(db_path)
        assert isinstance(backend, PayloadCacheBackend), (
            "PayloadCacheSqliteBackend must satisfy the PayloadCacheBackend Protocol. "
            "Check that all protocol methods are implemented with matching signatures."
        )
        backend.close()

    def test_store_and_retrieve_round_trip(self, backend):
        """store() then retrieve() must return matching content and preview."""
        cache_handle = "test-handle-001"
        content = "Full content of the cached entry"
        preview = "Full content"
        ttl_seconds = 300

        backend.store(
            cache_handle=cache_handle,
            content=content,
            preview=preview,
            ttl_seconds=ttl_seconds,
            node_id=None,
        )

        result = backend.retrieve(cache_handle)

        assert result is not None, "retrieve() must return a dict for a valid handle"
        assert result["content"] == content
        assert result["preview"] == preview
        assert "created_at" in result

    def test_retrieve_nonexistent_handle_returns_none(self, backend):
        """retrieve() must return None for a handle that was never stored."""
        result = backend.retrieve("no-such-handle-xyz")
        assert result is None

    def test_retrieve_expired_entry_returns_none(self, backend):
        """retrieve() must return None when the entry has exceeded its TTL."""
        cache_handle = "expired-handle"
        content = "This will expire"
        preview = "This will"
        # TTL of 1 second
        ttl_seconds = 1

        backend.store(
            cache_handle=cache_handle,
            content=content,
            preview=preview,
            ttl_seconds=ttl_seconds,
            node_id=None,
        )

        # Wait for TTL to expire
        time.sleep(1.1)

        result = backend.retrieve(cache_handle)
        assert result is None, "retrieve() must return None for an expired entry"

    def test_retrieve_non_expired_entry_returns_data(self, backend):
        """retrieve() must return data for an entry within its TTL."""
        cache_handle = "fresh-handle"
        content = "Fresh content"
        preview = "Fresh"
        ttl_seconds = 300  # Long enough to not expire

        backend.store(
            cache_handle=cache_handle,
            content=content,
            preview=preview,
            ttl_seconds=ttl_seconds,
            node_id=None,
        )

        result = backend.retrieve(cache_handle)
        assert result is not None
        assert result["content"] == content

    def test_cleanup_expired_deletes_expired_entries(self, backend):
        """cleanup_expired() must delete expired entries and return the count."""
        # Store 3 entries: 2 with short TTL, 1 with long TTL
        for i in range(2):
            backend.store(
                cache_handle=f"short-ttl-{i}",
                content=f"Short content {i}",
                preview=f"Short {i}",
                ttl_seconds=1,  # Expires immediately
                node_id=None,
            )

        backend.store(
            cache_handle="long-ttl",
            content="Long content",
            preview="Long",
            ttl_seconds=300,  # Will not expire
            node_id=None,
        )

        # Wait for short TTL entries to expire
        time.sleep(1.1)

        deleted = backend.cleanup_expired()
        assert deleted == 2, f"Expected 2 expired entries deleted, got {deleted}"

        # The long-TTL entry must still be retrievable
        result = backend.retrieve("long-ttl")
        assert result is not None, "Long-TTL entry must still exist after cleanup"

    def test_cleanup_expired_returns_zero_when_nothing_expired(self, backend):
        """cleanup_expired() must return 0 when no entries have expired."""
        backend.store(
            cache_handle="fresh-1",
            content="Content 1",
            preview="Prev 1",
            ttl_seconds=300,
            node_id=None,
        )

        deleted = backend.cleanup_expired()
        assert deleted == 0

    def test_store_with_node_id(self, backend):
        """store() must accept and persist node_id."""
        cache_handle = "node-handle"
        backend.store(
            cache_handle=cache_handle,
            content="Some content",
            preview="Some",
            ttl_seconds=300,
            node_id="node-cluster-1",
        )

        result = backend.retrieve(cache_handle)
        assert result is not None
        assert result.get("node_id") == "node-cluster-1"

    def test_store_without_node_id(self, backend):
        """store() must work with node_id=None (standalone mode)."""
        cache_handle = "no-node-handle"
        backend.store(
            cache_handle=cache_handle,
            content="Content no node",
            preview="Content",
            ttl_seconds=300,
            node_id=None,
        )

        result = backend.retrieve(cache_handle)
        assert result is not None
        # node_id in result should be None
        assert result.get("node_id") is None

    def test_store_overwrites_existing_handle(self, backend):
        """Storing with the same handle twice must update the entry."""
        cache_handle = "overwrite-handle"
        backend.store(
            cache_handle=cache_handle,
            content="Original content",
            preview="Original",
            ttl_seconds=300,
            node_id=None,
        )
        backend.store(
            cache_handle=cache_handle,
            content="Updated content",
            preview="Updated",
            ttl_seconds=300,
            node_id=None,
        )

        result = backend.retrieve(cache_handle)
        assert result is not None
        assert result["content"] == "Updated content"

    def test_cleanup_expired_does_not_remove_fresh_entries(self, backend):
        """cleanup_expired() must not touch entries with remaining TTL."""
        # Insert 3 fresh entries
        for i in range(3):
            backend.store(
                cache_handle=f"fresh-{i}",
                content=f"Content {i}",
                preview=f"Prev {i}",
                ttl_seconds=300,
                node_id=None,
            )

        deleted = backend.cleanup_expired()
        assert deleted == 0

        # All 3 must still be retrievable
        for i in range(3):
            result = backend.retrieve(f"fresh-{i}")
            assert result is not None, f"Entry fresh-{i} must still exist"

    def test_close_does_not_raise(self, db_path):
        """close() must not raise any exception."""
        from code_indexer.server.storage.sqlite_backends import (
            PayloadCacheSqliteBackend,
        )

        backend = PayloadCacheSqliteBackend(db_path)
        backend.close()  # Should not raise


# ---------------------------------------------------------------------------
# AC3: BackendRegistry and StorageFactory
# ---------------------------------------------------------------------------


class TestBackendRegistryPayloadCacheField:
    """Tests for BackendRegistry.payload_cache field (AC3)."""

    def test_backend_registry_has_payload_cache_field(self):
        """BackendRegistry dataclass must have a 'payload_cache' field."""
        from code_indexer.server.storage.factory import BackendRegistry
        import dataclasses

        fields = {f.name for f in dataclasses.fields(BackendRegistry)}
        assert "payload_cache" in fields, (
            "BackendRegistry must have a 'payload_cache' field. "
            f"Current fields: {fields}"
        )

    def test_storage_factory_creates_payload_cache_backend_sqlite_mode(self, tmp_path):
        """StorageFactory._create_sqlite_backends must produce a valid PayloadCacheBackend."""
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import PayloadCacheBackend

        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)

        registry = StorageFactory._create_sqlite_backends(data_dir)

        assert hasattr(
            registry, "payload_cache"
        ), "BackendRegistry must have .payload_cache attribute"
        assert isinstance(registry.payload_cache, PayloadCacheBackend), (
            f"registry.payload_cache must satisfy PayloadCacheBackend protocol, "
            f"got {type(registry.payload_cache)}"
        )

    def test_storage_factory_payload_cache_backend_is_functional(self, tmp_path):
        """The payload_cache backend from StorageFactory must be able to store and retrieve."""
        from code_indexer.server.storage.factory import StorageFactory

        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)

        registry = StorageFactory._create_sqlite_backends(data_dir)
        cache = registry.payload_cache

        cache.store(
            cache_handle="factory-test-handle",
            content="Factory test content",
            preview="Factory test",
            ttl_seconds=300,
            node_id=None,
        )

        result = cache.retrieve("factory-test-handle")
        assert result is not None
        assert result["content"] == "Factory test content"


# ---------------------------------------------------------------------------
# AC4: PostgreSQL backend module satisfies Protocol
# ---------------------------------------------------------------------------


class TestPayloadCachePostgresBackend:
    """Tests that PayloadCachePostgresBackend module exists and satisfies Protocol."""

    def test_postgres_backend_module_is_importable(self):
        """PayloadCachePostgresBackend must be importable from the postgres package."""
        try:
            from code_indexer.server.storage.postgres.payload_cache_backend import (  # noqa: F401
                PayloadCachePostgresBackend,
            )
        except ImportError as e:
            pytest.fail(
                f"PayloadCachePostgresBackend must be importable without psycopg: {e}"
            )

    def test_postgres_backend_class_has_required_methods(self):
        """PayloadCachePostgresBackend class must define all protocol methods."""
        from code_indexer.server.storage.postgres.payload_cache_backend import (
            PayloadCachePostgresBackend,
        )

        required_methods = {"store", "retrieve", "cleanup_expired", "close"}
        class_methods = set(dir(PayloadCachePostgresBackend))
        missing = required_methods - class_methods
        assert not missing, f"PayloadCachePostgresBackend is missing methods: {missing}"
