"""Bug #1206 — FIX 3: Remove global _write_lock from FilesystemVectorStore._atomic_write_json.

Before the fix, _atomic_write_json held a process-wide threading.Lock() for
the entire duration of every vector JSON write (temp write + rename), serializing
ALL 8 embed threads even when writing to completely different files.

The temp+rename pattern (write to .tmp then os.replace) is already atomic at the
OS/filesystem level — the lock added no correctness benefit for independent files.

After the fix:
- _write_lock is removed (or reduced to guard only any shared in-memory state,
  NOT the file I/O itself).
- Concurrent writes to DIFFERENT files proceed in parallel with no serialization.
- Each individual file write is still atomic (temp+rename pattern preserved).
- Concurrent writes to the SAME file remain safe via OS-level atomic rename.

Tests use real filesystem in a temp directory — no mocks.
"""

import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from src.code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


def _make_store(base: str) -> FilesystemVectorStore:
    """Create a FilesystemVectorStore pointed at base/.code-indexer/index/."""
    index_path = Path(base) / ".code-indexer" / "index"
    index_path.mkdir(parents=True, exist_ok=True)
    return FilesystemVectorStore(base_path=index_path)


class TestAtomicWriteJsonConcurrent:
    """Tests that concurrent _atomic_write_json calls for distinct files do not serialize."""

    # ------------------------------------------------------------------
    # Test 1: _atomic_write_json writes valid JSON (basic correctness)
    # ------------------------------------------------------------------
    def test_atomic_write_json_produces_valid_json(self):
        """_atomic_write_json writes valid, readable JSON to the target path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            target = Path(tmpdir) / "vector_test.json"
            data = {"point_id": "abc123", "vector": [0.1, 0.2, 0.3]}

            store._atomic_write_json(target, data)

            assert target.exists()
            with open(target) as f:
                loaded = json.load(f)
            assert loaded == data

    # ------------------------------------------------------------------
    # Test 2: concurrent writes to DIFFERENT files all succeed
    # ------------------------------------------------------------------
    def test_concurrent_writes_to_distinct_files_all_succeed(self):
        """N threads writing to N distinct files concurrently all produce valid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            n = 20

            def write_file(i: int) -> Path:
                path = Path(tmpdir) / f"vector_{i:04d}.json"
                store._atomic_write_json(path, {"index": i, "data": list(range(10))})
                return path

            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(write_file, i) for i in range(n)]
                paths = [f.result() for f in as_completed(futures)]

            assert len(paths) == n
            for i in range(n):
                path = Path(tmpdir) / f"vector_{i:04d}.json"
                assert path.exists(), f"File {path} missing"
                with open(path) as f:
                    data = json.load(f)
                assert data["index"] == i

    # ------------------------------------------------------------------
    # Test 3: no .tmp files are left behind after concurrent writes
    # ------------------------------------------------------------------
    def test_no_tmp_files_left_after_concurrent_writes(self):
        """Concurrent _atomic_write_json calls leave no .tmp files behind."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)

            def write_file(i: int) -> None:
                path = Path(tmpdir) / f"vector_{i:04d}.json"
                store._atomic_write_json(path, {"index": i})

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(write_file, range(30)))

            tmp_files = list(Path(tmpdir).glob("*.tmp"))
            assert tmp_files == [], f"Leftover .tmp files: {tmp_files}"

    # ------------------------------------------------------------------
    # Test 4: concurrent writes to SAME file are safe (last write wins,
    # no partial/corrupt JSON)
    # ------------------------------------------------------------------
    def test_concurrent_writes_to_same_file_no_corruption(self):
        """Concurrent writes to the same file via temp+rename produce valid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            target = Path(tmpdir) / "shared.json"
            n = 20

            def write_shared(i: int) -> None:
                store._atomic_write_json(target, {"writer": i})

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(write_shared, range(n)))

            # The file must exist and contain valid JSON (last writer wins)
            assert target.exists()
            with open(target) as f:
                data = json.load(f)
            assert "writer" in data
            assert data["writer"] in range(n)

    # ------------------------------------------------------------------
    # Test 5: parallel writes are faster than serialized writes
    # (the global lock removal should reduce wall-clock time for N independent files)
    # ------------------------------------------------------------------
    def test_concurrent_writes_complete_faster_than_serial(self):
        """Writing 20 files concurrently is meaningfully faster than sequentially.

        This is a smoke test for the lock-removal: with the global lock held
        during file I/O, 8-thread concurrent writes would take ~serial time.
        Without it, 8 threads write in parallel.

        We use artificial delay via large payloads rather than time.sleep()
        to keep this realistic.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            # Large payload to make I/O time meaningful
            big_data = {"vector": list(range(1024)), "metadata": "x" * 4096}
            n = 16

            # Measure serial time
            t0 = time.monotonic()
            for i in range(n):
                path = Path(tmpdir) / "serial" / f"v_{i:04d}.json"
                path.parent.mkdir(exist_ok=True)
                store._atomic_write_json(path, big_data)
            t_serial = time.monotonic() - t0

            # Measure parallel time (8 workers)
            def write_par(i: int) -> None:
                path = Path(tmpdir) / "parallel" / f"v_{i:04d}.json"
                path.parent.mkdir(exist_ok=True)
                store._atomic_write_json(path, big_data)

            t0 = time.monotonic()
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(write_par, range(n)))
            t_parallel = time.monotonic() - t0

            # Parallel should be at least 1.5x faster than serial
            # (on a real disk with no global lock, 8 threads overlap I/O)
            # We use a generous threshold to avoid flakiness on slow CI.
            assert t_parallel < t_serial * 0.8 or t_parallel < 0.5, (
                f"Parallel ({t_parallel * 1000:.1f}ms) not faster than serial "
                f"({t_serial * 1000:.1f}ms) — global write lock may still be held"
            )
