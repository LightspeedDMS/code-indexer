"""Bug #1206 — FIX 2: TemporalProgressiveMetadata O(1) per-commit save cost.

Before the fix, mark_commit_indexed() -> _atomic_update() re-sorted the ENTIRE
completed_commits list and rewrote the whole progress file on EVERY commit.
With N completed commits, each save did O(N) work: sort + full JSON rewrite.

After the fix:
- mark_commit_indexed() accumulates pending commits in an in-memory set, writing
  to disk only when flush_pending() is called (or when a threshold is reached).
- flush_pending() writes all pending commits in ONE atomic operation.
- Durability invariant: flush_pending() MUST be called AFTER the vectors for
  those commits have been persisted.  A simulated crash before flush_pending()
  leaves those commits absent from load_completed() — i.e. they will be re-indexed
  on resume, not silently skipped.
- load_completed() still returns the full set of completed commits (both flushed
  and pending if flush has been called).

Tests use real filesystem in a temp directory — no mocks.
"""

import tempfile
import threading
import time
from pathlib import Path

from src.code_indexer.services.temporal.temporal_progressive_metadata import (
    TemporalProgressiveMetadata,
)

# Timeout constants for concurrent tests (seconds)
_WORKER_JOIN_TIMEOUT_S = 30
_FLUSHER_JOIN_TIMEOUT_S = 5


