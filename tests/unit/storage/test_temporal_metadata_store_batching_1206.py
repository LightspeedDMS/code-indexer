"""Bug #1206 — FIX 1: TemporalMetadataStore batched-transaction + connection-reuse.

Before the fix, save_metadata() opened a NEW sqlite3.connect() + ran PRAGMAs +
committed (fsync) + closed PER VECTOR.  With 8 threads on one SQLite DB this was
the single biggest bottleneck.

After the fix:
- save_metadata_batch(rows) inserts a list of (point_id, payload) in ONE
  transaction, with ONE connect/commit/close cycle.
- checkpoint_wal() is exposed for the periodic-checkpoint caller.
- All rows committed in a batch are immediately readable by new connections
  (crash-safety: the commit boundary is the batch, not the individual row).

Tests use a REAL SQLite database in a temp directory — no mocks.
"""

import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Tuple

from src.code_indexer.storage.temporal_metadata_store import TemporalMetadataStore


class TestSaveMetadataBatch:
    """Tests for the new save_metadata_batch() API (FIX 1)."""

    def _make_store(
        self, base: str, subdir: str = "code-indexer-temporal"
    ) -> TemporalMetadataStore:
        collection_path = Path(base) / subdir
        return TemporalMetadataStore(collection_path)

    def _make_rows(self, n: int, prefix: str = "abc") -> List[Tuple[str, dict]]:
        return [
            (
                f"project:diff:{prefix}{i:04d}:src/file{i}.py:0",
                {
                    "commit_hash": f"{prefix}{i:04d}",
                    "path": f"src/file{i}.py",
                    "chunk_index": 0,
                },
            )
            for i in range(n)
        ]

    # ------------------------------------------------------------------
    # Test 1: save_metadata_batch returns correct hash prefixes
    # ------------------------------------------------------------------
    def test_save_metadata_batch_returns_hash_prefixes(self):
        """save_metadata_batch returns a 16-char hash prefix for each input row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._make_store(tmpdir)
            rows = self._make_rows(10)

            hash_prefixes = store.save_metadata_batch(rows)

            assert len(hash_prefixes) == 10
            for hp in hash_prefixes:
                assert len(hp) == 16, f"Expected 16-char hash, got {len(hp)}: {hp}"
                assert all(c in "0123456789abcdef" for c in hp), (
                    f"Non-hex char in {hp!r}"
                )

    # ------------------------------------------------------------------
    # Test 2: all rows are actually persisted (readable after commit)
    # ------------------------------------------------------------------
    def test_save_metadata_batch_all_rows_persisted(self):
        """All rows saved in batch are immediately readable via get_point_id."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._make_store(tmpdir)
            rows = self._make_rows(50)

            hash_prefixes = store.save_metadata_batch(rows)

            for i, hp in enumerate(hash_prefixes):
                point_id = rows[i][0]
                retrieved = store.get_point_id(hp)
                assert retrieved == point_id, (
                    f"Row {i}: expected '{point_id}', got '{retrieved}'"
                )

    # ------------------------------------------------------------------
    # Test 3: batch is crash-safe — all rows land via new store instance
    # ------------------------------------------------------------------
    def test_save_metadata_batch_readable_via_new_instance(self):
        """Rows written by save_metadata_batch are readable via a fresh store instance."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._make_store(tmpdir)
            rows = self._make_rows(30)

            hash_prefixes = store.save_metadata_batch(rows)

            # Verify via a fresh store instance (new connection)
            store2 = TemporalMetadataStore(Path(tmpdir) / "code-indexer-temporal")
            for i, hp in enumerate(hash_prefixes):
                assert store2.get_point_id(hp) is not None, (
                    f"Row {i} with hash_prefix {hp} is missing after batch commit"
                )

    # ------------------------------------------------------------------
    # Test 4: concurrent batch calls from multiple threads all succeed
    # ------------------------------------------------------------------
    def test_save_metadata_batch_concurrent_threads_no_data_loss(self):
        """Multiple threads calling save_metadata_batch concurrently all persist data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._make_store(tmpdir)
            n_threads = 4
            rows_per_thread = 25

            def worker(tid: int) -> List[str]:
                rows = [
                    (
                        f"project:diff:th{tid}_{i:04d}:file{i}.py:0",
                        {
                            "commit_hash": f"th{tid}_{i:04d}",
                            "path": f"file{i}.py",
                            "chunk_index": 0,
                        },
                    )
                    for i in range(rows_per_thread)
                ]
                return store.save_metadata_batch(rows)

            with ThreadPoolExecutor(max_workers=n_threads) as pool:
                futures = [pool.submit(worker, tid) for tid in range(n_threads)]
                all_hashes: List[str] = []
                for future in as_completed(futures):
                    all_hashes.extend(future.result())  # raises if worker raised

            assert len(all_hashes) == n_threads * rows_per_thread
            for hp in all_hashes:
                assert store.get_point_id(hp) is not None, f"Missing hash_prefix {hp}"

    # ------------------------------------------------------------------
    # Test 5: WAL checkpoint method exists and executes without error
    # ------------------------------------------------------------------
    def test_wal_checkpoint_method_exists_and_runs(self):
        """checkpoint_wal() method exists on TemporalMetadataStore and runs cleanly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._make_store(tmpdir)
            rows = self._make_rows(5)
            store.save_metadata_batch(rows)

            # Must not raise
            store.checkpoint_wal()

    # ------------------------------------------------------------------
    # Test 6: save_metadata (single-row API) still works after refactor
    # ------------------------------------------------------------------
    def test_save_metadata_single_row_still_works(self):
        """Legacy single-row save_metadata() still persists correctly after FIX 1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._make_store(tmpdir)

            point_id = "project:diff:abc999:legacy.py:0"
            payload = {"commit_hash": "abc999", "path": "legacy.py", "chunk_index": 0}

            hp = store.save_metadata(point_id, payload)
            assert store.get_point_id(hp) == point_id

    # ------------------------------------------------------------------
    # Test 7: empty batch returns empty list without error
    # ------------------------------------------------------------------
    def test_save_metadata_batch_empty_input(self):
        """save_metadata_batch([]) returns [] without errors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._make_store(tmpdir)
            result = store.save_metadata_batch([])
            assert result == []
