"""Bug #1845 -- a versioned snapshot can be deleted while a reader on
another node still has it open, losing the reader's data mid-flight.

AC1 EVIDENCE (why no existing primitive can serve as the liveness signal,
read from source this run):

- ``QueryTracker`` (global_repos/query_tracker.py) is a pure in-process
  refcount, pinned only for the duration of ``_perform_search``
  (search.py ~line 1108). It is invisible across processes/nodes by
  construction -- confirmed here by running the reader and the deleter in
  two separate OS processes, so process B's ``QueryTracker`` instance can
  never see anything process A's instance did.
- ``HNSWIndexCache`` (server/cache/hnsw_index_cache.py) IS the long-lived
  reader named in the bug report: ``get_or_load()`` populates a
  ``HNSWIndexCacheEntry`` with its own TTL (default 10 minutes,
  ``HNSWIndexCacheConfig.ttl_minutes``), independent of any single
  request's lifetime, evicted only via ``invalidate()``/expiry
  (hnsw_index_cache.py lines 353-386, 541-590). It is, however, purely
  in-memory and per-process -- nothing publishes its liveness anywhere
  another node/process could observe it.
- ``SharedJobSentinel`` (server/services/shared_job_sentinel.py) is a
  SINGLE-HOLDER exclusive claim: one lock file per ``op_type``
  (``try_claim``, line 81), refused for anyone else until ``release()`` or
  staleness replacement (``is_stale``, line 216). It cannot represent "N
  nodes concurrently reading the same snapshot" -- wrong shape by
  construction, not merely unwired.
- ``JobTracker.register_job_if_no_conflict``
  (server/services/job_tracker.py line 480) is the SAME shape: one active
  job per conflict key (``repo_alias``), enforced by a partial unique DB
  index (``idx_active_job_per_repo``). Bug #1844 already reuses it
  correctly for cluster-wide *deletion* mutual exclusion (exactly one node
  deletes), which is a single-holder problem -- but "which nodes currently
  have this path open for reading" is a multi-holder membership problem,
  not mutual exclusion, so the SAME primitive cannot also serve as the
  liveness signal without corrupting its existing conflict semantics.

Conclusion demonstrated by this test: none of the above is consulted by
``CleanupManager._process_cleanup_queue()`` today. The only gates are
``QueryTracker.get_ref_count()`` (process-local, always 0 in the deleter's
own process), the ``MIN_RETENTION_AGE_SECONDS`` wall clock (bypassed here
via ``min_retention_age_seconds=0.0`` to prove the defect is independent
of timing, per AC4), and the #1844 cluster-wide claim (irrelevant to a
single node/attempt). A real, live HNSW index loaded through the exact
production loader shape, held in a genuinely separate OS process, changes
nothing about the outcome.

Test shape (why two REAL OS processes, not one): the defect IS that a
reader's liveness on one node is invisible to a deleter on another. A
single-process test could only fail by construction (nothing shares
memory across ``multiprocessing.get_context("spawn")`` children), so it
would prove nothing about cross-node visibility -- see
``test_cleanup_manager_cluster_dedup_1844.py`` for the established
sibling pattern this test follows (module-level process targets, pytest
fixtures for the on-disk snapshot, inline orchestration in the test
method).

The reader loads a REAL hnswlib index (built the same way
``tests/unit/server/cache/test_hnsw_cache_inplace_rebuild_staleness_1538.py``
does) through a loader shaped exactly like the production one in
``filesystem_vector_store.py``'s ``hnsw_loader()`` -- not a synthetic
stand-in -- so this is the genuine reader lifecycle named in the issue,
not a lease invented for the test. Liveness is verified only through the
public ``HNSWIndexCache.get_stats()`` API (never the private ``_cache``
dict), so the check is lock-safe and cannot race cache maintenance.

Typing note: the loaded index object is annotated ``Any``, the same
convention ``HNSWIndexCacheEntry.hnsw_index`` and
test_hnsw_cache_inplace_rebuild_staleness_1538.py itself use -- ``hnswlib``
is a C extension with no type stubs, so ``hnswlib.Index`` is not a usable
mypy annotation under this project's configuration.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import time
from pathlib import Path
from typing import Any, Dict, Tuple

import hnswlib
import numpy as np
import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.snapshot_reader_lease import (
    _lease_directory,
    _snapshot_key,
    snapshot_has_live_reader,
)
from code_indexer.server.cache.hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
)
from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.server.storage.shared.snapshot_paths import (
    is_versioned_snapshot,
)
from code_indexer.server.storage.sqlite_backends import (
    GoldenRepoMetadataSqliteBackend,
)

#: Bounded join/event deadline for the two-process test (Messi Rule #14 --
#: the test itself must not be able to hang forever either).
PROCESS_DEADLINE_SECONDS = 60.0

#: Default cache TTL is 10 minutes in production; using it here (rather
#: than an artificially long value) proves the reader is live via the
#: SAME configuration production actually ships.
READER_TTL_MINUTES = 10.0

VECTOR_DIM = 8
ITEM_COUNT = 3
LOAD_MAX_ELEMENTS = 100_000
INDEX_FILENAME = "hnsw_index.bin"


def _write_real_hnsw_index(index_file: Path) -> None:
    """Build and persist a genuinely valid hnswlib index -- same
    construction the production ``HNSWIndexManager``/existing test suite
    uses (see test_hnsw_cache_inplace_rebuild_staleness_1538.py)."""
    index = hnswlib.Index(space="cosine", dim=VECTOR_DIM)
    index.init_index(max_elements=100, ef_construction=100, M=16)
    rng = np.random.default_rng(42)
    index.add_items(
        rng.standard_normal((ITEM_COUNT, VECTOR_DIM)).astype(np.float32),
        list(range(ITEM_COUNT)),
    )
    staging_path = f"{index_file}.tmp"
    index.save_index(staging_path)
    os.replace(staging_path, str(index_file))


def reader_worker(
    snapshot_path: str, mount_point: str, ready_event, release_event, result_queue
) -> None:
    """Process A: a REAL, separate OS process. Loads the real hnswlib
    index at ``snapshot_path`` through the REAL, unmodified,
    already-existing production reader lifecycle named in the bug report
    -- ``HNSWIndexCache.get_or_load()`` -- and stays alive holding that
    cache entry while process B runs its real deletion cycle against the
    SAME path. Liveness is checked only through the public
    ``get_stats()`` API.

    ``mount_point`` is injected as ``lease_root`` (``mount_point /
    "cidx-meta"``) exactly the way the real server-layer construction
    site (``server/cache/__init__.py``'s ``_resolve_hnsw_lease_kwargs``)
    would resolve ``golden_repos_dir / "cidx-meta"`` -- this test
    constructs ``HNSWIndexCache`` directly rather than through that
    wiring, so it must replicate the injection to exercise the real
    reader-lease publication path (Bug #1845 remediation round 2,
    Defects 1+3).
    """
    try:
        index_file = Path(snapshot_path) / INDEX_FILENAME
        lease_root = Path(mount_point) / "cidx-meta"

        def _loader() -> Tuple[Any, Dict[int, str]]:
            # Any: hnswlib.Index has no type stubs (see module docstring).
            index = hnswlib.Index(space="cosine", dim=VECTOR_DIM)
            index.load_index(str(index_file), max_elements=LOAD_MAX_ELEMENTS)
            return index, {i: str(i) for i in range(ITEM_COUNT)}

        cache = HNSWIndexCache(
            HNSWIndexCacheConfig(ttl_minutes=READER_TTL_MINUTES),
            lease_root=lease_root,
            is_versioned_snapshot=is_versioned_snapshot,
        )
        cache.get_or_load(
            repo_path=snapshot_path, loader=_loader, index_file=index_file
        )

        normalized = str(Path(snapshot_path).resolve())
        repo_stats = cache.get_stats().per_repository_stats.get(normalized)
        if repo_stats is None or repo_stats["ttl_remaining_seconds"] <= 0:
            result_queue.put(
                {
                    "role": "A",
                    "ok": False,
                    "error": "cache entry missing or expired after load",
                }
            )
            return

        ready_event.set()

        if not release_event.wait(timeout=PROCESS_DEADLINE_SECONDS):
            result_queue.put(
                {"role": "A", "ok": False, "error": "B never signaled done"}
            )
            return

        # Re-check AFTER B has finished its cleanup cycle: this proves the
        # reader was genuinely still holding the snapshot at the moment
        # deletion happened, not merely that it once loaded it earlier.
        repo_stats_after = cache.get_stats().per_repository_stats.get(normalized)
        still_live = (
            repo_stats_after is not None
            and repo_stats_after["ttl_remaining_seconds"] > 0
        )
        result_queue.put({"role": "A", "ok": True, "still_live": still_live})
    except Exception as exc:
        result_queue.put(
            {"role": "A", "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )


def deleter_worker(
    db_path: str,
    mount_point: str,
    snapshot_path: str,
    ready_event,
    release_event,
    result_queue,
) -> None:
    """Process B: a REAL, separate OS process running the REAL,
    unmodified production ``CleanupManager`` deletion gate against the
    same snapshot path -- entirely independent of process A's in-memory
    ``HNSWIndexCache`` (a distinct Python interpreter has no way to see
    it), which is exactly the bug.
    """
    try:
        if not ready_event.wait(timeout=PROCESS_DEADLINE_SECONDS):
            result_queue.put(
                {"role": "B", "ok": False, "error": "A never signaled ready"}
            )
            return

        backend = GoldenRepoMetadataSqliteBackend(db_path)
        manager = CleanupManager(
            query_tracker=QueryTracker(),
            job_tracker=JobTracker(db_path),
            # AC4: bypass the 900s wall-clock floor entirely so this test
            # proves the defect independent of timing -- correctness must
            # not depend on a reader finishing within an arbitrary window,
            # so the test must show the gate fails even when that window
            # is zero.
            min_retention_age_seconds=0.0,
            persistence_backend=backend,
        )
        manager.set_snapshot_manager(
            VersionedSnapshotManager(
                versioned_base=mount_point,
                clone_backend=LocalCloneBackend(versioned_base=mount_point),
            )
        )
        # Bug #1845 remediation round 2 (Defect 3): must be the SAME root
        # process A's HNSWIndexCache injected via lease_root=Path(mount_point)
        # / "cidx-meta" in reader_worker, or the two processes would look in
        # different directories and the liveness check would be meaningless.
        manager.set_lease_root(Path(mount_point) / "cidx-meta")
        manager.schedule_cleanup(snapshot_path)
        manager._process_cleanup_queue()

        deleted = not Path(snapshot_path).exists()
        result_queue.put({"role": "B", "ok": True, "deleted": deleted})
    except Exception as exc:
        result_queue.put(
            {"role": "B", "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        # Always release A, even on failure, so the test cannot hang.
        release_event.set()


@pytest.fixture
def mount_point(tmp_path: Path) -> Path:
    mount = tmp_path / "cow-storage"
    mount.mkdir()
    return mount


@pytest.fixture
def snapshot_path(mount_point: Path) -> Path:
    snapshot = mount_point / ".versioned" / "repo_ns" / "v_1789090800"
    snapshot.mkdir(parents=True)
    _write_real_hnsw_index(snapshot / INDEX_FILENAME)
    return snapshot


@pytest.mark.slow
class TestSnapshotNotDeletedWhileLiveReaderHoldsItCrossProcess:
    def test_snapshot_survives_cleanup_while_hnsw_reader_on_another_process_holds_it(
        self, tmp_path: Path, mount_point: Path, snapshot_path: Path
    ) -> None:
        """RED on unmodified code: CleanupManager deletes the snapshot even
        though a real HNSWIndexCache reader entry (loaded from a genuine
        hnswlib index file) for the exact same path is provably still
        live in a separate process at the moment of deletion. GREEN
        (after the fix): the snapshot must survive while that live
        reader holds it (AC2).
        """
        db_path = str(tmp_path / "server.db")
        DatabaseSchema(db_path).initialize_database()
        GoldenRepoMetadataSqliteBackend(db_path).ensure_table_exists()

        ctx = mp.get_context("spawn")
        ready_event = ctx.Event()
        release_event = ctx.Event()
        result_queue = ctx.Queue()

        reader = ctx.Process(
            target=reader_worker,
            args=(
                str(snapshot_path),
                str(mount_point),
                ready_event,
                release_event,
                result_queue,
            ),
        )
        deleter = ctx.Process(
            target=deleter_worker,
            args=(
                db_path,
                str(mount_point),
                str(snapshot_path),
                ready_event,
                release_event,
                result_queue,
            ),
        )

        reader.start()
        deleter.start()
        try:
            reader.join(timeout=PROCESS_DEADLINE_SECONDS)
            deleter.join(timeout=PROCESS_DEADLINE_SECONDS)
            assert not reader.is_alive(), "reader process (A) did not finish in time"
            assert not deleter.is_alive(), "deleter process (B) did not finish in time"
            results = {
                r["role"]: r for r in (result_queue.get(timeout=10) for _ in range(2))
            }
        finally:
            for proc in (reader, deleter):
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=10)

        assert results["A"]["ok"], f"reader process failed: {results['A']}"
        assert results["A"]["still_live"], (
            "precondition broken: process A's HNSWIndexCache entry must "
            "still be live at the moment process B finished its cleanup "
            "cycle, or this test proves nothing about the bug"
        )
        assert results["B"]["ok"], f"deleter process failed: {results['B']}"

        # AC2: a snapshot with a live reader on ANY node/process must not
        # be deleted while that reader holds it.
        assert not results["B"]["deleted"], (
            "BUG #1845: CleanupManager deleted the snapshot while a real, "
            "live HNSWIndexCache reader (loaded from a genuine hnswlib "
            "index file) for the SAME path was open in a separate "
            "process -- the deletion gate consulted no cross-process "
            "liveness signal at all"
        )
        assert snapshot_path.exists(), (
            "the snapshot directory must survive while a live cross-process "
            "reader holds it"
        )


#: AC5 stale-lease fixture constants: a lease whose recorded TTL is this
#: short, and whose age is this far past it, unambiguously represents a
#: reader that stopped renewing long ago (a crashed process), never a
#: borderline/flaky timing case.
STALE_LEASE_TTL_SECONDS = 1.0
STALE_LEASE_AGE_SECONDS = 10_000.0


def _write_stale_lease(snapshot_path: Path, lease_root: Path) -> None:
    """Simulate a reader that acquired a lease and then crashed (or leaked
    the handle) without ever renewing or releasing it: a lease file whose
    ``updated_at`` is far older than its own recorded ``ttl_seconds``,
    written with the exact schema ``SnapshotReaderLease`` itself uses via
    the REAL production path-resolution helpers (``_lease_directory``,
    ``_snapshot_key``) -- not a synthetic location invented for the test.
    """
    directory = _lease_directory(str(snapshot_path), lease_root=lease_root)
    lease_file = directory / f"{_snapshot_key(str(snapshot_path))}-crashed-reader.json"
    payload = {
        "snapshot_path": str(snapshot_path.resolve()),
        "lease_id": "crashed-reader",
        "updated_at": time.time() - STALE_LEASE_AGE_SECONDS,
        "ttl_seconds": STALE_LEASE_TTL_SECONDS,
    }
    lease_file.write_text(json.dumps(payload), encoding="utf-8")


class TestStaleCrashedReaderLeaseDoesNotBlockReclamationForever:
    def test_stale_lease_from_a_crashed_reader_does_not_block_cleanup(
        self, tmp_path: Path, mount_point: Path, snapshot_path: Path
    ) -> None:
        """AC5: a reader that never releases -- crashed process, leaked
        handle, wedged node -- must not block reclamation forever. A lease
        left behind by a crashed reader stops being renewed, so it becomes
        stale once its own recorded ``ttl_seconds`` elapses; at that point
        both the liveness predicate and the real CleanupManager deletion
        gate must treat the snapshot as reclaimable. This is the explicit,
        conservative bound Messi Rule #14 requires -- no unbounded wait.
        """
        lease_root = mount_point / "cidx-meta"
        _write_stale_lease(snapshot_path, lease_root)
        assert not snapshot_has_live_reader(
            str(snapshot_path), lease_root=lease_root
        ), (
            "a lease whose age exceeds its own recorded ttl_seconds must "
            "not be reported live -- this is the bound that keeps a "
            "crashed reader from blocking reclamation forever"
        )

        db_path = str(tmp_path / "server.db")
        DatabaseSchema(db_path).initialize_database()
        GoldenRepoMetadataSqliteBackend(db_path).ensure_table_exists()

        backend = GoldenRepoMetadataSqliteBackend(db_path)
        manager = CleanupManager(
            query_tracker=QueryTracker(),
            job_tracker=JobTracker(db_path),
            min_retention_age_seconds=0.0,
            persistence_backend=backend,
        )
        manager.set_snapshot_manager(
            VersionedSnapshotManager(
                versioned_base=str(mount_point),
                clone_backend=LocalCloneBackend(versioned_base=str(mount_point)),
            )
        )
        manager.set_lease_root(lease_root)
        manager.schedule_cleanup(str(snapshot_path))
        manager._process_cleanup_queue()

        assert not snapshot_path.exists(), (
            "a stale lease left behind by a crashed reader must not block "
            "reclamation indefinitely -- deletion must proceed once the "
            "lease's own bounded TTL has elapsed"
        )
