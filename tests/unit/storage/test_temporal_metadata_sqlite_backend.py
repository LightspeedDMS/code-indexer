"""Unit tests for TemporalMetadataSqliteBackend (Bug #1313 Step 3).

Verifies the extracted SQLite backend satisfies TemporalMetadataBackend and
behaves byte-for-byte identically to the original TemporalMetadataStore body
(same schema, same UPSERT semantics, same WAL pragmas).

The pre-existing regression suite (test_temporal_metadata_operations.py,
test_temporal_format_detection.py, test_bug_484_sqlite_locking.py) exercises
the facade (TemporalMetadataStore) and must keep passing unchanged -- those
are the byte-for-byte behavior guard mandated by the plan.
"""

import sqlite3
import tempfile
from pathlib import Path


class TestTemporalMetadataSqliteBackendProtocolCompliance:
    def test_isinstance_check_passes(self):
        from code_indexer.storage.temporal_metadata_backend import (
            TemporalMetadataBackend,
        )
        from code_indexer.storage.temporal_metadata_sqlite_backend import (
            TemporalMetadataSqliteBackend,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            backend = TemporalMetadataSqliteBackend(Path(tmpdir) / "temporal")
            assert isinstance(backend, TemporalMetadataBackend)


class TestTemporalMetadataSqliteBackendBehavior:
    def test_constructor_creates_db_file_with_wal_schema(self):
        from code_indexer.storage.temporal_metadata_sqlite_backend import (
            TemporalMetadataSqliteBackend,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            collection_path = Path(tmpdir) / "temporal"
            TemporalMetadataSqliteBackend(collection_path)

            db_path = collection_path / "temporal_metadata.db"
            assert db_path.exists()

            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='temporal_metadata'"
                )
                assert cursor.fetchone() is not None
            finally:
                conn.close()

    def test_save_metadata_batch_then_get_point_id_round_trips(self):
        from code_indexer.storage.temporal_metadata_sqlite_backend import (
            TemporalMetadataSqliteBackend,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            backend = TemporalMetadataSqliteBackend(Path(tmpdir) / "temporal")

            point_id = "project:diff:abc:file.py:0"
            payload = {"commit_hash": "abc", "path": "file.py", "chunk_index": 0}

            hash_prefixes = backend.save_metadata_batch([(point_id, payload)])

            assert len(hash_prefixes) == 1
            assert backend.get_point_id(hash_prefixes[0]) == point_id

    def test_save_metadata_batch_upserts_same_point_id(self):
        from code_indexer.storage.temporal_metadata_sqlite_backend import (
            TemporalMetadataSqliteBackend,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            backend = TemporalMetadataSqliteBackend(Path(tmpdir) / "temporal")

            point_id = "project:diff:dup:file.py:0"
            backend.save_metadata_batch(
                [(point_id, {"commit_hash": "v1", "path": "file.py", "chunk_index": 0})]
            )
            backend.save_metadata_batch(
                [(point_id, {"commit_hash": "v2", "path": "file.py", "chunk_index": 0})]
            )

            assert backend.count_entries() == 1

    def test_checkpoint_wal_does_not_raise(self):
        from code_indexer.storage.temporal_metadata_sqlite_backend import (
            TemporalMetadataSqliteBackend,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            backend = TemporalMetadataSqliteBackend(Path(tmpdir) / "temporal")
            backend.checkpoint_wal()  # must not raise
