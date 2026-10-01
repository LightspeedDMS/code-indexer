"""Bug #1575 Fix 4 -- O(collection-size) performance regression on the
non-git incremental delete path.

``smart_indexer.py``'s non-git-aware incremental reconcile path
(``_do_reconcile_with_database``) calls ``delete_by_filter()`` ->
``delete_points()`` ONCE PER MODIFIED FILE, ALL BEFORE ``begin_indexing()``
is called. Every one of those per-file deletes therefore takes the
out-of-session-persist path (Bug #1575 Round 3 Fix B), and
``_save_path_index()`` ALSO co-persists the ENTIRE ``id_index.bin`` on
every single call (``IDIndexManager.save_index`` -- an O(collection-size)
rewrite + two fsyncs). Measured (dual review): 5.6x slower incremental
refresh on a 4000-file collection.

This test isolates the underlying MECHANISM (not smart_indexer.py's call
site, which is fixed separately in ``services/smart_indexer.py``) against
a REAL ``FilesystemVectorStore`` + real filesystem: performing M per-file
deletes OUT of any indexing session (mirroring the pre-fix call pattern)
versus performing the SAME M deletes bracketed inside ONE
``begin_indexing()``/``end_indexing()`` session (mirroring the post-fix
call pattern, Option (a) -- hoisting the delete loop into a session
bracket so in-session deletes are tracked via
``_indexing_session_changes`` and persisted ONCE at ``end_indexing()``,
never once per delete). A trailing no-op
``begin_indexing()``/``end_indexing()`` is added to the "before" scenario
so BOTH scenarios include exactly one real indexing-session finalization
-- an apples-to-apples comparison of "M deletes then finalize" (pre-fix
ordering) versus "finalize covers the M deletes" (post-fix ordering),
never "finalize never happens at all" for either side.

PRIMARY assertion: the number of ``IDIndexManager.save_index`` calls (the
actual O(collection-size) rewrite-and-fsync this fix eliminates per
delete) drops from ``M + 2`` (M out-of-session persists, plus the 2 calls
end_indexing() itself already makes once) down to exactly ``2`` --
deterministic and environment-independent, unlike wall-clock timing,
which in THIS environment is dominated by an unrelated per-call fsync cost
(Bug #1575 Part C's HNSW dirty-marking protocol, present on every
delete_points() call regardless of session state) that dilutes -- without
invalidating -- the wall-clock speedup ratio. Wall-clock is still measured
and reported as corroborating evidence (never gated), following this
codebase's own established "AC20: wall_time is a REPORTED measurement,
never gated" precedent (see
``test_filesystem_vector_store_1575_measurement_scaling.py``).

Uses ``sys.setprofile`` with code-object identity matching -- the SAME
non-mocking instrumentation technique
``scripts/analysis/measure_incremental_refresh_cost_1575.py`` already
established for observing production code from outside (Messi Rule #17:
never patch/subclass the observed function). The prior profiler (if any)
is captured and restored, never blindly cleared to None.

No mocking of the code under test -- real disk I/O, real fsyncs.
"""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, List

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.id_index_manager import IDIndexManager

VECTOR_DIM = 8
TOTAL_FILE_COUNT = 4_000
DELETED_FILE_COUNT = 60
# end_indexing() itself calls IDIndexManager.save_index THREE times per
# finalization for a SHARDED_JSON collection -- a pre-existing,
# unrelated-to-THIS-fix redundancy that both scenarios incur identically
# exactly once, so it is accounted for here rather than treated as part of
# the regression being measured. This was 2 (one direct call, one via
# _save_path_index()'s id_index co-persist) until Bug #1575's LATER
# project-owner decision to abandon the SHARDED_JSON PathIndex fast-path
# entirely: _calculate_and_save_unique_file_count() now unconditionally
# calls _rebuild_and_repair_path_index(), which internally calls
# _save_path_index() (its own id_index co-persist) as an extra,
# unconditional cost of every finalization -- ON TOP OF end_indexing()'s
# own separate, subsequent _save_path_index() call. This is a deliberate,
# accepted cost of THAT decision (correctness over speed after 6 rounds of
# confirmed bugs in the abandoned mechanism), not a regression of Fix 4's
# own property below, which is unaffected: both scenarios still incur this
# per-finalization cost identically exactly once, so the O(deletes) ->
# O(1) collapse this test exists to prove is unchanged.
SAVE_INDEX_CALLS_PER_FINALIZE = 3
EXPECTED_SAVE_INDEX_CALLS_BEFORE = DELETED_FILE_COUNT + SAVE_INDEX_CALLS_PER_FINALIZE
EXPECTED_SAVE_INDEX_CALLS_AFTER = SAVE_INDEX_CALLS_PER_FINALIZE

_SAVE_INDEX_CODE = IDIndexManager.save_index.__code__


