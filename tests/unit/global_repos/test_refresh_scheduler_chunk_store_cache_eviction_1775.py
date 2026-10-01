"""GitHub Bug #1775 remediation: RefreshScheduler._execute_refresh() is the
ACTUAL production leak driver -- the periodic, hourly, fleet-wide
golden-repo refresh (~900 repos, per project CLAUDE.md scale) -- distinct
from GoldenRepoManager._cb_swap_alias(), which fires only on a rare,
operator-initiated branch switch and was the (necessary but insufficient)
site the original #1775 fix wired.

Code review on the original fix (both an independent Claude review and an
independent Codex review) confirmed the chunk-store-cache eviction
mechanism and thread-affinity design are sound, but flagged that
_execute_refresh() calls ``self.alias_manager.swap_alias(...)`` DIRECTLY
and never goes through ``_cb_swap_alias()`` -- so the fleet's actual hourly
refresh cycle never invalidated the chunk-store cache at all. This test
exercises the REAL ``_execute_refresh()`` -> ``swap_alias()`` path (not a
narrower, more convenient stand-in) to prove the gap is closed at the
production code path that actually causes the leak.

Harness mirrors test_refresh_scheduler_integrity_gate_1506.py's
established ``_run_refresh()`` recipe exactly (real RefreshScheduler, real
AliasManager, real write-lock + integrity-gate machinery; only
GitPullUpdater/_index_source/_create_snapshot/_detect_existing_indexes/
_reconcile_registry_with_filesystem mocked -- the same boundary every
other _execute_refresh test in this suite uses), with ONE deliberate
deviation: ``swap_alias`` is NOT mocked here -- it is the exact call this
bug's fix hooks into, so it must run for real against a real AliasManager.

Round-2 code review (HIGH #2, both an independent Claude review and an
independent Codex review) flagged that the ORIGINAL version of this file
noticed but deliberately stepped around the FIRST-refresh scenario --
where ``current_target`` IS the master base clone, not yet any
``.versioned/`` snapshot -- and tested only the second-or-later refresh.
``TestFirstRefreshMasterCloneNeverBlacklisted`` fills that gap: the master
clone must never be registered as a stale chunk-store prefix (which would
permanently disable that repo's cache forever after onboarding).
"""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.storage.shared.chunk_store_cache import (
    get_global_chunk_store_cache,
    reset_global_chunk_store_cache,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR = [0.1, 0.2, 0.3, 0.4]
CHUNKS_DB_FILENAME = "chunks.db"
PROVIDER_DIR = "voyage-code-3"
INDEX_SUBPATH = Path(".code-indexer") / "index" / PROVIDER_DIR


def _make_versioned_snapshot(base: Path, repo: str, version: str, point_id: str):
    """Create a real, valid chunk store at a canonical
    ``.versioned/<repo>/<version>/.code-indexer/index/<provider>/chunks.db``
    path. Returns (db_path_str, collection_path_str, snapshot_dir_str).
    """
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


def _make_master_clone_chunk_store(golden_repos_dir: Path, repo: str, point_id: str):
    """Create a real chunk store directly under the MASTER base clone path
    (``golden_repos_dir/{repo}``) -- NOT under ``.versioned/`` -- exactly
    what a repo's FIRST-EVER refresh has before any snapshot has ever been
    published for it. Returns (db_path_str, collection_path_str,
    master_dir_str).
    """
    master_dir = golden_repos_dir / repo
    collection_dir = master_dir / INDEX_SUBPATH
    collection_dir.mkdir(parents=True, exist_ok=True)
    db_path = collection_dir / CHUNKS_DB_FILENAME

    store = ChunkStore(db_path)
    try:
        store.write_batch(
            [{"id": point_id, "vector": VECTOR, "payload": {"path": f"{point_id}.py"}}]
        )
    finally:
        store.close()

    return str(db_path), str(collection_dir), str(master_dir)


def _run_real_refresh_cycle(
    scheduler: RefreshScheduler,
    alias_name: str,
    master_path: Path,
    new_versioned: str,
) -> dict:
    """Drive a real RefreshScheduler._execute_refresh() cycle that reaches
    the REAL swap_alias() call. Mocks ONLY the same boundary every other
    _execute_refresh test in this suite mocks (git/indexing subprocess
    layer + snapshot creation) -- alias_manager.swap_alias, the write
    lock, and the integrity gate all run for real.
    """
    with (
        patch.object(scheduler, "_detect_existing_indexes", return_value={}),
        patch.object(scheduler, "_reconcile_registry_with_filesystem"),
        patch.object(scheduler, "_index_source"),
        patch.object(scheduler, "_create_snapshot", return_value=new_versioned),
        patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_git_updater_cls,
    ):
        mock_updater = Mock()
        mock_updater.has_changes.return_value = True
        mock_updater.get_source_path.return_value = str(master_path)
        mock_git_updater_cls.return_value = mock_updater

        return scheduler._execute_refresh(alias_name)


@pytest.fixture(autouse=True)
def _reset_chunk_store_cache():
    reset_global_chunk_store_cache()
    yield
    # Close this thread's real, still-cached sqlite3 handles before
    # resetting the singleton -- avoids leaking file descriptors across
    # test runs (the sweep only fires on a subsequent get_or_open() call,
    # which a failed/aborted test may never reach).
    get_global_chunk_store_cache().close_current_thread()
    reset_global_chunk_store_cache()


@pytest.fixture
def golden_repos_dir(tmp_path):
    d = tmp_path / "golden-repos"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def mock_query_tracker():
    return Mock(spec=QueryTracker)


@pytest.fixture
def mock_cleanup_manager():
    return Mock(spec=CleanupManager)


@pytest.fixture
def mock_config_source():
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    return config


@pytest.fixture
def mock_registry():
    registry = Mock()
    registry.get_global_repo.return_value = {
        "alias_name": "my-repo-global",
        "repo_url": "git@github.com:org/my-repo.git",
    }
    registry.list_global_repos.return_value = []
    registry.update_refresh_timestamp.return_value = None
    return registry


@pytest.fixture
def mock_golden_repo_metadata():
    """Mirrors test_refresh_scheduler_integrity_gate_1506.py's identically
    named fixture -- keeps the integrity gate's bookkeeping calls hermetic
    (never touches the real local dev server's SQLite DB)."""
    backend = Mock()
    backend.record_refresh_integrity_failure.return_value = 1
    backend.get_refresh_integrity_failure_state.return_value = None
    return backend


@pytest.fixture
def scheduler(
    golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
    mock_golden_repo_metadata,
):
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
        golden_repo_metadata_backend=mock_golden_repo_metadata,
    )


