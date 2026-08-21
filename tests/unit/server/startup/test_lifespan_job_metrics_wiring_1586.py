"""Story #1586 AC3/AC6: JobMetrics observable-gauge callback factories.

Proves the WIRING for the two observable-gauge callbacks JobMetrics needs
(cidx.jobs.active/queued and cidx.repos.total/indexed) -- extracted as
module-level factory functions in startup/lifespan.py (mirroring the
existing _make_dep_map_repair_invoker_fn testability pattern in that same
file) so they can be exercised directly without booting a full FastAPI app.

Real JobTracker (SQLite-backed) and real GoldenRepoManager + real
GoldenRepoMetadataSqliteBackend + real on-disk CHUNKS_DB collection data
(MESSI Rule #1: no mocks of the code under test). The repo-counts fixture
reuses the exact same real-fixture recipe as
tests/unit/server/repositories/test_index_exists_chunks_db_layout_1459.py.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from code_indexer.server.startup.lifespan import (
    _build_job_counts_callback,
    _build_repository_counts_callback,
)
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore

_BACKGROUND_JOBS_DDL = """
    CREATE TABLE IF NOT EXISTS background_jobs (
        job_id TEXT PRIMARY KEY,
        operation_type TEXT,
        status TEXT,
        created_at TEXT,
        started_at TEXT,
        completed_at TEXT,
        result TEXT,
        error TEXT,
        progress INTEGER DEFAULT 0,
        username TEXT,
        is_admin INTEGER DEFAULT 0,
        cancelled INTEGER DEFAULT 0,
        repo_alias TEXT,
        resolution_attempts INTEGER DEFAULT 0,
        progress_info TEXT,
        metadata TEXT,
        actor_username TEXT
    )
"""

_TEST_VECTOR = [0.1, 0.2, 0.3, 0.4]


def _make_job_tracker(tmp_path):
    """Build a real, SQLite-backed JobTracker (no backend injection)."""
    from code_indexer.server.services.job_tracker import JobTracker

    db_path = str(tmp_path / "jobs.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_BACKGROUND_JOBS_DDL)
        conn.commit()
    finally:
        conn.close()
    return JobTracker(db_path)


def _make_golden_repo_manager(tmp_path):
    """Build a real GoldenRepoManager backed by a real SQLite metadata store
    (GoldenRepoMetadataSqliteBackend) -- no golden_repos in-memory cache, so
    list_golden_repos()/get_golden_repo() both exercise the real backend
    read path exactly as production does.
    """
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoManager,
    )
    from code_indexer.server.storage.sqlite_backends import (
        GoldenRepoMetadataSqliteBackend,
    )

    backend = GoldenRepoMetadataSqliteBackend(str(tmp_path / "golden_repos.db"))
    backend.ensure_table_exists()

    manager = GoldenRepoManager.__new__(GoldenRepoManager)
    manager.golden_repos_dir = str(tmp_path)
    manager.golden_repos = {}
    manager._sqlite_backend = backend
    return manager, backend


def _register_repo(backend, tmp_path: Path, alias: str) -> Path:
    """Add a golden repo row pointing at a real on-disk clone_path."""
    repo_dir = tmp_path / alias
    repo_dir.mkdir(parents=True)
    backend.add_repo(
        alias=alias,
        repo_url="https://example.invalid/repo.git",
        default_branch="main",
        clone_path=str(repo_dir),
        created_at="2026-01-01T00:00:00Z",
    )
    return repo_dir


def _write_indexed_semantic_collection(repo_dir: Path) -> None:
    """Create a real CHUNKS_DB semantic collection with real chunk rows,
    so _index_exists(golden_repo, "semantic") genuinely returns True."""
    coll_dir = repo_dir / ".code-indexer" / "index" / "collection_default"
    coll_dir.mkdir(parents=True)
    (coll_dir / "collection_meta.json").write_text('{"name": "x", "vector_size": 4}')
    store = ChunkStore(coll_dir / "chunks.db")
    try:
        store.write_batch([{"id": "point-0", "vector": _TEST_VECTOR}])
    finally:
        store.close()
    write_chunks_db_discriminator(coll_dir)


class TestJobCountsCallbackFactory:
    def test_callback_matches_job_tracker_live_counts(self, tmp_path):
        tracker = _make_job_tracker(tmp_path)
        tracker.register_job("job-running-1", "dep_map_analysis", "admin")
        tracker.update_status("job-running-1", status="running")
        tracker.register_job("job-pending-1", "dep_map_analysis", "admin")

        callback = _build_job_counts_callback(tracker)
        counts = callback()

        assert counts == {
            "active": tracker.get_active_job_count(),
            "queued": tracker.get_queued_jobs_count(),
        }
        assert counts == {"active": 1, "queued": 1}


def _build_primed_repository_counts_callback(manager):
    """Story #1586 Finding 1 fix: the callback is now backed by a
    background-refreshed _RepositoryCountsCache -- callback() itself is
    O(1) and never blocks, so this helper builds the callback AND waits
    for its background priming refresh (started synchronously in the
    cache's constructor) to complete before returning it, so callers can
    assert on real computed values.
    """
    callback = _build_repository_counts_callback(manager)
    assert callback.cache.wait_for_idle(timeout=5.0) is True
    return callback


class TestRepositoryCountsCallbackFactory:
    def test_callback_counts_total_and_indexed_repos(self, tmp_path):
        manager, backend = _make_golden_repo_manager(tmp_path)

        indexed_repo_dir = _register_repo(backend, tmp_path, "repo-indexed")
        _write_indexed_semantic_collection(indexed_repo_dir)
        _register_repo(backend, tmp_path, "repo-unindexed")

        callback = _build_primed_repository_counts_callback(manager)
        counts = callback()

        assert counts == {"total": 2, "indexed": 1}

    def test_callback_returns_zero_total_when_no_repos(self, tmp_path):
        manager, _backend = _make_golden_repo_manager(tmp_path)

        callback = _build_primed_repository_counts_callback(manager)
        counts = callback()

        assert counts == {"total": 0, "indexed": 0}
