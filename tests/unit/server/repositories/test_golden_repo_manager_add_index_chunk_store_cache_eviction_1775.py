"""GitHub Bug #1775 remediation: GoldenRepoManager.add_indexes_to_golden_repo()'s
post-loop publish block (CoW snapshot + alias swap, gated on a configured
`_refresh_scheduler`) never invalidated EITHER cache before this fix --
unlike `_cb_swap_alias()` and `RefreshScheduler._execute_refresh_impl()`,
which already had (at least) HNSW eviction wired. This test proves BOTH
the HNSW cache and the chunk-store cache are invalidated after this fix.

This is a REAL, non-mocked exercise of the actual production add-index
background_worker: `manager.add_index_to_golden_repo()` synchronously (via
a real-closure-executing job-manager double), a REAL `RefreshScheduler`
(real `AliasManager`) attached as `manager._refresh_scheduler`, the REAL
global `ChunkStoreThreadCache` singleton, and the REAL global
`HNSWIndexCache` singleton. Reuses
`test_clear_dedup_state_on_full_reindex_1560.py`'s established
`_fake_subprocess_boundaries` recipe verbatim -- the ONE external-process
boundary this project's mocking hierarchy tolerates a stand-in for
(`cidx init`/`cidx index` subprocess calls); `_create_snapshot` is also
stubbed (its own behavior -- real CoW snapshot creation -- is already
covered elsewhere; here we only care that its RETURN VALUE flows into a
real `swap_alias()` call).
"""

from pathlib import Path
from typing import Any, List
from unittest.mock import Mock

