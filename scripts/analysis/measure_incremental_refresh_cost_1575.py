#!/usr/bin/env python3
"""
Bug #1575 AC-M1: measurement utility for the visibility-epoch decision
engine (Part C, ``filesystem_vector_store.py``) and the authoritative
content-path enumeration (Part A, ``distinct_content_paths`` /
``fetch_points_for_paths``).

Wraps FIVE production boundary calls FROM OUTSIDE -- never patches,
monkeypatches, or subclasses them (Messi Rule #17: no metaprogramming that
obscures control/data flow):

  * ``HNSWIndexManager._load_vectors_from_json_files``  (SHARDED_JSON full
    rebuild loader -- opens one file per vector on disk)
  * ``HNSWIndexManager._load_vectors_from_chunks_db``    (CHUNKS_DB full
    rebuild loader -- streams every row of ``chunks.db``)
  * ``FilesystemVectorStore.distinct_content_paths``     (Part A
    authoritative path enumeration)
  * ``FilesystemVectorStore.fetch_points_for_paths``     (Part A targeted
    fetch)
  * ``ChunkStore.distinct_paths``                         (Bug #1575
    Finding 1: the CHUNKS_DB unique-file-count full ``SELECT DISTINCT
    path`` index scan invoked from
    ``FilesystemVectorStore._calculate_and_save_unique_file_count`` --
    added after dual review found this exact call left completely
    uninstrumented, hiding an O(N) cost outside the original four
    boundaries)

The wrapping mechanism is a ``sys.setprofile``-based call counter, matched
by EXACT CODE-OBJECT IDENTITY (never by name/heuristic string matching) --
a pure, read-only observer built on CPython's own profiling hook. It never
changes what the wrapped functions do and is torn down (exception-safe,
via ``try/finally``) even if the measured block raises.

This is the SAME utility reused by the permanent AC51/AC52 regression test:
``tests/unit/storage/test_filesystem_vector_store_1575_measurement_scaling.py``.

The five required cost metrics (Bug #1575 Measurement Methodology). Note
these track ONLY the five named boundary calls above -- a full-collection
scan hiding in some OTHER, unlisted call site would not be reflected here
(``ChunkStore.count()``, used for ``vector_count`` reporting in
``end_indexing()``, is one such lighter-weight full-table ``SELECT
COUNT(*)`` that remains untracked here -- out of this fix's scope,
documented rather than silently omitted):

KNOWN, MATERIAL, SHARDED_JSON-ONLY OMISSION (do not mistake for O(1)):
``end_indexing()`` unconditionally calls
``FilesystemVectorStore._calculate_and_save_unique_file_count()`` on
EVERY refresh. For CHUNKS_DB that call is the tracked, lightweight
``ChunkStore.distinct_paths()`` boundary above. For SHARDED_JSON it is
NOT tracked at all: it always invokes
``_rebuild_and_repair_path_index()`` -> ``_rebuild_path_index_from_disk()``,
which ``rglob``s the collection directory and ``open()``s + ``json.load()``s
EVERY remaining ``vector_*.json`` file, regardless of collection size --
the project owner's FINAL decision (after 6 consecutive dual-review
rounds each found a new correctness bug in the PathIndex fast-path
shortcut this replaced) to accept correctness-over-speed for this layout;
see ``tests/unit/storage/test_filesystem_vector_store_1575_sharded_json_shortcut_abandoned.py``
and ``tests/unit/storage/test_filesystem_vector_store_1575_finding1_unique_file_count_scaling.py``.
Unlike ``ChunkStore.count()`` above, this is NOT "lighter-weight" -- it is
the SAME CLASS of O(collection) file-open cost this entire bug is about,
just running through a function outside this module's 5 tracked
boundaries, so ``files_opened``/``vectors_materialized`` read as exactly
0 for a SHARDED_JSON "after" refresh even though a real, size-correlated
file-open pass is happening. Empirically confirmed via this module's own
AC51 test (``tests/unit/storage/test_filesystem_vector_store_1575_measurement_scaling.py``)
using its uninstrumented ``measure_single_file_change_refresh_wall_clock()``
reading: a SHARDED_JSON refresh with 0 tracked ``files_opened`` took
6.76s of HONEST wall-clock time at n=40,000, versus 3.30s for a CHUNKS_DB
refresh at n=100,000 (more than double the collection size) -- direct
evidence that SHARDED_JSON's refresh is NOT actually O(1) end-to-end,
even though Part A/C's own specific wins (no full HNSW rebuild, no full
payload materialization) are real and correctly reflected by the 5
tracked metrics. Do not "fix" this by re-adding a fast-path shortcut --
that door is closed by the abandonment decision above; this note exists
so nobody mistakes the 5 tracked metrics for a claim that SHARDED_JSON's
total refresh cost is bounded, which it is not.
  * ``files_opened``          -- SHARDED_JSON per-vector-file reads, for
                                   the boundaries above that open files
  * ``bytes_read``             -- total I/O volume attributable to a
                                   boundary call
  * ``store_rows_scanned``     -- CHUNKS_DB: whether a call touches 1 row
                                   or the whole table, for the boundaries
                                   above that scan chunks.db
  * ``vectors_materialized``   -- points actually loaded into HNSW
                                   construction by a boundary call
  * ``instrumented_wall_time_seconds`` / ``instrumented_peak_rss_delta_bytes``
                                -- end-to-end cost of the measured block,
                                   inflated by sys.setprofile overhead
                                   (Bug #1575 Finding 4) -- for an honest
                                   reading use
                                   ``measure_single_file_change_refresh_wall_clock()``
                                   on a fresh twin fixture instead

Usage as a library (see the test file above for real examples):

    from measure_incremental_refresh_cost_1575 import (
        build_synthetic_fixture,
        apply_single_file_change_refresh,
        instrument_boundary_calls,
    )

    fixture = build_synthetic_fixture(tmp_path, num_points=100_000,
                                       use_chunks_db=False)
    result, metrics = apply_single_file_change_refresh(
        fixture, target_file=..., hide_file=..., restore_file=...
    )

Usage as a script (AC19/AC20/AC21 performance demonstration -- builds a
reproducible, >=100,000-point synthetic fixture for BOTH storage layouts,
requires no production access):

    PYTHONPATH=<repo>/src python3 \\
        scripts/analysis/measure_incremental_refresh_cost_1575.py
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.chunk_layout import ChunkLayout
from code_indexer.storage.sqlite_chunk_store import ChunkStore

DEFAULT_VECTOR_DIM = 8
DEFAULT_CHUNKS_PER_FILE = 5
# The single synthetic branch name used by every fixture in this module --
# a module constant (not a scattered literal) so bootstrap and refresh
# always agree on it by construction.
_BOOTSTRAP_BRANCH = "main"
_LINUX_PLATFORM_PREFIX = "linux"
_BYTES_PER_KIB = 1024
_VMRSS_STATUS_PREFIX = "VmRSS:"


# ---------------------------------------------------------------------------
# Current RSS (Linux-only signal; 0 elsewhere -- informational metric, never
# a pass/fail gate, so degrading gracefully on unsupported platforms is
# safe).
#
# Bug #1575 Finding 4: this used to read
# ``resource.getrusage(RUSAGE_SELF).ru_maxrss`` -- the PROCESS-LIFETIME
# monotonically non-decreasing high-water mark, not current RSS. Since this
# harness's bootstrap fixture-build phase always runs (and always peaks
# memory first) before the measured refresh, that made
# ``peak_rss_delta_bytes`` structurally biased toward reporting 0 --
# unusable evidence for an AC20-style per-block RSS claim. Reading
# ``/proc/self/status``'s ``VmRSS`` line instead reports the CURRENT
# resident set at the moment of the call, so a before/after delta around a
# measured block is meaningful.
# ---------------------------------------------------------------------------


def _peak_rss_bytes() -> int:
    if not sys.platform.startswith(_LINUX_PLATFORM_PREFIX):
        return 0
    try:
        with open("/proc/self/status") as status_file:
            for line in status_file:
                if line.startswith(_VMRSS_STATUS_PREFIX):
                    # Format: "VmRSS:\t   12345 kB\n"
                    kib = int(line.split()[1])
                    return kib * _BYTES_PER_KIB
    except OSError:
        # Intentional graceful degrade, matching this function's own
        # documented contract above ("informational metric, never a
        # pass/fail gate") -- /proc is Linux-specific and can legitimately
        # be unreadable in a sandboxed/containerized test environment;
        # this is never the platform-detection branch above (that already
        # returns 0 for non-Linux), only an unexpected read failure ON
        # Linux, which must not fail the measured block it wraps.
        return 0
    return 0


# ---------------------------------------------------------------------------
# Boundary call recorder
# ---------------------------------------------------------------------------

# Identity-based matching: the exact code objects of the five named
# boundary functions. If any of these functions is ever renamed or moved,
# THIS IMPORT FAILS LOUDLY at module load time -- a stronger, more honest
# verification than a filename/string heuristic that could silently stop
# matching anything.
#
# Bug #1575 Finding 1 (dual review): ``ChunkStore.distinct_paths`` was
# added as a 5th boundary after the initial four-boundary design left the
# CHUNKS_DB unique-file-count rescan (a full ``SELECT DISTINCT path``
# index scan whose cost scales with collection size, invoked from
# ``FilesystemVectorStore._calculate_and_save_unique_file_count``)
# completely uninstrumented -- the exact O(N) cost hiding outside the
# original four boundaries that this bug's dual review caught. Tracked
# here (rather than via a second, independent ``sys.setprofile`` in a
# separate test) because ``instrument_boundary_calls()`` below is the
# ONLY profiler active during a measured refresh -- installing a second
# one around a call to ``apply_single_file_change_refresh()`` would be
# silently shadowed by this one for the whole duration (Bug #1575
# Finding 3's exact single-active-profiler lesson, rediscovered while
# building this fix's own regression test).
_TARGET_CODE_OBJECTS: Dict[Any, str] = {
    HNSWIndexManager._load_vectors_from_json_files.__code__: (
        "_load_vectors_from_json_files"
    ),
    HNSWIndexManager._load_vectors_from_chunks_db.__code__: (
        "_load_vectors_from_chunks_db"
    ),
    FilesystemVectorStore.distinct_content_paths.__code__: ("distinct_content_paths"),
    FilesystemVectorStore.fetch_points_for_paths.__code__: ("fetch_points_for_paths"),
    ChunkStore.distinct_paths.__code__: ("distinct_paths"),
}


def _count_chunks_db_rows_and_size(db_path: Path) -> Tuple[int, int]:
    """Independent, outside-production, read-only probe of a chunks.db
    file's total row count and byte size.

    Used ONLY to attribute the cost of a call to the production
    ``_load_vectors_from_chunks_db`` boundary, which internally performs an
    UNCONDITIONAL full-table stream (``ChunkStore.stream_for_index_rebuild``)
    regardless of any filter -- so "rows scanned" for that boundary IS the
    table's total row count. Opens its own connection; never touches
    production code. URI-escapes the path via ``Path.resolve().as_uri()``
    (Bug #1459's established fix for SQLite's literal-``?`` URI parsing
    trap), not a naive f-string.
    """
    try:
        size = db_path.stat().st_size
    except OSError:
        return 0, 0
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except (OSError, sqlite3.Error):
        return 0, size
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM chunks")
        (count,) = cursor.fetchone()
        return int(count), size
    except sqlite3.Error:
        return 0, size
    finally:
        conn.close()


@dataclass
class BoundaryMetrics:
    """The five required cost metrics plus auxiliary per-boundary counters
    for Part A's targeted-enumeration calls.

    Bug #1575 Finding 4: ``instrumented_wall_time_seconds``/
    ``instrumented_peak_rss_delta_bytes`` are measured WHILE
    ``sys.setprofile`` is active for call-counting -- that imposes real,
    well-documented per-call overhead on every Python-level call in the
    process, inflating both figures relative to an uninstrumented run.
    They are named ``instrumented_*`` specifically so nobody mistakes them
    for a real cost figure: the ONLY honest wall-clock/RSS reading is
    ``measure_single_file_change_refresh_wall_clock()``'s uninstrumented
    return values, on a fresh twin fixture.
    """

    files_opened: int = 0
    bytes_read: int = 0
    store_rows_scanned: int = 0
    vectors_materialized: int = 0
    instrumented_wall_time_seconds: float = 0.0
    instrumented_peak_rss_delta_bytes: int = 0
    call_counts: Dict[str, int] = field(default_factory=dict)
    # Part A auxiliary counters (not one of the five core metrics -- these
    # boundaries return paths/payloads, not vectors, so they are reported
    # separately rather than folded into vectors_materialized).
    content_paths_returned: int = 0
    fetched_points_requested: int = 0
    fetched_points_returned: int = 0


class BoundaryCallRecorder:
    """``sys.setprofile``-based call counter for the four named Bug #1575
    boundary functions.

    Never patches, subclasses, or wraps the target functions -- it purely
    OBSERVES calls via CPython's own profiling hook, matching by exact
    code-object identity, so it can never diverge from -- or interfere
    with -- what the functions actually do.
    """

    def __init__(self) -> None:
        self.metrics = BoundaryMetrics()
        self._tracked: Dict[int, str] = {}
        self._lock = threading.Lock()

    def profiler(self, frame: Any, event: str, arg: Any) -> None:
        if event == "call":
            self._on_call(frame)
        elif event == "return":
            self._on_return(frame, arg)
        return None

    def _on_call(self, frame: Any) -> None:
        func_name = _TARGET_CODE_OBJECTS.get(frame.f_code)
        if func_name is None:
            return
        with self._lock:
            self._tracked[id(frame)] = func_name
            self.metrics.call_counts[func_name] = (
                self.metrics.call_counts.get(func_name, 0) + 1
            )
        if func_name == "_load_vectors_from_json_files":
            self._on_call_json_loader(frame)
        elif func_name == "_load_vectors_from_chunks_db":
            self._on_call_chunks_db_loader(frame)
        elif func_name == "fetch_points_for_paths":
            self._on_call_fetch_points(frame)

    def _on_call_json_loader(self, frame: Any) -> None:
        vector_files = frame.f_locals.get("vector_files") or []
        files_opened = len(vector_files)
        bytes_read = 0
        for vector_file in vector_files:
            try:
                bytes_read += Path(vector_file).stat().st_size
            except OSError:
                continue
        with self._lock:
            self.metrics.files_opened += files_opened
            self.metrics.bytes_read += bytes_read

    def _on_call_chunks_db_loader(self, frame: Any) -> None:
        collection_path = frame.f_locals.get("collection_path")
        if collection_path is None:
            return
        rows, size = _count_chunks_db_rows_and_size(Path(collection_path) / "chunks.db")
        with self._lock:
            self.metrics.store_rows_scanned += rows
            self.metrics.bytes_read += size

    def _on_call_fetch_points(self, frame: Any) -> None:
        paths = frame.f_locals.get("paths")
        if paths is None:
            return
        try:
            count = len(paths)
        except TypeError:
            return
        with self._lock:
            self.metrics.fetched_points_requested += count

    def _on_return(self, frame: Any, arg: Any) -> None:
        with self._lock:
            func_name = self._tracked.pop(id(frame), None)
        if func_name is None:
            return
        if func_name in (
            "_load_vectors_from_json_files",
            "_load_vectors_from_chunks_db",
        ):
            if isinstance(arg, tuple) and len(arg) == 2:
                try:
                    count = len(arg[0])
                except TypeError:
                    return
                with self._lock:
                    self.metrics.vectors_materialized += count
        elif func_name == "distinct_content_paths":
            try:
                count = len(arg)
            except TypeError:
                return
            with self._lock:
                self.metrics.content_paths_returned += count
        elif func_name == "fetch_points_for_paths":
            try:
                count = len(arg)
            except TypeError:
                return
            with self._lock:
                self.metrics.fetched_points_returned += count


@contextlib.contextmanager
def instrument_boundary_calls() -> Iterator[BoundaryCallRecorder]:
    """Measure instrumented wall time, instrumented peak-RSS delta, and
    calls to the five Bug #1575 boundary functions for the code executed
    inside the ``with`` block.

    Bug #1575 Finding 3: this used to also call ``threading.setprofile()``
    to (supposedly) install/restore the profiler for any NEW thread the
    measured block might spawn. That was broken: ``sys.getprofile()``
    (captured as ``prior_profile`` below) and ``threading.setprofile()``
    are two DISTINCT Python globals -- Python 3.9 has no
    ``threading.getprofile()`` to read the thread-bootstrap hook's actual
    prior value before overwriting it, so the teardown's
    ``threading.setprofile(prior_profile)`` silently replaced the CALLER's
    real thread-bootstrap profiler hook with the current-THREAD's hook
    instead (frequently ``None``), corrupting it. Removed entirely rather
    than "fixed" some other way: every measured code path in this module
    (``begin_indexing``/``upsert_points``/``end_indexing``, the sequence
    ``apply_single_file_change_refresh`` wraps) is single-threaded --
    ``FilesystemVectorStore`` only spawns a ``ThreadPoolExecutor`` inside
    ``search()``, which this instrumented block never calls -- so there is
    no new thread here for a thread-bootstrap profiler to ever apply to.

    Exception-safe: the profiler hook is ALWAYS restored, and
    instrumented_wall_time/instrumented_peak_rss are ALWAYS recorded, even
    if the measured block raises -- see the ``finally`` below (the raised
    exception still propagates unchanged; only the instrumentation
    teardown is guaranteed). Touches NO production code path.
    """
    recorder = BoundaryCallRecorder()
    prior_profile = sys.getprofile()
    start_wall = time.perf_counter()
    start_rss = _peak_rss_bytes()
    try:
        sys.setprofile(recorder.profiler)
        yield recorder
    finally:
        sys.setprofile(prior_profile)
        recorder.metrics.instrumented_wall_time_seconds = (
            time.perf_counter() - start_wall
        )
        recorder.metrics.instrumented_peak_rss_delta_bytes = (
            _peak_rss_bytes() - start_rss
        )


# ---------------------------------------------------------------------------
# Synthetic fixture builder (AC21: reproducible, no production access)
# ---------------------------------------------------------------------------


class _NeverInvokedEmbeddingProvider:
    """Placeholder passed as ``embedding_provider`` to ``search()`` --
    every call in this module supplies ``precomputed_query_vector``, so
    this is never actually invoked."""


def _make_vector(seed: int, dim: int) -> List[float]:
    rng = np.random.default_rng(seed)
    vector: List[float] = rng.standard_normal(dim).astype(np.float32).tolist()
    return vector


@dataclass
class SyntheticFixture:
    store: FilesystemVectorStore
    collection_name: str
    base_path: Path
    file_paths: List[str]
    chunks_per_file: int
    use_chunks_db: bool
    point_ids_by_file: Dict[str, List[str]]
    vector_dim: int = DEFAULT_VECTOR_DIM


def build_synthetic_fixture(
    base_path: Path,
    *,
    num_points: int,
    chunks_per_file: int = DEFAULT_CHUNKS_PER_FILE,
    vector_dim: int = DEFAULT_VECTOR_DIM,
    use_chunks_db: bool,
    collection_name: str = "coll",
    hidden_at_bootstrap: Optional[Set[str]] = None,
) -> SyntheticFixture:
    """Build a deterministic synthetic collection with ``num_points``
    points spread evenly across ``num_points // chunks_per_file`` distinct
    files, then perform the initial bootstrap full build (NOT part of any
    measured "refresh" cost -- callers instrument only the subsequent
    single-file-change refresh).

    ``hidden_at_bootstrap`` (optional): a subset of the generated file
    paths to EXCLUDE from the bootstrap's ``visible_files`` set, so their
    points are never added to the initial HNSW graph -- used by callers to
    set up "restore a hidden file" / "hidden stays hidden" correctness
    scenarios for the SAME refresh whose cost is measured (AC52 pairing).
    """
    if chunks_per_file <= 0:
        raise ValueError("chunks_per_file must be positive")
    if num_points % chunks_per_file != 0:
        raise ValueError("num_points must be a multiple of chunks_per_file")

    store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=use_chunks_db
    )
    store.create_collection(collection_name, vector_size=vector_dim)

    num_files = num_points // chunks_per_file
    file_paths = [f"src/module_{i}.py" for i in range(num_files)]
    hidden_at_bootstrap = hidden_at_bootstrap or set()
    bootstrap_visible = {fp for fp in file_paths if fp not in hidden_at_bootstrap}
    point_ids_by_file: Dict[str, List[str]] = {fp: [] for fp in file_paths}

    points = []
    seed = 0
    for file_index, file_path in enumerate(file_paths):
        # A point belonging to a ``hidden_at_bootstrap`` file carries a
        # REAL hidden_branches value from the start (never a bare "[]"),
        # so that later clearing it via _batch_update_payload_only (the
        # "restore" operation) is a genuine payload TRANSITION -- Part C's
        # own no-op-merge semantics (test_no_op_merge_does_not_register_
        # visibility_changed_but_still_bumps_epoch) mean a "[] -> []"
        # clear would never register as a visibility change and the point
        # would never be re-added via the incremental path. The bootstrap
        # exclusion from the initial HNSW graph is still driven by
        # `bootstrap_visible` above (the full loader's own semantics), so
        # this hidden_branches value is inert for the FIRST build and only
        # becomes load-bearing for the later restore.
        point_hidden_branches = (
            [_BOOTSTRAP_BRANCH] if file_path in hidden_at_bootstrap else []
        )
        for chunk_index in range(chunks_per_file):
            point_id = f"pt_{file_index}_{chunk_index}"
            points.append(
                {
                    "id": point_id,
                    "vector": _make_vector(seed, vector_dim),
                    "payload": {
                        "path": file_path,
                        "type": "content",
                        "hidden_branches": list(point_hidden_branches),
                    },
                }
            )
            point_ids_by_file[file_path].append(point_id)
            seed += 1

    store.begin_indexing(collection_name)
    store.upsert_points(collection_name, points)
    store.set_hnsw_branch_context(collection_name, _BOOTSTRAP_BRANCH, bootstrap_visible)
    store.end_indexing(collection_name)

    return SyntheticFixture(
        store=store,
        collection_name=collection_name,
        base_path=Path(base_path),
        file_paths=file_paths,
        chunks_per_file=chunks_per_file,
        use_chunks_db=use_chunks_db,
        point_ids_by_file=point_ids_by_file,
        vector_dim=vector_dim,
    )


def query_ids_for_vector(
    fixture: SyntheticFixture, vector: List[float], limit: int = 5
) -> Set[str]:
    """Real query-result membership helper (AC52), mirroring the
    established pattern in
    ``test_filesystem_vector_store_1575_part_c_decision_engine.py``."""
    results = fixture.store.search(
        query="unused",
        embedding_provider=_NeverInvokedEmbeddingProvider(),
        collection_name=fixture.collection_name,
        limit=limit,
        precomputed_query_vector=vector,
    )
    return {r["id"] for r in results}


# ---------------------------------------------------------------------------
# The measured single-file-change refresh (AC19/AC51/AC52)
#
# Bug #1575 Finding 2 (Codex-reproduced): _apply_content_visibility_
# mutations() used to mutate only fixture.point_ids_by_file[...][0] -- the
# FIRST chunk of hide_file/restore_file -- even though every synthetic
# file has fixture.chunks_per_file chunks (5 by default). A concrete
# repro: after "hiding" a file, its non-first chunk (e.g. pt_2_1) remained
# queryable; after "restoring" a file, its non-first chunk (e.g. pt_1_1)
# remained absent. This meant AC52's correctness pairing never actually
# verified AC4's requirement that ALL chunks of a matched file are
# updated. Fixed by mutating every chunk in the file's point-id list,
# batched into one _batch_update_payload_only call per file.
# ---------------------------------------------------------------------------


def _apply_content_visibility_mutations(
    fixture: SyntheticFixture,
    *,
    target_file: str,
    hide_file: str,
    restore_file: str,
    new_seed: int,
) -> str:
    """Upsert the new content-change point and flip visibility for ALL
    chunks of hide_file/restore_file (via ``_batch_update_payload_only``,
    the SAME bounded mechanism Part C's own AC11(b) test uses), inside the
    caller's active indexing session. Returns the new point's id.

    Bug #1575 Finding 2: previously mutated only chunk index 0 of each
    file, leaving non-first chunks at their prior visibility -- see
    module-level Finding 2 notes for the full rationale.
    """
    store = fixture.store
    collection_name = fixture.collection_name
    hide_point_ids = fixture.point_ids_by_file[hide_file]
    restore_point_ids = fixture.point_ids_by_file[restore_file]
    new_point_id = f"pt_new_{target_file}_{new_seed}"

    store.upsert_points(
        collection_name,
        [
            {
                "id": new_point_id,
                "vector": _make_vector(new_seed, fixture.vector_dim),
                "payload": {
                    "path": target_file,
                    "type": "content",
                    "hidden_branches": [],
                },
            }
        ],
    )
    store._batch_update_payload_only(
        [
            {"id": point_id, "payload": {"hidden_branches": [_BOOTSTRAP_BRANCH]}}
            for point_id in hide_point_ids
        ],
        collection_name,
    )
    store._batch_update_payload_only(
        [
            {"id": point_id, "payload": {"hidden_branches": []}}
            for point_id in restore_point_ids
        ],
        collection_name,
    )
    return new_point_id


def _perform_single_file_change_mutations(
    fixture: SyntheticFixture,
    *,
    target_file: str,
    hide_file: str,
    restore_file: str,
    new_seed: int,
) -> Dict[str, Any]:
    """The actual mutation sequence shared by
    ``apply_single_file_change_refresh`` (instrumented, for call-count
    metrics) and ``measure_single_file_change_refresh_wall_clock``
    (uninstrumented, for an honest wall-clock reading) -- kept in ONE
    place so the two measurement modes can never silently diverge.

    Part A's ``distinct_content_paths``/``fetch_points_for_paths`` run
    WHILE the indexing session is still active (before end_indexing()),
    matching production's own ordering (``hide_files_not_in_branch_
    thread_safe`` runs during an active session) -- required for
    ``distinct_content_paths()`` to hit its fast, in-memory live-session
    path instead of its documented no-active-session disk-scan fallback.
    """
    store = fixture.store
    collection_name = fixture.collection_name

    store.begin_indexing(collection_name)
    new_point_id = _apply_content_visibility_mutations(
        fixture,
        target_file=target_file,
        hide_file=hide_file,
        restore_file=restore_file,
        new_seed=new_seed,
    )
    store.set_hnsw_branch_context(
        collection_name, _BOOTSTRAP_BRANCH, set(fixture.file_paths)
    )
    store.distinct_content_paths(collection_name)
    store.fetch_points_for_paths(collection_name, {target_file})

    result: Dict[str, Any] = store.end_indexing(collection_name)
    fixture.point_ids_by_file[target_file].append(new_point_id)
    return result


def apply_single_file_change_refresh(
    fixture: SyntheticFixture,
    *,
    target_file: str,
    hide_file: str,
    restore_file: str,
    new_seed: int = 999_999,
) -> Tuple[Dict[str, Any], BoundaryMetrics]:
    """Perform ONE realistic single-file-change refresh (see
    ``_perform_single_file_change_mutations`` for the exact sequence),
    measured end-to-end via ``instrument_boundary_calls()``.

    Returns ``(end_indexing_result, metrics)``. ``end_indexing_result``
    includes ``"hnsw_update": "incremental"`` when Part C's decision engine
    took the incremental path (the expected outcome here).

    IMPORTANT for ``metrics.instrumented_wall_time_seconds``/
    ``metrics.instrumented_peak_rss_delta_bytes``:
    ``sys.setprofile`` (which powers the accurate files_opened/
    store_rows_scanned/vectors_materialized/call_counts fields) imposes
    real, well-documented per-call overhead on EVERY Python-level call in
    the process while active -- so the wall-clock/RSS figures measured
    here are "instrumented" time, inflated relative to an uninstrumented
    run, and are reported for relative/orders-of-magnitude comparison
    only (AC20 requires reporting, never a pass/fail threshold, on these
    two fields). For an honest, uninstrumented wall-clock reading of the
    identical sequence, use ``measure_single_file_change_refresh_wall_clock()``
    on a FRESH, disposable fixture instead -- this mutating operation
    cannot safely be re-run a second time on the SAME fixture (a repeat
    call would take the "reuse" decision-engine path, not "incremental").
    """
    with instrument_boundary_calls() as recorder:
        result = _perform_single_file_change_mutations(
            fixture,
            target_file=target_file,
            hide_file=hide_file,
            restore_file=restore_file,
            new_seed=new_seed,
        )
    return result, recorder.metrics


def measure_single_file_change_refresh_wall_clock(
    fixture: SyntheticFixture,
    *,
    target_file: str,
    hide_file: str,
    restore_file: str,
    new_seed: int = 999_999,
) -> Tuple[Dict[str, Any], float, int]:
    """Run the IDENTICAL single-file-change refresh sequence as
    ``apply_single_file_change_refresh`` -- via the SAME shared
    ``_perform_single_file_change_mutations`` helper -- WITHOUT installing
    the call-counting profiler, for an honest, uninstrumented
    ``wall_time_seconds``/``peak_rss_delta_bytes`` reading (AC20). Intended
    for a fresh, disposable fixture never also passed to
    ``apply_single_file_change_refresh``.

    Returns ``(end_indexing_result, wall_time_seconds, peak_rss_delta_bytes)``.
    """
    start_wall = time.perf_counter()
    start_rss = _peak_rss_bytes()
    result = _perform_single_file_change_mutations(
        fixture,
        target_file=target_file,
        hide_file=hide_file,
        restore_file=restore_file,
        new_seed=new_seed,
    )
    wall_time_seconds = time.perf_counter() - start_wall
    peak_rss_delta_bytes = _peak_rss_bytes() - start_rss
    return result, wall_time_seconds, peak_rss_delta_bytes


def measure_forced_full_rebuild_metrics_only(
    fixture: SyntheticFixture, *, use_chunks_db: bool
) -> BoundaryMetrics:
    """Directly invoke ``HNSWIndexManager.rebuild_from_vectors()`` -- the
    exact call EVERY refresh made, unconditionally, before Bug #1575 Part
    C's decision engine existed -- against the fixture's CURRENT on-disk
    state. Demonstrates the discriminating power of the bounded-cost
    assertions: this call's cost scales with the full collection size,
    unlike the incremental path measured by
    ``apply_single_file_change_refresh``.

    DESTRUCTIVE: overwrites ``hnsw_index.bin`` with an unfiltered full
    rebuild. Callers must run this AFTER any correctness assertions on the
    fixture, never before.
    """
    vector_size = fixture.store._get_vector_size(fixture.collection_name)
    hnsw_manager = HNSWIndexManager(vector_dim=vector_size, space="cosine")
    collection_path = fixture.base_path / fixture.collection_name

    with instrument_boundary_calls() as recorder:
        hnsw_manager.rebuild_from_vectors(
            collection_path=collection_path,
            layout_override=ChunkLayout.CHUNKS_DB if use_chunks_db else None,
        )
    return recorder.metrics


# ---------------------------------------------------------------------------
# AC19/AC20/AC21 performance demonstration script
# ---------------------------------------------------------------------------

_AC19_DEMO_POINT_COUNT = 100_000
_AC19_DEMO_CHUNKS_PER_FILE = 5
_AC19_DEMO_RESTORE_FILE = "src/module_0.py"
_AC19_DEMO_HIDDEN_AT_BOOTSTRAP = {_AC19_DEMO_RESTORE_FILE, "src/module_1.py"}
_AC19_DEMO_TARGET_FILE_INDEX = 5
_AC19_DEMO_HIDE_FILE_INDEX = 6


def _print_metrics(label: str, metrics: BoundaryMetrics) -> None:
    print(f"  [{label}]")
    print(f"    files_opened                      = {metrics.files_opened}")
    print(f"    bytes_read                        = {metrics.bytes_read}")
    print(f"    store_rows_scanned                = {metrics.store_rows_scanned}")
    print(f"    vectors_materialized              = {metrics.vectors_materialized}")
    # Bug #1575 Finding 4: these two are profiler-inflated (sys.setprofile
    # overhead on every call) -- NOT a real cost figure. See
    # _report_honest_wall_clock() below for the uninstrumented reading.
    print(
        f"    instrumented_wall_time_seconds    = "
        f"{metrics.instrumented_wall_time_seconds:.4f}  (profiler-inflated)"
    )
    print(
        f"    instrumented_peak_rss_delta_bytes = "
        f"{metrics.instrumented_peak_rss_delta_bytes}  (profiler-inflated)"
    )
    print(f"    call_counts                       = {metrics.call_counts}")
    print(f"    content_paths_returned            = {metrics.content_paths_returned}")
    print(
        f"    fetched_points_req/ret            = "
        f"{metrics.fetched_points_requested}/{metrics.fetched_points_returned}"
    )


def _build_ac19_demo_fixture(base_path: Path, use_chunks_db: bool) -> SyntheticFixture:
    return build_synthetic_fixture(
        base_path,
        num_points=_AC19_DEMO_POINT_COUNT,
        chunks_per_file=_AC19_DEMO_CHUNKS_PER_FILE,
        use_chunks_db=use_chunks_db,
        hidden_at_bootstrap=set(_AC19_DEMO_HIDDEN_AT_BOOTSTRAP),
    )


def _run_ac19_demo(use_chunks_db: bool) -> None:
    layout_label = "CHUNKS_DB" if use_chunks_db else "SHARDED_JSON"
    print(
        f"\n=== AC19/AC20 demonstration -- layout={layout_label} "
        f"({_AC19_DEMO_POINT_COUNT:,} points) ==="
    )
    with tempfile.TemporaryDirectory(prefix="cidx_1575_measure_") as tmp:
        fixture = _build_ac19_demo_fixture(Path(tmp), use_chunks_db)
        target_file = fixture.file_paths[_AC19_DEMO_TARGET_FILE_INDEX]
        hide_file = fixture.file_paths[_AC19_DEMO_HIDE_FILE_INDEX]

        after_result, after_metrics = apply_single_file_change_refresh(
            fixture,
            target_file=target_file,
            hide_file=hide_file,
            restore_file=_AC19_DEMO_RESTORE_FILE,
        )
        print(
            f"AFTER (current shipped code; "
            f"hnsw_update={after_result.get('hnsw_update', 'full_rebuild')}):"
        )
        _print_metrics("after (incremental)", after_metrics)
        if not use_chunks_db:
            print(
                "  CAVEAT: files_opened=0/vectors_materialized=0 above does NOT "
                "mean this refresh is O(1). end_indexing() unconditionally reruns "
                "_calculate_and_save_unique_file_count(), which for SHARDED_JSON "
                "always opens+parses every remaining vector_*.json file "
                "(project-owner FINAL decision -- see this module's docstring). "
                "That cost is real and size-correlated but untracked by the 5 "
                "metrics above; see the honest wall-clock reading below."
            )

        before_metrics = measure_forced_full_rebuild_metrics_only(
            fixture, use_chunks_db=use_chunks_db
        )
        print(
            "BEFORE (pre-Part-C equivalent -- direct unconditional "
            "rebuild_from_vectors call over the SAME collection):"
        )
        _print_metrics("before (full rebuild)", before_metrics)

    _report_honest_wall_clock(use_chunks_db)


def _report_honest_wall_clock(use_chunks_db: bool) -> None:
    """AC20: an uninstrumented (no sys.setprofile overhead) wall-clock/RSS
    reading of the IDENTICAL single-file-change refresh, on a fresh twin
    fixture -- see ``apply_single_file_change_refresh``'s docstring for
    why the instrumented run's own wall-clock figure is not representative.
    """
    with tempfile.TemporaryDirectory(prefix="cidx_1575_measure_twin_") as tmp:
        twin = _build_ac19_demo_fixture(Path(tmp), use_chunks_db)
        _, wall_time_seconds, peak_rss_delta_bytes = (
            measure_single_file_change_refresh_wall_clock(
                twin,
                target_file=twin.file_paths[_AC19_DEMO_TARGET_FILE_INDEX],
                hide_file=twin.file_paths[_AC19_DEMO_HIDE_FILE_INDEX],
                restore_file=_AC19_DEMO_RESTORE_FILE,
            )
        )
        print(
            f"  [after (incremental), UNINSTRUMENTED wall-clock] "
            f"wall_time_seconds={wall_time_seconds:.4f} "
            f"peak_rss_delta_bytes={peak_rss_delta_bytes}"
        )


def main() -> None:
    _run_ac19_demo(use_chunks_db=False)
    _run_ac19_demo(use_chunks_db=True)


if __name__ == "__main__":
    main()
