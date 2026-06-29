"""
Tests for Bug #1233: Multi-worker concurrent FTS queries fail with Tantivy LockBusy.

Root cause: The server FTS query path calls initialize_index(create_new=False) which
always creates an IndexWriter, taking the exclusive .tantivy-writer.lock. Under
multi-worker (separate OS processes), concurrent FTS reads from different workers
collide on that exclusive writer lockfile.

Fix: Add open_for_search() method that opens the index in read-only mode WITHOUT
creating an IndexWriter.

Tests use real Tantivy indexes on tmp_path — no mocking of Tantivy internals.
Cross-process test not used because Tantivy's writer lock is process-exclusive and
the test would require spawning real OS processes with a shared filesystem, which
is fragile in CI. Instead we test the critical invariants:
  1. open_for_search() does NOT set self._writer (no writer lock taken)
  2. Multiple TantivyIndexManager instances opened via open_for_search() can all
     search concurrently from the same directory (simulates multi-worker reads)
  3. Search results from open_for_search() are identical to results from
     initialize_index() (correctness preserved)
"""

import threading
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from code_indexer.services.tantivy_index_manager import TantivyIndexManager

pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TWO_READERS = 2
FOUR_READERS = 4
STRESS_READER_COUNT = 12
THREAD_JOIN_TIMEOUT_SECONDS = 60

SAMPLE_DOCS = [
    {
        "path": "src/auth.py",
        "content": "def login_user(username, password):\n    authenticate(username, password)\n    return session",
        "content_raw": "def login_user(username, password):\n    authenticate(username, password)\n    return session",
        "identifiers": ["login_user", "authenticate", "session"],
        "line_start": 1,
        "line_end": 3,
        "language": "python",
    },
    {
        "path": "src/config.py",
        "content": "CONFIG_PATH = '/etc/app/config'\nclass Configuration:\n    pass",
        "content_raw": "CONFIG_PATH = '/etc/app/config'\nclass Configuration:\n    pass",
        "identifiers": ["CONFIG_PATH", "Configuration"],
        "line_start": 1,
        "line_end": 3,
        "language": "python",
    },
    {
        "path": "src/utils.js",
        "content": "function authenticate(user, pass) {\n  return validateCredentials(user, pass);\n}",
        "content_raw": "function authenticate(user, pass) {\n  return validateCredentials(user, pass);\n}",
        "identifiers": ["authenticate", "validateCredentials"],
        "line_start": 1,
        "line_end": 3,
        "language": "javascript",
    },
]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _build_index(index_dir: Path) -> None:
    """Build a real Tantivy index with sample documents, then close writer."""
    manager = TantivyIndexManager(index_dir)
    try:
        manager.initialize_index(create_new=True)
        for doc in SAMPLE_DOCS:
            manager.add_document(doc)
        manager.commit()
    finally:
        manager.close()


@pytest.fixture()
def built_index(tmp_path: Path) -> Path:
    """Return path to a pre-built Tantivy index."""
    index_dir = tmp_path / "tantivy_index"
    _build_index(index_dir)
    return index_dir


def _run_concurrent_searches(
    index_dir: Path,
    query: str,
    worker_count: int,
) -> Tuple[List[Tuple[int, List[Any]]], List[Tuple[int, str]]]:
    """
    Run `worker_count` concurrent threads, each creating a fresh
    TantivyIndexManager via open_for_search() and searching for `query`.

    Returns (results, errors).  Each element of `results` is (worker_id, hits).
    Each element of `errors` is (worker_id, error_message).
    """
    results: List[Tuple[int, List[Any]]] = []
    errors: List[Tuple[int, str]] = []
    lock = threading.Lock()

    def worker(worker_id: int) -> None:
        manager = TantivyIndexManager(index_dir)
        try:
            manager.open_for_search()
            hits = manager.search(query, limit=50)
            with lock:
                results.append((worker_id, hits))
        except Exception as exc:
            with lock:
                errors.append((worker_id, str(exc)))
        finally:
            manager.close()

    threads = [
        threading.Thread(target=worker, args=(i,), daemon=True)
        for i in range(worker_count)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)

    return results, errors


# ---------------------------------------------------------------------------
# Test: open_for_search() exists and returns a usable manager
# ---------------------------------------------------------------------------