import hnswlib
import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.cache import get_global_cache, reset_global_cache
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.storage.shared.chunk_store_cache import (
    get_global_chunk_store_cache,
    reset_global_chunk_store_cache,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR = [0.1, 0.2, 0.3, 0.4]
CHUNKS_DB_FILENAME = "chunks.db"
PROVIDER_DIR = "voyage-code-3"
INDEX_SUBPATH = Path(".code-indexer") / "index" / PROVIDER_DIR
HNSW_SPACE = "cosine"
HNSW_DIM = 4
HNSW_MAX_ELEMENTS = 10
HNSW_EF_CONSTRUCTION = 10
HNSW_M = 4


def _make_real_hnsw_index() -> hnswlib.Index:
    idx = hnswlib.Index(space=HNSW_SPACE, dim=HNSW_DIM)
    idx.init_index(
        max_elements=HNSW_MAX_ELEMENTS, ef_construction=HNSW_EF_CONSTRUCTION, M=HNSW_M
    )
    return idx


def _make_versioned_snapshot(base: Path, repo: str, version: str, point_id: str):
    snapshot_dir = base / ".versioned" / repo / version
    collection_dir = snapshot_dir / INDEX_SUBPATH
    collection_dir.mkdir(parents=True, exist_ok=True)
    db_path = collection_dir / CHUNKS_DB_FILENAME
    store = ChunkStore(db_path)
    try:
        store.write_batch(
            [{"id": point_id, "vector": VECTOR, "payload": {"path": f"{point_id}.py"}}]
        )
    finally:
        store.close()
    return str(db_path), str(collection_dir), str(snapshot_dir)


class _SyncBackgroundJobManager:
    """Executes the submitted closure SYNCHRONOUSLY and for real -- same
    convention as test_clear_dedup_state_on_full_reindex_1560.py."""

    def submit_job(
        self,
        operation_type: str,
        func,
        *args: Any,
        submitter_username: str,
        is_admin: bool = False,
        repo_alias=None,
        **kwargs: Any,
    ) -> str:
        func()
        return "job-sync"


def _fake_run_with_popen_progress(
    command: List[str],
    phase_name: str,
    allocator,
    progress_callback,
    all_stdout: List[str],
    all_stderr: List[str],
    cwd,
    error_label=None,
    last_reported=None,
    env=None,
    orphan_event_callback=None,
) -> int:
    """Stands in for the real `cidx index --clear` subprocess -- the ONE
    external-process boundary this test doubles."""
    return 0


@pytest.fixture(autouse=True)
def _fake_subprocess_boundaries(monkeypatch):
    import subprocess

    from code_indexer.services import progress_subprocess_runner

    monkeypatch.setattr(
        progress_subprocess_runner,
        "run_with_popen_progress",
        _fake_run_with_popen_progress,
    )

    real_run = subprocess.run

    def _fake_subprocess_run(command, *args, **kwargs):
        is_fakeable_cidx_call = (
            len(command) > 1
            and command[0] == "cidx"
            and command[1] in ("init", "index")
        )
        if is_fakeable_cidx_call:

            class _FakeCompletedProcess:
                returncode = 0
                stdout = "already exists"
                stderr = ""

            return _FakeCompletedProcess()
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(
        "code_indexer.server.repositories.golden_repo_manager.subprocess.run",
        _fake_subprocess_run,
    )


@pytest.fixture(autouse=True)
def _reset_caches():
    reset_global_cache()
    reset_global_chunk_store_cache()
    yield
    get_global_chunk_store_cache().close_current_thread()
    reset_global_cache()
    reset_global_chunk_store_cache()


def _make_manager_with_real_repo_and_scheduler(
    tmp_path: Path, old_dir: str, new_versioned: str
) -> GoldenRepoManager:
    manager = GoldenRepoManager(data_dir=str(tmp_path))
    manager.background_job_manager = _SyncBackgroundJobManager()

    repo_path = tmp_path / "golden-repos" / "click"
    repo_path.mkdir(parents=True)
    (repo_path / ".code-indexer").mkdir()

    manager._sqlite_backend.add_repo(
        alias="click",
        repo_url="https://github.com/example/click.git",
        default_branch="main",
        clone_path=str(repo_path),
        created_at="2024-01-01T00:00:00Z",
    )

    # Real RefreshScheduler (real AliasManager, pointed at the SAME
    # aliases dir manager itself uses) -- only _create_snapshot and
    # cleanup_manager are test doubles; swap_alias() runs for real.
    scheduler = RefreshScheduler(
        golden_repos_dir=str(manager.golden_repos_dir),
        config_source=Mock(),
        query_tracker=Mock(spec=QueryTracker),
        cleanup_manager=Mock(spec=CleanupManager),
        registry=Mock(),
    )
    scheduler.alias_manager.create_alias("click-global", old_dir, repo_name="click")
    # setattr (not direct assignment) -- mypy's method-assign check flags
    # `scheduler._create_snapshot = ...` as reassigning a bound method;
    # setattr achieves the identical runtime effect without tripping it.
    setattr(scheduler, "_create_snapshot", Mock(return_value=new_versioned))
    manager._refresh_scheduler = scheduler

    return manager


class TestAddIndexPublishInvalidatesBothCaches1775:
    def test_add_index_swap_invalidates_hnsw_and_chunk_store_caches_for_old_snapshot(
        self, tmp_path: Path
    ) -> None:
        old_db, old_coll, old_dir = _make_versioned_snapshot(
            tmp_path, "click", "v_1", "p1"
        )
        new_versioned = str(tmp_path / ".versioned" / "click" / "v_2000000")

        manager = _make_manager_with_real_repo_and_scheduler(
            tmp_path, old_dir, new_versioned
        )

        chunk_cache = get_global_chunk_store_cache()
        store_first = chunk_cache.get_or_open(old_db, old_coll)
        try:
            assert store_first.read("p1") is not None

            hnsw_cache = get_global_cache()
            hnsw_cache.get_or_load(
                str(Path(old_coll)), lambda: (_make_real_hnsw_index(), {})
            )

            job_id = manager.add_index_to_golden_repo(
                alias="click", index_type="semantic", submitter_username="admin"
            )
            assert job_id == "job-sync"

            # Prove the REAL swap actually ran.
            scheduler = manager._refresh_scheduler
            assert scheduler is not None  # narrows Optional[Any] for mypy
            assert scheduler.alias_manager.read_alias("click-global") == new_versioned

            # HNSW: invalidate_prefix() evicts synchronously -- verify via
            # a tracking loader (fires only on a genuine cache-miss).
            hnsw_loader_calls: list = []

            def _tracking_loader():
                hnsw_loader_calls.append(1)
                return _make_real_hnsw_index(), {}

            hnsw_cache.get_or_load(str(Path(old_coll)), _tracking_loader)
            assert hnsw_loader_calls, (
                "GoldenRepoManager.add_indexes_to_golden_repo()'s "
                "post-loop publish block must invalidate the HNSW cache "
                "for the OLD snapshot."
            )

            # Chunk store: staleness is checked per-key on direct
            # re-access (not via a proactive cross-key sweep) --
            # re-requesting the SAME old key must return a genuinely NEW,
            # uncached object.
            store_second = chunk_cache.get_or_open(old_db, old_coll)
            try:
                assert store_second is not store_first, (
                    "GoldenRepoManager.add_indexes_to_golden_repo()'s "
                    "post-loop publish block must invalidate the "
                    "ChunkStoreThreadCache for the OLD snapshot -- this "
                    "is a third real production alias-swap site distinct "
                    "from _cb_swap_alias() and "
                    "RefreshScheduler._execute_refresh_impl()."
                )
            finally:
                store_second.close()
        finally:
            # store_first is already closed by production code's own
            # stale-eviction branch (inside the store_second open above)
            # on the success path; sqlite3's Connection.close() tolerates
            # a repeat call, and this guarantees cleanup even if an
            # earlier assertion/step raised first.
            store_first.close()