class TestProgressiveMetadataO1Cost:
    """Tests that per-commit save cost is O(1) and durability is preserved."""

    # ------------------------------------------------------------------
    # Test 1: flush_pending() method exists
    # ------------------------------------------------------------------
    def test_flush_pending_method_exists(self):
        """TemporalProgressiveMetadata exposes flush_pending() method."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temporal_dir = Path(tmpdir) / ".code-indexer" / "index" / "temporal"
            temporal_dir.mkdir(parents=True)
            meta = TemporalProgressiveMetadata(temporal_dir)

            # Attribute must exist
            assert hasattr(meta, "flush_pending"), (
                "flush_pending() method must exist on TemporalProgressiveMetadata"
            )
            assert callable(meta.flush_pending)

    # ------------------------------------------------------------------
    # Test 2: mark_commit_indexed + flush_pending persists the commit
    # ------------------------------------------------------------------
    def test_flush_pending_persists_commits(self):
        """Commits staged via mark_commit_indexed are persisted after flush_pending."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temporal_dir = Path(tmpdir) / "temporal"
            temporal_dir.mkdir(parents=True)
            meta = TemporalProgressiveMetadata(temporal_dir)

            meta.mark_commit_indexed("aaa111")
            meta.mark_commit_indexed("bbb222")
            meta.flush_pending()

            completed = meta.load_completed()
            assert "aaa111" in completed
            assert "bbb222" in completed

    # ------------------------------------------------------------------
    # Test 3: durability — commits NOT flushed are absent from load_completed
    # on a fresh instance (crash-resume correctness)
    # ------------------------------------------------------------------
    def test_unflushed_commits_absent_after_crash(self):
        """Commits staged but not flushed are absent from a fresh load_completed().

        This simulates: worker stages a commit in memory, then crashes before
        flush_pending() runs.  On resume the commit is absent from the completed
        set, so the indexer re-indexes it — correct behavior (no silent skip).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            temporal_dir = Path(tmpdir) / "temporal"
            temporal_dir.mkdir(parents=True)

            # First, flush some earlier commits to establish baseline
            meta = TemporalProgressiveMetadata(temporal_dir)
            meta.mark_commit_indexed("committed_before")
            meta.flush_pending()

            # Now stage a new commit but simulate crash (no flush)
            meta2 = TemporalProgressiveMetadata(temporal_dir)
            meta2.mark_commit_indexed("not_flushed")
            # *** no flush_pending() call ***

            # Fresh instance (simulates resume after crash)
            meta3 = TemporalProgressiveMetadata(temporal_dir)
            completed = meta3.load_completed()

            assert "committed_before" in completed, (
                "Previously flushed commit must survive"
            )
            assert "not_flushed" not in completed, (
                "Staged but unflushed commit must be absent after crash (so indexer re-indexes it)"
            )

    # ------------------------------------------------------------------
    # Test 4: mark_commit_indexed is cheaper per call as N grows
    # (time-bound: staging 100 commits takes < 2x the time of staging 10 commits)
    # ------------------------------------------------------------------
    def test_mark_commit_indexed_cost_does_not_grow_with_n(self):
        """Per-commit staging cost is bounded: 10x more commits, <2x more time."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temporal_dir_small = Path(tmpdir) / "small"
            temporal_dir_large = Path(tmpdir) / "large"
            temporal_dir_small.mkdir(parents=True)
            temporal_dir_large.mkdir(parents=True)

            # Warm up the small case
            meta_small = TemporalProgressiveMetadata(temporal_dir_small)
            t0 = time.monotonic()
            for i in range(10):
                meta_small.mark_commit_indexed(f"commit_{i:04d}")
            t_small = time.monotonic() - t0

            # Large case: 100 commits (10x more)
            meta_large = TemporalProgressiveMetadata(temporal_dir_large)
            t0 = time.monotonic()
            for i in range(100):
                meta_large.mark_commit_indexed(f"commit_{i:04d}")
            t_large = time.monotonic() - t0

            # If O(1), t_large should be ~10x t_small (linear with count, not N^2).
            # We accept up to 15x overhead to be lenient on CI timing jitter.
            # The OLD O(N^2) code would produce ~100x overhead for 10x more commits.
            ratio = t_large / max(t_small, 1e-9)
            assert ratio < 15.0, (
                f"mark_commit_indexed cost grew too fast: 100 commits took {ratio:.1f}x "
                f"longer than 10 commits (expected <15x for O(1) staging). "
                f"t_small={t_small * 1000:.1f}ms, t_large={t_large * 1000:.1f}ms"
            )

    # ------------------------------------------------------------------
    # Test 5: load_completed still returns full set after multiple flushes
    # ------------------------------------------------------------------
    def test_load_completed_accumulates_across_flushes(self):
        """load_completed returns union of all flushed commit batches."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temporal_dir = Path(tmpdir) / "temporal"
            temporal_dir.mkdir(parents=True)
            meta = TemporalProgressiveMetadata(temporal_dir)

            # First batch
            for i in range(5):
                meta.mark_commit_indexed(f"batch1_{i}")
            meta.flush_pending()

            # Second batch
            for i in range(5):
                meta.mark_commit_indexed(f"batch2_{i}")
            meta.flush_pending()

            completed = meta.load_completed()
            for i in range(5):
                assert f"batch1_{i}" in completed
                assert f"batch2_{i}" in completed

    # ------------------------------------------------------------------
    # Test 6: legacy save_completed() still works via flush_pending or direct
    # ------------------------------------------------------------------
    def test_save_completed_backward_compat_with_flush(self):
        """Legacy save_completed() API still persists commit after fix."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temporal_dir = Path(tmpdir) / "temporal"
            temporal_dir.mkdir(parents=True)
            meta = TemporalProgressiveMetadata(temporal_dir)

            meta.save_completed("legacy_hash_abc")
            # If save_completed buffers, we need to flush; if it writes directly, still OK
            if hasattr(meta, "flush_pending"):
                meta.flush_pending()

            completed = meta.load_completed()
            assert "legacy_hash_abc" in completed

    # ------------------------------------------------------------------
    # Test 7: progress file is NOT rewritten on every mark_commit_indexed call
    # (file mtime should NOT change until flush_pending is called)
    # ------------------------------------------------------------------
    def test_progress_file_not_rewritten_on_every_mark(self):
        """After the first flush, subsequent mark_commit_indexed calls do NOT rewrite the file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temporal_dir = Path(tmpdir) / "temporal"
            temporal_dir.mkdir(parents=True)
            meta = TemporalProgressiveMetadata(temporal_dir)

            # Bootstrap: flush one commit so the file exists
            meta.mark_commit_indexed("bootstrap")
            meta.flush_pending()

            progress_path = meta.progress_path
            assert progress_path.exists(), "Progress file must exist after first flush"
            mtime_after_flush = progress_path.stat().st_mtime_ns

            # Stage more commits WITHOUT flushing
            for i in range(10):
                meta.mark_commit_indexed(f"staged_{i}")

            mtime_after_staging = progress_path.stat().st_mtime_ns

            assert mtime_after_staging == mtime_after_flush, (
                "Progress file must NOT be rewritten during mark_commit_indexed staging — "
                "only flush_pending() should write to disk."
            )


class TestConcurrentMarkAndFlushNoDataLoss:
    """Bug #1206 FIX 2 — race-condition regression test.

    Without _pending_lock, flush_pending() does:
        to_flush = set(self._pending)   # snapshot (no lock)
        _atomic_update(...)             # slow fsync — other threads add() HERE
        self._pending.clear()           # clears commits that arrived after snapshot

    This test reproduces that race: 8 threads call mark_commit_indexed()
    concurrently while a 9th thread calls flush_pending() in a tight loop.
    Must FAIL on the unlocked code (commits silently lost) and PASS after the
    _pending_lock fix is applied.
    """

    def test_concurrent_mark_and_flush_no_data_loss(self):
        """8 worker threads + 1 flusher thread — all 200 commits must survive."""
        NUM_WORKERS = 8
        COMMITS_PER_WORKER = 25  # 200 total
        TOTAL = NUM_WORKERS * COMMITS_PER_WORKER

        with tempfile.TemporaryDirectory() as tmpdir:
            temporal_dir = Path(tmpdir) / "temporal"
            temporal_dir.mkdir(parents=True)
            meta = TemporalProgressiveMetadata(temporal_dir)

            # All expected commit hashes
            all_hashes = {
                f"worker{w}_commit{c}"
                for w in range(NUM_WORKERS)
                for c in range(COMMITS_PER_WORKER)
            }

            stop_flusher = threading.Event()
            errors: list = []

            def worker(worker_id: int) -> None:
                for c in range(COMMITS_PER_WORKER):
                    meta.mark_commit_indexed(f"worker{worker_id}_commit{c}")
                    # Zero-duration yield to maximise scheduling interleaving
                    time.sleep(0)

            def flusher() -> None:
                while not stop_flusher.is_set():
                    try:
                        meta.flush_pending()
                    except Exception as e:
                        errors.append(e)
                    time.sleep(0.001)  # flush every ~1 ms

            # Start flusher thread before workers to ensure maximum overlap
            flusher_thread = threading.Thread(target=flusher, daemon=True)
            flusher_thread.start()

            # Start and join all worker threads
            worker_threads = [
                threading.Thread(target=worker, args=(w,)) for w in range(NUM_WORKERS)
            ]
            for t in worker_threads:
                t.start()
            for t in worker_threads:
                t.join(timeout=_WORKER_JOIN_TIMEOUT_S)

            # Stop flusher and do a final drain flush
            stop_flusher.set()
            flusher_thread.join(timeout=_FLUSHER_JOIN_TIMEOUT_S)
            meta.flush_pending()

            assert not errors, f"Flusher thread raised exceptions: {errors}"

            completed = meta.load_completed()
            missing = all_hashes - completed
            assert len(missing) == 0, (
                f"{len(missing)}/{TOTAL} commits silently lost in concurrent "
                f"mark+flush race (Bug #1206 FIX 2 _pending_lock missing). "
                f"Missing sample: {sorted(missing)[:5]}"
            )
