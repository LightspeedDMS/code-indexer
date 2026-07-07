"""Unit tests for TemporalMetadataStore as a backend-selecting facade
(Bug #1313 Step 5).

Public class name and constructor signature (collection_path) MUST stay
unchanged so every existing caller (filesystem_vector_store.py,
dashboard_service.py) needs zero changes. Internally, __init__ selects the
backend: SQLite by default, or whatever the registry factory returns when one
is installed (cluster/postgres mode).
"""

import hashlib
import tempfile
from pathlib import Path

import pytest


class TestFacadeDefaultsToSqliteBackend:
    def test_no_factory_uses_sqlite_backend_instance(self):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            clear_temporal_metadata_backend_factory,
        )
        from code_indexer.storage.temporal_metadata_sqlite_backend import (
            TemporalMetadataSqliteBackend,
        )
        from code_indexer.storage.temporal_metadata_store import TemporalMetadataStore

        clear_temporal_metadata_backend_factory()

        with tempfile.TemporaryDirectory() as tmpdir:
            store = TemporalMetadataStore(Path(tmpdir) / "temporal")
            assert isinstance(store._backend, TemporalMetadataSqliteBackend)

    def test_behaves_exactly_as_before_save_and_get(self):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            clear_temporal_metadata_backend_factory,
        )
        from code_indexer.storage.temporal_metadata_store import TemporalMetadataStore

        clear_temporal_metadata_backend_factory()

        with tempfile.TemporaryDirectory() as tmpdir:
            store = TemporalMetadataStore(Path(tmpdir) / "temporal")
            point_id = "project:diff:facade:file.py:0"
            payload = {"commit_hash": "facade", "path": "file.py", "chunk_index": 0}

            hash_prefix = store.save_metadata(point_id, payload)

            assert store.get_point_id(hash_prefix) == point_id
            assert store.count_entries() == 1


class TestFacadeUsesRegisteredFactory:
    def test_factory_set_routes_construction_through_it(self):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            clear_temporal_metadata_backend_factory,
            set_temporal_metadata_backend_factory,
        )
        from code_indexer.storage.temporal_metadata_store import TemporalMetadataStore

        class _FakeBackend:
            def __init__(self, path):
                self.path = path

            def save_metadata_batch(self, rows):
                return []

            def save_metadata(self, point_id, payload):
                return "fake-hash"

            def checkpoint_wal(self):
                pass

            def get_point_id(self, hash_prefix):
                return "fake-point-id"

            def get_metadata(self, hash_prefix):
                return None

            def delete_metadata(self, hash_prefix):
                pass

            def cleanup_stale_metadata(self, valid_hash_prefixes):
                return 0

            def count_entries(self):
                return 42

        created = []

        def _factory(path):
            backend = _FakeBackend(path)
            created.append(backend)
            return backend

        try:
            set_temporal_metadata_backend_factory(_factory)

            collection_path = Path("/fake/collection/path")
            store = TemporalMetadataStore(collection_path)

            assert store._backend is created[0]
            assert store._backend.path == collection_path
            assert store.count_entries() == 42
            assert store.get_point_id("anything") == "fake-point-id"
        finally:
            clear_temporal_metadata_backend_factory()


class TestFacadeCollectionKeyAndHashPrefix:
    def test_collection_key_is_sha256_prefix_of_str_path(self):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            clear_temporal_metadata_backend_factory,
        )
        from code_indexer.storage.temporal_metadata_store import TemporalMetadataStore

        clear_temporal_metadata_backend_factory()

        with tempfile.TemporaryDirectory() as tmpdir:
            collection_path = Path(tmpdir) / "temporal"
            store = TemporalMetadataStore(collection_path)

            expected = hashlib.sha256(str(collection_path).encode()).hexdigest()[:32]
            assert store.collection_key == expected

    def test_generate_hash_prefix_forwards_to_shared_module_function(self):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            clear_temporal_metadata_backend_factory,
        )
        from code_indexer.storage.temporal_metadata_store import (
            TemporalMetadataStore,
            generate_hash_prefix,
        )

        clear_temporal_metadata_backend_factory()

        with tempfile.TemporaryDirectory() as tmpdir:
            store = TemporalMetadataStore(Path(tmpdir) / "temporal")
            point_id = "project:diff:xyz:file.py:2"
            assert store.generate_hash_prefix(point_id) == generate_hash_prefix(
                point_id
            )


@pytest.fixture(autouse=True)
def _clear_registry_after_each_test():
    yield
    from code_indexer.storage.temporal_metadata_backend_registry import (
        clear_temporal_metadata_backend_factory,
    )

    clear_temporal_metadata_backend_factory()