class TestOpenForSearch:
    """Tests for the new open_for_search() read-only entry point."""

    def test_open_for_search_sets_index_without_writer(self, built_index: Path) -> None:
        """open_for_search() must open the index but NOT create a writer."""
        manager = TantivyIndexManager(built_index)
        try:
            manager.open_for_search()
            # Index must be loaded (search would fail otherwise)
            assert manager._index is not None
            assert manager._schema is not None
            # CRITICAL: writer must NOT be created — this is the bug fix
            assert manager._writer is None, (
                "open_for_search() must NOT create an IndexWriter; "
                "creating a writer takes the exclusive .tantivy-writer.lock "
                "which causes LockBusy under multi-worker concurrent reads."
            )
        finally:
            manager.close()

    def test_open_for_search_returns_correct_results(self, built_index: Path) -> None:
        """Search results via open_for_search() must match expected documents."""
        manager = TantivyIndexManager(built_index)
        try:
            manager.open_for_search()
            results = manager.search("authenticate")
            paths = {r["path"] for r in results}
            assert "src/auth.py" in paths, (
                f"Expected src/auth.py in results; got {paths}"
            )
        finally:
            manager.close()

    def test_open_for_search_results_identical_to_initialize_index(
        self, built_index: Path
    ) -> None:
        """Search results from open_for_search() must be identical to initialize_index()."""
        ro_manager = TantivyIndexManager(built_index)
        try:
            ro_manager.open_for_search()
            ro_results = ro_manager.search("authenticate", limit=50)
            ro_paths = sorted(r["path"] for r in ro_results)
        finally:
            ro_manager.close()

        # initialize_index(create_new=False) path (original behaviour for comparison)
        rw_manager = TantivyIndexManager(built_index)
        try:
            rw_manager.initialize_index(create_new=False)
            rw_results = rw_manager.search("authenticate", limit=50)
            rw_paths = sorted(r["path"] for r in rw_results)
        finally:
            rw_manager.close()

        assert ro_paths == rw_paths, (
            f"open_for_search() returned different paths than initialize_index(): "
            f"ro={ro_paths}, rw={rw_paths}"
        )


# ---------------------------------------------------------------------------
# Test: concurrent multi-instance reads succeed (simulates multi-worker)
# ---------------------------------------------------------------------------


class TestConcurrentFTSReads:
    """
    Simulate multi-worker concurrent FTS reads by running multiple
    TantivyIndexManager instances (each opened via open_for_search) from
    concurrent threads against the same index directory.

    This reproduces the real-world failure mode: under multi-worker uvicorn,
    each worker process builds its own TantivyIndexManager per query and would
    previously call initialize_index(create_new=False), taking the exclusive
    writer lock — causing LockBusy collisions.

    Using threads instead of processes is valid here because:
    - The Tantivy writer lock is a FILE-level lock held by a file descriptor
    - Multiple TantivyIndexManager *instances* within the same process each
      attempt to acquire the lock independently
    - The pre-fix code (writer created in initialize_index) would cause the
      second instance to fail to acquire the lock even within one process
    - The post-fix code (open_for_search, no writer) eliminates lock contention
      regardless of process/thread boundaries
    """

    def test_two_concurrent_readers_same_index_dir(self, built_index: Path) -> None:
        """Two TantivyIndexManager instances can search concurrently, no LockBusy."""
        results, errors = _run_concurrent_searches(
            built_index, "authenticate", TWO_READERS
        )

        assert not errors, (
            f"Concurrent FTS reads raised errors: {errors}. "
            "This is the Bug #1233 LockBusy symptom."
        )
        assert len(results) == TWO_READERS, (
            f"Expected {TWO_READERS} results, got {len(results)}"
        )
        for worker_id, hits in results:
            paths = {r["path"] for r in hits}
            assert "src/auth.py" in paths, (
                f"Worker {worker_id} did not find src/auth.py in {paths}"
            )

    def test_twelve_concurrent_readers_same_index_dir(self, built_index: Path) -> None:
        """Twelve concurrent readers reproduce the original 8/12 failure rate."""
        results, errors = _run_concurrent_searches(
            built_index, "authenticate", STRESS_READER_COUNT
        )

        assert not errors, (
            f"Concurrent FTS reads raised errors (Bug #1233 reproduced): {errors}"
        )
        assert len(results) == STRESS_READER_COUNT, (
            f"Only {len(results)}/{STRESS_READER_COUNT} workers completed"
        )

    def test_concurrent_readers_find_all_documents(self, built_index: Path) -> None:
        """All concurrent readers find the expected documents."""
        results, errors = _run_concurrent_searches(
            built_index, "authenticate", FOUR_READERS
        )

        assert not errors, f"Errors from concurrent readers: {errors}"
        assert len(results) == FOUR_READERS

        for _worker_id, hits in results:
            paths = {r["path"] for r in hits}
            # Both Python and JS files contain "authenticate"
            assert "src/auth.py" in paths
            assert "src/utils.js" in paths
