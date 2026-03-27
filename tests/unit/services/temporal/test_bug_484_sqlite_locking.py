"""
Test for Bug #484: Temporal indexer sqlite3 database is locked.

Root Cause: TemporalIndexer processes commits in parallel threads (8 default).
Each thread calls temporal_metadata_store.save_metadata() which does SQLite
INSERT/UPDATE. SQLite single-writer lock causes "database is locked" errors.

Fix: Enable WAL mode and add busy_timeout on the SQLite connection, plus retry
logic around the cursor.execute() call.

Test strategy: Spawn 8+ threads that all call save_metadata() concurrently
and verify no OperationalError is raised.
"""

import tempfile
import threading
import sqlite3
from pathlib import Path

import pytest

from code_indexer.storage.temporal_metadata_store import TemporalMetadataStore


@pytest.mark.slow
class TestBug484SqliteLocking:
    """Verify concurrent writes to TemporalMetadataStore don't raise OperationalError."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.collection_path = Path(self.temp_dir) / "temporal_collection"
        self.collection_path.mkdir(parents=True, exist_ok=True)
        self.store = TemporalMetadataStore(self.collection_path)

    def teardown_method(self):
        import shutil
        import os

        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_concurrent_writes_no_database_locked_error(self):
        """
        8 concurrent threads writing to temporal_metadata_store must not raise
        sqlite3.OperationalError: database is locked.

        This is the exact failure pattern from Bug #484 where TemporalIndexer
        processes commits in parallel threads and each thread calls save_metadata().
        """
        num_threads = 8
        writes_per_thread = 20
        errors = []
        results = []

        def worker(thread_id: int) -> None:
            """Simulate a TemporalIndexer worker thread calling save_metadata()."""
            for i in range(writes_per_thread):
                point_id = f"project:diff:abc{thread_id:02d}{i:03d}:src/file_{thread_id}_{i}.py:0"
                payload = {
                    "commit_hash": f"abc{thread_id:02d}{i:03d}",
                    "path": f"src/file_{thread_id}_{i}.py",
                    "chunk_index": i,
                }
                try:
                    hash_prefix = self.store.save_metadata(point_id, payload)
                    results.append(hash_prefix)
                except sqlite3.OperationalError as e:
                    errors.append(f"Thread {thread_id}, write {i}: {e}")
                except Exception as e:
                    errors.append(
                        f"Thread {thread_id}, write {i} (unexpected): {type(e).__name__}: {e}"
                    )

        threads = [
            threading.Thread(target=worker, args=(t,)) for t in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        assert not errors, (
            f"Bug #484: {len(errors)} concurrent write errors:\n"
            + "\n".join(errors[:10])
        )
        # All writes should have succeeded
        expected_total = num_threads * writes_per_thread
        assert (
            len(results) == expected_total
        ), f"Expected {expected_total} successful writes, got {len(results)}"

    def test_wal_mode_enabled_on_connection(self):
        """
        The temporal_metadata_store must configure WAL journal mode on its
        SQLite connections to allow concurrent readers while writing.

        WAL mode is essential for the fix: without it, any reader blocks writers
        and vice versa, causing 'database is locked' errors under parallelism.
        """
        # Trigger a write to ensure the DB is configured
        self.store.save_metadata(
            "test:diff:abc:src/x.py:0",
            {"commit_hash": "abc", "path": "src/x.py", "chunk_index": 0},
        )

        # Connect to the same database and verify WAL mode is active
        conn = sqlite3.connect(self.store.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode")
            journal_mode = cursor.fetchone()[0]
        finally:
            conn.close()

        assert journal_mode == "wal", (
            f"Bug #484 fix requires WAL journal mode, but got '{journal_mode}'. "
            f"WAL mode allows concurrent reads while one writer holds the lock."
        )