class TestRefreshSchedulerInvalidatesChunkStoreCache1775:
    def test_execute_refresh_swap_alias_invalidates_chunk_store_cache_for_old_snapshot(
        self, scheduler, golden_repos_dir
    ):
        alias_name = "my-repo-global"
        master_path = golden_repos_dir / "my-repo"
        master_path.mkdir(parents=True, exist_ok=True)
        # No chunks.db collections at master_path -- the integrity gate
        # passes trivially (mirrors
        # TestIntegrityGatePassProceedsAsBefore::
        # test_no_chunks_db_collections_still_publishes in
        # test_refresh_scheduler_integrity_gate_1506.py).

        # OLD (current) snapshot -- a REAL, previously-published versioned
        # snapshot: what production has once the FIRST refresh has already
        # run once. This is the SECOND-or-later hourly refresh scenario;
        # the FIRST-refresh (master-clone) scenario is covered separately
        # by TestFirstRefreshMasterCloneNeverBlacklisted below.
        old_db, old_coll, old_dir = _make_versioned_snapshot(
            golden_repos_dir, "my-repo", "v_1", "p1"
        )
        scheduler.alias_manager.create_alias(alias_name, old_dir)

        cache = get_global_chunk_store_cache()
        store_first = cache.get_or_open(old_db, old_coll)
        assert store_first.read("p1") is not None

        new_versioned = str(golden_repos_dir / ".versioned" / "my-repo" / "v_2000000")
        result = _run_real_refresh_cycle(
            scheduler, alias_name, master_path, new_versioned
        )

        assert result["success"] is True
        # Prove the REAL swap actually ran (not skipped/mocked away).
        assert scheduler.alias_manager.read_alias(alias_name) == new_versioned

        # Re-accessing the SAME old key directly must return a genuinely
        # NEW, uncached object -- proving invalidate_prefix() was
        # genuinely invoked with old_dir by the REAL _execute_refresh()
        # -> swap_alias() production path, not merely by the narrower
        # _cb_swap_alias() branch-switch path.
        store_second = cache.get_or_open(old_db, old_coll)
        assert store_second is not store_first, (
            "RefreshScheduler._execute_refresh()'s REAL swap_alias() call "
            "must invalidate the ChunkStoreThreadCache for the OLD "
            "snapshot -- this is the actual production leak driver "
            "(hourly, fleet-wide refresh), distinct from the rare "
            "operator-initiated _cb_swap_alias() branch-switch path."
        )


class TestFirstRefreshMasterCloneNeverBlacklisted:
    """HIGH #2 (code review round 2): a golden repo's FIRST-EVER refresh
    has ``current_target`` equal to the MASTER base clone path -- the
    alias hasn't been swapped to any ``.versioned/`` snapshot yet. The
    ORIGINAL version of this test file noticed this case in a comment
    ("the very first one (where current_target would still be the master
    clone)") and then deliberately tested only the second-or-later
    refresh -- the case was seen and stepped around, not covered. This
    test fills that gap with the REAL production path: the master clone's
    chunk-store cache entry must survive a real refresh cycle intact,
    never permanently blacklisted (which would silently disable that
    repo's cache forever after onboarding).
    """

    def test_first_refresh_master_clone_is_never_blacklisted_from_chunk_store_cache(
        self, scheduler, golden_repos_dir
    ):
        alias_name = "my-repo-global"

        # FIRST-EVER refresh: the alias currently points at the MASTER
        # base clone itself (real chunk store content in place, as if a
        # prior indexing pass had already populated it) -- no
        # `.versioned/` snapshot has ever been published for this repo.
        master_db, master_coll, master_dir = _make_master_clone_chunk_store(
            golden_repos_dir, "my-repo", "p1"
        )
        scheduler.alias_manager.create_alias(alias_name, master_dir)

        cache = get_global_chunk_store_cache()
        store_first = cache.get_or_open(master_db, master_coll)
        assert store_first.read("p1") is not None

        new_versioned = str(golden_repos_dir / ".versioned" / "my-repo" / "v_1000000")
        result = _run_real_refresh_cycle(
            scheduler, alias_name, Path(master_dir), new_versioned
        )

        assert result["success"] is True
        assert scheduler.alias_manager.read_alias(alias_name) == new_versioned

        # The master clone's cache entry must survive UNTOUCHED -- same
        # object, still cached, never permanently refused re-caching.
        store_second = cache.get_or_open(master_db, master_coll)
        assert store_second is store_first, (
            "The master base clone path must NEVER be registered as a "
            "stale chunk-store prefix by a first-ever refresh -- doing so "
            "would silently disable that repo's chunk-store cache "
            "forever after onboarding."
        )
