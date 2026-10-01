"""GitHub Bug #1775 remediation: shared ``invalidate_snapshot_caches()``
helper.

Code review flagged that the chunk-store cache eviction call was wired at
only ONE of five real alias-swap/publish sites, and suggested factoring
"invalidate both caches for an old snapshot path" into one small shared
helper so the two-cache contract cannot drift again as more call sites are
wired. This module tests that helper directly with REAL cache singletons
(HNSWIndexCache + ChunkStoreThreadCache) -- no mocking of the caches
themselves for the happy-path tests. The one fault-isolation test mocks a
cache's own accessor to simulate a genuine failure, which is a legitimate,
standard way to test an orchestrator's own non-fatal error handling (the
orchestrator -- this helper -- is the code under test there, not the
caches it calls).

Round-2 code review (both an independent Claude review and an independent
Codex review) additionally found: a golden repo's FIRST refresh has
``current_target`` equal to the MASTER base clone path (not yet any
``.versioned/`` snapshot) -- calling ``invalidate_prefix()`` on it would
permanently disable that repo's chunk-store cache forever after
onboarding, since (unlike the one-shot HNSWIndexCache eviction) a
chunk-store stale registration is a standing rule nothing ever undoes.
``TestMasterBaseCloneNeverBlacklisted`` proves the fix.
"""

from pathlib import Path
from unittest.mock import patch

import hnswlib
import pytest