@contextmanager
def _count_save_index_calls(counts: List[int]) -> Iterator[None]:
    """sys.setprofile-based counter of IDIndexManager.save_index calls,
    matched by code-object identity (never by patching/subclassing the
    observed method -- Messi Rule #17). Captures and restores whatever
    profiler was previously installed, rather than blindly clearing it.
    """
    previous_profile = sys.getprofile()

    def _tracer(frame: Any, event: str, arg: Any) -> None:
        if event == "call" and frame.f_code is _SAVE_INDEX_CODE:
            counts[0] += 1

    sys.setprofile(_tracer)
    try:
        yield
    finally:
        sys.setprofile(previous_profile)


def _make_vector(seed: int):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist()


def _build_large_sharded_json_collection(base_path: Path) -> FilesystemVectorStore:
    store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=False
    )
    collection_name = "coll"
    store.create_collection(collection_name, vector_size=VECTOR_DIM)
    store.begin_indexing(collection_name)
    points = [
        {
            "id": f"pt_{i}",
            "vector": _make_vector(i),
            "payload": {
                "path": f"src/module_{i}.py",
                "type": "content",
                "hidden_branches": [],
            },
        }
        for i in range(TOTAL_FILE_COUNT)
    ]
    store.upsert_points(collection_name, points)
    store.end_indexing(collection_name)
    return store


def _delete_out_of_session_then_finalize(
    store: FilesystemVectorStore, collection_name: str, point_ids: list
) -> None:
    """Mirrors the PRE-FIX call pattern: one delete_points() call PER
    FILE, with NO active indexing session (each takes the
    out-of-session-persist path), followed by the SAME trailing
    begin_indexing()/end_indexing() finalization the "after" scenario
    also performs -- so both scenarios include exactly one real
    finalization, isolating the M extra out-of-session persists as the
    ONLY difference.
    """
    assert collection_name not in store._indexing_session_changes
    for point_id in point_ids:
        result = store.delete_points(collection_name, [point_id])
        assert result["status"] == "ok"
    store.begin_indexing(collection_name)
    store.end_indexing(collection_name)


def _delete_bracketed_in_one_session(
    store: FilesystemVectorStore, collection_name: str, point_ids: list
) -> None:
    """Mirrors the POST-FIX call pattern (Fix 4 Option (a)): the SAME
    per-file delete_points() calls, but bracketed inside ONE
    begin_indexing()/end_indexing() session -- in-session deletes are
    tracked via _indexing_session_changes and persisted ONCE at
    end_indexing(), never once per delete.
    """
    store.begin_indexing(collection_name)
    for point_id in point_ids:
        result = store.delete_points(collection_name, [point_id])
        assert result["status"] == "ok"
    store.end_indexing(collection_name)


def _measure_scenario(fn) -> tuple:
    """Run fn() under the save_index call counter, returning
    (save_index_call_count, wall_clock_seconds)."""
    counts = [0]
    start = time.perf_counter()
    with _count_save_index_calls(counts):
        fn()
    elapsed = time.perf_counter() - start
    return counts[0], elapsed


@pytest.mark.slow
def test_session_bracketed_deletes_eliminate_per_delete_id_index_persist(tmp_path):
    before_store = _build_large_sharded_json_collection(tmp_path / "before")
    before_point_ids = [f"pt_{i}" for i in range(DELETED_FILE_COUNT)]
    before_calls, before_elapsed = _measure_scenario(
        lambda: _delete_out_of_session_then_finalize(
            before_store, "coll", before_point_ids
        )
    )

    after_store = _build_large_sharded_json_collection(tmp_path / "after")
    after_point_ids = [f"pt_{i}" for i in range(DELETED_FILE_COUNT)]
    after_calls, after_elapsed = _measure_scenario(
        lambda: _delete_bracketed_in_one_session(after_store, "coll", after_point_ids)
    )

    speedup = before_elapsed / after_elapsed if after_elapsed > 0 else float("inf")
    print(
        f"\n[Fix 4 perf report] out-of-session: save_index_calls="
        f"{before_calls}, wall_time={before_elapsed:.4f}s | "
        f"session-bracketed: save_index_calls={after_calls}, "
        f"wall_time={after_elapsed:.4f}s | wall_time speedup={speedup:.2f}x "
        f"(collection_size={TOTAL_FILE_COUNT}, deletes={DELETED_FILE_COUNT})"
    )

    assert before_calls == EXPECTED_SAVE_INDEX_CALLS_BEFORE, (
        f"expected {EXPECTED_SAVE_INDEX_CALLS_BEFORE} IDIndexManager."
        f"save_index() calls for {DELETED_FILE_COUNT} out-of-session "
        f"deletes plus one finalization, got {before_calls} -- this pins "
        f"the REGRESSION this fix eliminates (one full id_index.bin "
        f"rewrite PER out-of-session delete)"
    )
    assert after_calls == EXPECTED_SAVE_INDEX_CALLS_AFTER, (
        f"expected exactly {EXPECTED_SAVE_INDEX_CALLS_AFTER} "
        f"IDIndexManager.save_index() calls (only end_indexing()'s own "
        f"finalization) for {DELETED_FILE_COUNT} SESSION-BRACKETED "
        f"deletes, got {after_calls} -- bracketing the delete loop inside "
        f"begin_indexing()/end_indexing() must eliminate the per-delete "
        f"id_index.bin co-persist entirely, regardless of how many files "
        f"are deleted in that session"
    )
