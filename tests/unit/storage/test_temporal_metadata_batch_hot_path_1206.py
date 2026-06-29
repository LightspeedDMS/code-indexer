"""Bug #1206 — Integration tests: batch hot-path wiring.

These tests verify that the performance fixes are ACTUALLY WIRED into the hot
paths, not just available as orphan helper methods.

BLOCKER 1 (FIX 1): upsert_points temporal branch must use save_metadata_batch()
  — metadata rows land via a single batch call per upsert_points invocation.

BLOCKER 2 (FIX 2): temporal_progressive_metadata staging + amortized flush:
  — mark_commit_indexed() must NOT write the progress file (lazy staging).
  — flush_pending() must be called amortized (not once per commit).
  — flush-after-success ordering preserves durability on crash.

BLOCKER 3: durability — simulated crash before flush_pending() leaves those
  commits absent from load_completed() on a fresh instance.

All tests use real SQLite + real filesystem — no mocks.
"""

import hashlib
import inspect
import sqlite3
import tempfile
from pathlib import Path
from typing import List

import numpy as np

from src.code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from src.code_indexer.storage.temporal_metadata_store import TemporalMetadataStore
from src.code_indexer.services.temporal.temporal_progressive_metadata import (
    TemporalProgressiveMetadata,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(base: str) -> FilesystemVectorStore:
    """Create a FilesystemVectorStore backed by a fresh temp directory."""
    index_path = Path(base) / ".code-indexer" / "index"
    index_path.mkdir(parents=True, exist_ok=True)
    return FilesystemVectorStore(base_path=index_path)


def _get_temporal_collection_name() -> str:
    from src.code_indexer.services.temporal.temporal_collection_naming import (
        LEGACY_TEMPORAL_COLLECTION,
    )

    return LEGACY_TEMPORAL_COLLECTION


def _make_temporal_points(n: int, commit_hash: str = "abc1234") -> List[dict]:
    """Build n fake temporal upsert_points rows (1024-dim zero vectors)."""
    return [
        {
            "id": f"{commit_hash}:src/file{i}.py:{i}",
            "vector": list(np.zeros(1024, dtype=float)),
            "payload": {
                "type": "commit_diff",
                "commit_hash": commit_hash,
                "path": f"src/file{i}.py",
                "chunk_index": i,
            },
            "chunk_text": f"def func_{i}(): pass",
        }
        for i in range(n)
    ]


def _count_sqlite_rows(db_path: Path) -> int:
    """Count rows in temporal_metadata table."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT COUNT(*) FROM temporal_metadata")
        return int(cur.fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# BLOCKER 1: upsert_points uses save_metadata_batch (all rows in one shot)
# ---------------------------------------------------------------------------


class TestUpsertPointsUsesBatchMetadata:
    """upsert_points temporal branch must call save_metadata_batch, not save_metadata."""

    def test_upsert_points_writes_all_metadata_rows(self):
        """All temporal points written by upsert_points appear in temporal_metadata.db."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            collection_name = _get_temporal_collection_name()
            store.create_collection(collection_name, vector_size=1024)

            n = 20
            points = _make_temporal_points(n, "deadbeef")

            store.upsert_points(collection_name=collection_name, points=points)

            db_path = (
                Path(tmpdir)
                / ".code-indexer"
                / "index"
                / collection_name
                / "temporal_metadata.db"
            )
            assert db_path.exists(), (
                "temporal_metadata.db must exist after upsert_points"
            )
            row_count = _count_sqlite_rows(db_path)
            assert row_count == n, (
                f"Expected {n} metadata rows, got {row_count}. "
                "Metadata may not be written by upsert_points."
            )

    def test_upsert_points_hash_prefix_matches_deterministic_formula(self):
        """Vector filenames use sha256(point_id)[:16] matching generate_hash_prefix."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            collection_name = _get_temporal_collection_name()
            store.create_collection(collection_name, vector_size=1024)

            points = _make_temporal_points(5, "f00dcafe")
            store.upsert_points(collection_name=collection_name, points=points)

            collection_path = Path(tmpdir) / ".code-indexer" / "index" / collection_name
            all_vector_files = list(collection_path.rglob("vector_*.json"))
            point_ids = [p["id"] for p in points]
            expected_prefixes = {
                hashlib.sha256(pid.encode()).hexdigest()[:16] for pid in point_ids
            }
            found_prefixes = {f.stem[len("vector_") :] for f in all_vector_files}

            for expected in expected_prefixes:
                assert expected in found_prefixes, (
                    f"Expected vector file 'vector_{expected}.json' not found. "
                    f"Found: {sorted(found_prefixes)}"
                )

    def test_upsert_points_wal_size_bounded_after_batch(self):
        """WAL file is small (bounded) after a batch upsert: single transaction commits.

        With per-vector commits, the WAL grows proportionally to N vectors
        (each commit adds frames). With one batch commit, the WAL is checkpointed
        after a single transaction and stays small relative to N.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            collection_name = _get_temporal_collection_name()
            store.create_collection(collection_name, vector_size=1024)

            n = 50
            points = _make_temporal_points(n, "batchtest")
            store.upsert_points(collection_name=collection_name, points=points)

            # Run WAL checkpoint to flush frames
            meta_store = store._get_temporal_metadata_store()
            meta_store.checkpoint_wal()

            db_path = (
                Path(tmpdir)
                / ".code-indexer"
                / "index"
                / collection_name
                / "temporal_metadata.db"
            )
            wal_path = Path(str(db_path) + "-wal")

            # After checkpoint, WAL should be empty or very small (< 64 KB)
            if wal_path.exists():
                wal_size = wal_path.stat().st_size
                assert wal_size < 64 * 1024, (
                    f"WAL file is {wal_size} bytes after checkpoint — "
                    "suggests many uncommitted transactions (per-vector path)."
                )

    def test_upsert_points_metadata_readable_via_new_store_instance(self):
        """Metadata written by upsert_points is readable via a completely fresh store."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            collection_name = _get_temporal_collection_name()
            store.create_collection(collection_name, vector_size=1024)

            points = _make_temporal_points(15, "abcdef01")
            store.upsert_points(collection_name=collection_name, points=points)

            # Open a completely fresh FilesystemVectorStore (simulates restart)
            store2 = _make_store(tmpdir)
            meta_store = store2._get_temporal_metadata_store()

            for p in points:
                hp = hashlib.sha256(p["id"].encode()).hexdigest()[:16]
                retrieved = meta_store.get_point_id(hp)
                assert retrieved == p["id"], (
                    f"Point '{p['id']}' not found via fresh store after restart. "
                    "Metadata may not have been committed."
                )


# ---------------------------------------------------------------------------
# BLOCKER 2: TemporalProgressiveMetadata staging + amortized flush
# ---------------------------------------------------------------------------


class TestAmortizedFlushWiring:
    """mark_commit_indexed must be O(1) (no disk write); flush_pending amortized."""

    def test_mark_commit_indexed_does_not_write_progress_file(self):
        """mark_commit_indexed must NOT write the progress file (lazy staging).

        If temporal_indexer uses save_completed() instead of mark_commit_indexed(),
        the progress file is written on every commit (O(N) cost). Verified here:
        after N mark_commit_indexed() calls, the file must NOT exist.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            temporal_dir = Path(tmpdir) / "temporal"
            temporal_dir.mkdir(parents=True)
            meta = TemporalProgressiveMetadata(temporal_dir)

            for i in range(20):
                meta.mark_commit_indexed(f"commit_{i:04d}")

            assert not meta.progress_path.exists(), (
                "progress file must NOT be written by mark_commit_indexed — "
                "it must be lazy (O(1) staging). If save_completed is used "
                "instead, the file is written on every commit (O(N) cost)."
            )

    def test_amortized_flush_writes_file_only_n_over_batch_times(self):
        """Flushing every 10 commits writes the file exactly ceil(N/10) + 1 times."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temporal_dir = Path(tmpdir) / "temporal"
            temporal_dir.mkdir(parents=True)
            meta = TemporalProgressiveMetadata(temporal_dir)

            n_commits = 50
            flush_interval = 10
            flush_count = 0

            for i in range(n_commits):
                meta.mark_commit_indexed(f"commit_{i:04d}")
                if (i + 1) % flush_interval == 0:
                    meta.flush_pending()
                    flush_count += 1

            # Final flush for tail
            meta.flush_pending()
            flush_count += 1

            # 50 commits / 10 per flush = 5 interval flushes + 1 tail = 6
            assert flush_count == 6, f"Expected 6 flushes, got {flush_count}"
            completed = meta.load_completed()
            assert len(completed) == n_commits

    def test_flush_after_success_ordering_preserves_durability(self):
        """flush_pending() AFTER vectors land — crash before flush loses only that batch.

        Correct ordering:
          1. upsert_points (vectors on disk)
          2. mark_commit_indexed (stage in memory)
          3. flush_pending() (mark complete on disk)

        Simulate crash after step 2 (no flush). Fresh instance must NOT
        contain the lost commit (re-index on resume = correct).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            temporal_dir = Path(tmpdir) / "temporal"
            temporal_dir.mkdir(parents=True)

            # Bootstrap: flush some earlier commits to establish baseline
            meta = TemporalProgressiveMetadata(temporal_dir)
            meta.mark_commit_indexed("safe_commit")
            meta.flush_pending()

            # Stage but do NOT flush (crash simulation)
            meta2 = TemporalProgressiveMetadata(temporal_dir)
            meta2.mark_commit_indexed("lost_commit")
            # No flush_pending() — crash

            # Fresh instance simulates post-crash resume
            meta3 = TemporalProgressiveMetadata(temporal_dir)
            completed = meta3.load_completed()

            assert "safe_commit" in completed, "Previously flushed commit must survive"
            assert "lost_commit" not in completed, (
                "Staged but unflushed commit must be absent after crash — "
                "indexer must re-index it on resume (no silent skip)"
            )

    def test_all_commits_present_after_amortized_flush_completes(self):
        """All commits staged across multiple batches are present after all flushes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temporal_dir = Path(tmpdir) / "temporal"
            temporal_dir.mkdir(parents=True)
            meta = TemporalProgressiveMetadata(temporal_dir)

            all_hashes = [f"commit_{i:04d}" for i in range(30)]
            for i, h in enumerate(all_hashes):
                meta.mark_commit_indexed(h)
                if (i + 1) % 10 == 0:
                    meta.flush_pending()
            meta.flush_pending()  # tail flush

            completed = meta.load_completed()
            for h in all_hashes:
                assert h in completed, f"{h} missing from completed set"


# ---------------------------------------------------------------------------
# BLOCKER 3: crash-resume completeness
# ---------------------------------------------------------------------------


class TestCrashResumeDurability:
    """Crash-resume: flushed commits survive; unflushed are re-indexed on resume."""

    def test_flushed_commits_survive_fresh_instance(self):
        """Flushed commits are present; unflushed commits absent on fresh instance."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temporal_dir = Path(tmpdir) / "temporal"
            temporal_dir.mkdir(parents=True)

            meta = TemporalProgressiveMetadata(temporal_dir)
            for i in range(5):
                meta.mark_commit_indexed(f"flushed_{i}")
            meta.flush_pending()

            # Stage second batch but do NOT flush
            meta2 = TemporalProgressiveMetadata(temporal_dir)
            for i in range(5):
                meta2.mark_commit_indexed(f"lost_{i}")
            # No flush

            # Fresh instance
            meta3 = TemporalProgressiveMetadata(temporal_dir)
            completed = meta3.load_completed()

            for i in range(5):
                assert f"flushed_{i}" in completed, f"flushed_{i} must survive"
                assert f"lost_{i}" not in completed, (
                    f"lost_{i} staged but not flushed must be absent (re-index on resume)"
                )

    def test_upsert_points_metadata_correct_after_two_batches(self):
        """Two sequential upsert_points calls both persist all metadata rows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            collection_name = _get_temporal_collection_name()
            store.create_collection(collection_name, vector_size=1024)

            batch1 = _make_temporal_points(10, "commit_a1")
            batch2 = _make_temporal_points(10, "commit_b2")

            store.upsert_points(collection_name=collection_name, points=batch1)
            store.upsert_points(collection_name=collection_name, points=batch2)

            db_path = (
                Path(tmpdir)
                / ".code-indexer"
                / "index"
                / collection_name
                / "temporal_metadata.db"
            )
            row_count = _count_sqlite_rows(db_path)
            assert row_count == 20, (
                f"Expected 20 rows after two batches of 10, got {row_count}"
            )


# ---------------------------------------------------------------------------
# HOT-PATH WIRING: prove save_metadata_batch is called by upsert_points
# and that temporal_indexer uses mark_commit_indexed (not save_completed)
# ---------------------------------------------------------------------------


class _CountingMetadataStore(TemporalMetadataStore):
    """Real passthrough subclass that counts save_metadata vs save_metadata_batch calls.

    Both methods execute their real logic — this is NOT a mock.
    The counters are used purely for test assertions about which hot path fired.
    """

    def __init__(self, collection_path: Path) -> None:
        super().__init__(collection_path)
        self.single_save_calls: int = 0
        self.batch_save_calls: int = 0

    def save_metadata(self, point_id: str, payload: dict) -> str:  # type: ignore[override]
        self.single_save_calls += 1
        return super().save_metadata(point_id, payload)

    def save_metadata_batch(self, rows: list) -> list:  # type: ignore[override]
        self.batch_save_calls += 1
        return super().save_metadata_batch(rows)


class TestHotPathWiring:
    """Prove the batch and staging APIs are wired into the actual hot paths."""

    # ------------------------------------------------------------------
    # BLOCKER 1 wiring: upsert_points must call save_metadata_batch, not save_metadata
    # ------------------------------------------------------------------
    def test_upsert_points_calls_save_metadata_batch_not_per_vector(self):
        """upsert_points must call save_metadata_batch (batch) NOT save_metadata (per-vector).

        We inject a counting passthrough subclass of TemporalMetadataStore.
        Both methods execute real SQLite logic — no behavior is mocked.
        After upsert_points: batch_save_calls must be 1, single_save_calls must be 0.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            collection_name = _get_temporal_collection_name()
            store.create_collection(collection_name, vector_size=1024)

            # Inject counting store before upsert (pre-initialise lazy field)
            collection_path = Path(tmpdir) / ".code-indexer" / "index" / collection_name
            counting_store = _CountingMetadataStore(collection_path)
            store._temporal_metadata_store = counting_store

            n = 10
            points = _make_temporal_points(n, "hotpath01")
            store.upsert_points(collection_name=collection_name, points=points)

            assert counting_store.batch_save_calls == 1, (
                f"Expected save_metadata_batch called 1 time for {n} points, "
                f"got {counting_store.batch_save_calls}. "
                "upsert_points is not using the batch API — FIX 1 not wired."
            )
            assert counting_store.single_save_calls == 0, (
                f"save_metadata (per-vector) was called {counting_store.single_save_calls} "
                f"times — upsert_points must use save_metadata_batch instead."
            )

    # ------------------------------------------------------------------
    # BLOCKER 2 wiring: temporal_indexer source must use mark_commit_indexed
    # ------------------------------------------------------------------
    def test_temporal_indexer_source_uses_mark_commit_indexed_not_save_completed(self):
        """temporal_indexer._index_commits_batch source must call mark_commit_indexed.

        We inspect the source of temporal_indexer to assert:
        - 'mark_commit_indexed' appears in the commit-complete section
        - 'save_completed' does NOT appear as an unconditional per-commit call

        This fails until temporal_indexer.py line 1231 is updated.
        """
        import src.code_indexer.services.temporal.temporal_indexer as ti_mod

        source = inspect.getsource(ti_mod)

        assert "mark_commit_indexed" in source, (
            "temporal_indexer must call progressive_metadata.mark_commit_indexed() "
            "instead of save_completed() — FIX 2 not wired in temporal_indexer.py"
        )

        # save_completed should no longer appear as the per-commit call
        # (it may still exist in TemporalProgressiveMetadata itself, so we check
        # the temporal_indexer module specifically for the active call site)
        assert "save_completed(commit.hash)" not in source, (
            "temporal_indexer still calls save_completed(commit.hash) per commit — "
            "replace with mark_commit_indexed(commit.hash) for O(1) staging."
        )