from code_indexer.server.cache import get_global_cache, reset_global_cache
from code_indexer.server.storage.shared.snapshot_paths import (
    is_versioned_snapshot,
)
from code_indexer.storage.shared.chunk_store_cache import (
    get_global_chunk_store_cache,
    reset_global_chunk_store_cache,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore

HNSW_SPACE = "cosine"
HNSW_DIM = 4
HNSW_MAX_ELEMENTS = 10
HNSW_EF_CONSTRUCTION = 10
HNSW_M = 4
VECTOR = [0.1, 0.2, 0.3, 0.4]
CHUNKS_DB_FILENAME = "chunks.db"
PROVIDER_DIR = "voyage-code-3"
INDEX_SUBPATH = Path(".code-indexer") / "index" / PROVIDER_DIR


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


def _make_master_clone_chunk_store(base: Path, repo: str, point_id: str):
    """Build a real chunk store directly under a MASTER base clone path --
    i.e. NOT under ``.versioned/`` -- exactly the shape
    ``golden_repos_dir/{alias}`` has on a repo's first refresh, before any
    snapshot has ever been published for it.
    """
    master_dir = base / "golden-repos" / repo
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


def _make_legacy_cow_chunk_store(mount_point: Path, repo: str, point_id: str):
    """Build a real chunk store at the LEGACY cow-daemon snapshot shape
    ``{mount}/{repo}/v_<ts>`` -- deliberately NO ``.versioned/`` segment,
    the exact shape the bare canonical-only ``is_versioned_snapshot()``
    predicate misclassifies as "not a versioned snapshot" (it is only
    recognized when ``mount_point`` is supplied to the predicate).
    """
    snapshot_dir = mount_point / repo / "v_1699999999"
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


def _assert_hnsw_eviction(hnsw_cache, coll_path: str, *, expect_evicted: bool) -> None:
    """Assert whether the HNSW cache entry for ``coll_path`` was evicted,
    via a tracking loader that fires only on a genuine cache-miss (a hit
    would skip it entirely).
    """
    loader_calls: list = []

    def _tracking_loader():
        loader_calls.append(1)
        return _make_real_hnsw_index(), {}

    hnsw_cache.get_or_load(str(Path(coll_path)), _tracking_loader)
    if expect_evicted:
        assert loader_calls, (
            f"Expected the HNSW cache entry for {coll_path} to have been "
            "evicted (loader must fire on next access)."
        )
    else:
        assert not loader_calls, (
            f"Expected the HNSW cache entry for {coll_path} to survive "
            "untouched (loader must NOT fire -- a hit skips it)."
        )


@pytest.fixture(autouse=True)
def _reset_caches():
    reset_global_cache()
    reset_global_chunk_store_cache()
    yield
    get_global_chunk_store_cache().close_current_thread()
    reset_global_cache()
    reset_global_chunk_store_cache()


class TestIsVersionedSnapshotCheckIsRequired:
    """Round-4 code review (both an independent Claude review and an
    independent Codex review, converged): ``is_versioned_snapshot_check``
    defaulting to the mount-unaware bare predicate was a trap -- a future
    6th call site could silently regress into round-3's bug by simply
    forgetting to pass it. Making it a required keyword-only parameter
    means mypy catches a missing argument at CI time, closing the trap
    for good. All 5 real call sites already pass it correctly.
    """

    def test_calling_without_is_versioned_snapshot_check_raises_type_error(self):
        from code_indexer.server.cache.snapshot_cache_invalidation import (
            invalidate_snapshot_caches,
        )

        # Intentional invalid call (omits the now-required keyword-only
        # is_versioned_snapshot_check) -- verifies the runtime TypeError
        # that closes the "silent default fallback" trap.
        with pytest.raises(TypeError):
            invalidate_snapshot_caches("/some/old/target", log_context="[test]")  # type: ignore[call-arg]


class TestNoopOnFalsyOldTarget:
    def test_none_old_target_is_a_noop(self):
        from code_indexer.server.cache.snapshot_cache_invalidation import (
            invalidate_snapshot_caches,
        )

        # Must not raise.
        invalidate_snapshot_caches(
            None,
            log_context="[test]",
            is_versioned_snapshot_check=is_versioned_snapshot,
        )

    def test_empty_string_old_target_is_a_noop(self):
        from code_indexer.server.cache.snapshot_cache_invalidation import (
            invalidate_snapshot_caches,
        )

        invalidate_snapshot_caches(
            "", log_context="[test]", is_versioned_snapshot_check=is_versioned_snapshot
        )


class TestEvictsBothRealCaches:
    def test_evicts_real_hnsw_and_real_chunk_store_entries_for_old_target(
        self, tmp_path
    ):
        from code_indexer.server.cache.snapshot_cache_invalidation import (
            invalidate_snapshot_caches,
        )

        old_db, old_coll, old_dir = _make_versioned_snapshot(
            tmp_path, "repo", "v_1", "p1"
        )

        hnsw_cache = get_global_cache()
        hnsw_cache.get_or_load(
            str(Path(old_coll)), lambda: (_make_real_hnsw_index(), {})
        )

        chunk_cache = get_global_chunk_store_cache()
        store_first = chunk_cache.get_or_open(old_db, old_coll)

        invalidate_snapshot_caches(
            old_dir,
            log_context="[test]",
            is_versioned_snapshot_check=is_versioned_snapshot,
        )

        _assert_hnsw_eviction(hnsw_cache, old_coll, expect_evicted=True)

        # Chunk store: staleness is checked per-key on direct re-access
        # (not via a proactive cross-key sweep) -- re-requesting the SAME
        # old key must return a genuinely NEW, uncached object.
        store_second = chunk_cache.get_or_open(old_db, old_coll)
        assert store_second is not store_first, (
            "Chunk store cache entry for the old snapshot must have been "
            "invalidated by invalidate_snapshot_caches() -- re-access must "
            "open a fresh handle, never reuse the stale cached one."
        )


class TestFaultIsolationBetweenTheTwoCaches:
    def test_hnsw_eviction_failure_does_not_block_chunk_store_eviction(self, tmp_path):
        from code_indexer.server.cache.snapshot_cache_invalidation import (
            invalidate_snapshot_caches,
        )

        old_db, old_coll, old_dir = _make_versioned_snapshot(
            tmp_path, "repo", "v_1", "p1"
        )

        chunk_cache = get_global_chunk_store_cache()
        store_first = chunk_cache.get_or_open(old_db, old_coll)

        with patch(
            "code_indexer.server.cache.get_global_cache",
            side_effect=RuntimeError("simulated HNSW cache failure"),
        ):
            # Must not raise despite the simulated HNSW failure.
            invalidate_snapshot_caches(
                old_dir,
                log_context="[test]",
                is_versioned_snapshot_check=is_versioned_snapshot,
            )

        store_second = chunk_cache.get_or_open(old_db, old_coll)
        assert store_second is not store_first, (
            "A failure evicting the HNSW cache must not prevent the chunk "
            "store cache from still being invalidated."
        )

    def test_chunk_store_eviction_failure_does_not_block_hnsw_eviction(self, tmp_path):
        from code_indexer.server.cache.snapshot_cache_invalidation import (
            invalidate_snapshot_caches,
        )

        old_db, old_coll, old_dir = _make_versioned_snapshot(
            tmp_path, "repo", "v_1", "p1"
        )

        hnsw_cache = get_global_cache()
        hnsw_cache.get_or_load(
            str(Path(old_coll)), lambda: (_make_real_hnsw_index(), {})
        )

        with patch(
            "code_indexer.storage.shared.chunk_store_cache.get_global_chunk_store_cache",
            side_effect=RuntimeError("simulated chunk store cache failure"),
        ):
            invalidate_snapshot_caches(
                old_dir,
                log_context="[test]",
                is_versioned_snapshot_check=is_versioned_snapshot,
            )

        _assert_hnsw_eviction(hnsw_cache, old_coll, expect_evicted=True)


class TestMasterBaseCloneNeverBlacklisted:
    """HIGH #2 (code review round 2): a golden repo's FIRST refresh has
    ``current_target`` equal to the MASTER base clone path -- structurally
    NOT a ``.versioned/{ns}/v_<ts>`` snapshot. Calling
    ``invalidate_prefix()`` on it would permanently disable that repo's
    chunk-store cache forever (nothing ever un-stales a registered
    prefix), unlike the HNSW cache's one-shot eviction. This must never
    happen -- ``invalidate_snapshot_caches()`` must skip BOTH cache
    invalidations entirely when ``old_target`` is not a genuine versioned
    snapshot.
    """

    def test_master_clone_path_is_never_registered_as_stale(self, tmp_path):
        from code_indexer.server.cache.snapshot_cache_invalidation import (
            invalidate_snapshot_caches,
        )

        master_db, master_coll, master_dir = _make_master_clone_chunk_store(
            tmp_path, "myrepo", "p1"
        )

        chunk_cache = get_global_chunk_store_cache()
        store_first = chunk_cache.get_or_open(master_db, master_coll)
        assert store_first.read("p1") is not None

        # This is EXACTLY what a first-ever refresh passes as old_target.
        invalidate_snapshot_caches(
            master_dir,
            log_context="[test]",
            is_versioned_snapshot_check=is_versioned_snapshot,
        )

        # The master clone's cache entry must survive UNTOUCHED -- same
        # object, still cached, never permanently refused re-caching.
        store_second = chunk_cache.get_or_open(master_db, master_coll)
        assert store_second is store_first, (
            "The master base clone path must NEVER be registered as a "
            "stale chunk-store prefix -- doing so would silently disable "
            "that repo's chunk-store cache forever after onboarding."
        )

    def test_master_clone_hnsw_entry_also_survives(self, tmp_path):
        from code_indexer.server.cache.snapshot_cache_invalidation import (
            invalidate_snapshot_caches,
        )

        _master_db, master_coll, master_dir = _make_master_clone_chunk_store(
            tmp_path, "myrepo", "p1"
        )

        hnsw_cache = get_global_cache()
        hnsw_cache.get_or_load(
            str(Path(master_coll)), lambda: (_make_real_hnsw_index(), {})
        )

        invalidate_snapshot_caches(
            master_dir,
            log_context="[test]",
            is_versioned_snapshot_check=is_versioned_snapshot,
        )

        _assert_hnsw_eviction(hnsw_cache, master_coll, expect_evicted=False)


# Test-fixture-only values for a CowDaemonBackend config object that this
# test never actually contacts over the network -- only its mount_point
# attribute is read by is_versioned_snapshot(). Obviously-fake placeholder
# text, not a real credential or a real infrastructure endpoint.
_FAKE_COW_DAEMON_URL = "http://fake-cow-daemon.test-fixture.invalid:8081"
_FAKE_COW_DAEMON_API_KEY = "test-fixture-not-a-real-key"  # nosec - not a secret


class TestMountPointAwareCheckRecognizesLegacySnapshots:
    """MEDIUM (round-3 code review, both an independent Claude review and
    an independent Codex review, independently verified): the bare
    module-level ``is_versioned_snapshot(old_target)`` (no ``mount_point``)
    misclassifies real legacy cow-daemon/ONTAP snapshot paths (e.g.
    ``{mount}/repo/v_1``, no ``.versioned/`` segment) as "not a versioned
    snapshot", silently skipping BOTH cache invalidations for those
    backends -- inconsistent with the sibling physical-cleanup-scheduling
    gate in the same functions, which already uses the mount-aware
    ``VersionedSnapshotManager``/``_is_versioned_snapshot()`` facade.
    """

    def test_explicitly_passing_the_bare_canonical_check_still_skips_a_legacy_snapshot(
        self, tmp_path
    ):
        """Control: documents the known limitation of the BARE canonical
        predicate (round-4: no longer a silent default -- a caller must
        explicitly choose it). A legacy (non-.versioned/) shape is
        wrongly treated as non-versioned when a mount-aware resolver
        isn't used -- the chunk-store cache entry survives untouched even
        though old_target IS a real superseded snapshot. This is WHY
        every real call site passes a mount-aware
        ``_is_versioned_snapshot`` bound method instead.
        """
        from code_indexer.server.cache.snapshot_cache_invalidation import (
            invalidate_snapshot_caches,
        )

        mount_point = tmp_path / "mnt" / "cow-storage"
        legacy_db, legacy_coll, legacy_dir = _make_legacy_cow_chunk_store(
            mount_point, "myrepo", "p1"
        )

        chunk_cache = get_global_chunk_store_cache()
        store_first = chunk_cache.get_or_open(legacy_db, legacy_coll)

        invalidate_snapshot_caches(
            legacy_dir,
            log_context="[test]",
            is_versioned_snapshot_check=is_versioned_snapshot,
        )

        store_second = chunk_cache.get_or_open(legacy_db, legacy_coll)
        assert store_second is store_first, (
            "Documents the known limitation: the bare canonical-only "
            "check misclassifies a legacy cow-daemon snapshot path as "
            "non-versioned and never invalidates it -- this is why real "
            "call sites must pass a mount-aware resolver instead."
        )

    def test_mount_aware_check_correctly_invalidates_a_legacy_snapshot(self, tmp_path):
        from code_indexer.server.cache.snapshot_cache_invalidation import (
            invalidate_snapshot_caches,
        )
        from code_indexer.server.storage.shared.clone_backend import (
            CowDaemonBackend,
        )
        from code_indexer.server.storage.shared.snapshot_manager import (
            VersionedSnapshotManager,
        )
        from code_indexer.server.utils.config_manager import CowDaemonConfig

        mount_point = tmp_path / "mnt" / "cow-storage"
        legacy_db, legacy_coll, legacy_dir = _make_legacy_cow_chunk_store(
            mount_point, "myrepo", "p1"
        )

        backend = CowDaemonBackend(
            config=CowDaemonConfig(
                daemon_url=_FAKE_COW_DAEMON_URL,
                api_key=_FAKE_COW_DAEMON_API_KEY,
                mount_point=str(mount_point),
            )
        )
        snapshot_manager = VersionedSnapshotManager(clone_backend=backend)

        hnsw_cache = get_global_cache()
        hnsw_cache.get_or_load(
            str(Path(legacy_coll)), lambda: (_make_real_hnsw_index(), {})
        )
        chunk_cache = get_global_chunk_store_cache()
        store_first = chunk_cache.get_or_open(legacy_db, legacy_coll)

        invalidate_snapshot_caches(
            legacy_dir,
            log_context="[test]",
            is_versioned_snapshot_check=snapshot_manager.is_versioned_snapshot,
        )

        _assert_hnsw_eviction(hnsw_cache, legacy_coll, expect_evicted=True)

        store_second = chunk_cache.get_or_open(legacy_db, legacy_coll)
        assert store_second is not store_first, (
            "When a mount-aware is_versioned_snapshot_check is provided "
            "(matching the sibling physical-cleanup gate's own "
            "resolution), a legacy cow-daemon snapshot path must be "
            "correctly recognized and invalidated."
        )
