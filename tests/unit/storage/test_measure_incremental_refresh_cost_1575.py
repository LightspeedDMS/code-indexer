"""TDD tests for the Bug #1575 AC-M1 measurement utility
(``scripts/analysis/measure_incremental_refresh_cost_1575.py``).

This utility wraps FOUR production boundary calls FROM OUTSIDE (never
patching/subclassing them -- Messi Rule #17) via a ``sys.setprofile``-based
call recorder matched by exact code-object identity:

  * ``HNSWIndexManager._load_vectors_from_json_files``  (SHARDED_JSON)
  * ``HNSWIndexManager._load_vectors_from_chunks_db``    (CHUNKS_DB)
  * ``FilesystemVectorStore.distinct_content_paths``     (Part A)
  * ``FilesystemVectorStore.fetch_points_for_paths``     (Part A)

It is the SAME utility reused by the permanent AC51/AC52 scaling-invariance
regression test in
``tests/unit/storage/test_filesystem_vector_store_1575_measurement_scaling.py``.

Real ``FilesystemVectorStore`` + real filesystem/SQLite throughout -- no
mocking of the code under test.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).parents[3]
_SCRIPT_PATH = (
    _PROJECT_ROOT / "scripts" / "analysis" / "measure_incremental_refresh_cost_1575.py"
)


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "measure_incremental_refresh_cost_1575", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _mut():
    return _load_module()


# ---------------------------------------------------------------------------
# Fixture builder sanity
# ---------------------------------------------------------------------------


def test_build_synthetic_fixture_produces_expected_point_and_file_counts(tmp_path):
    mut = _mut()
    fixture = mut.build_synthetic_fixture(
        tmp_path, num_points=50, chunks_per_file=5, use_chunks_db=False
    )
    assert len(fixture.file_paths) == 10
    assert sum(len(v) for v in fixture.point_ids_by_file.values()) == 50


def test_build_synthetic_fixture_supports_hidden_at_bootstrap(tmp_path):
    mut = _mut()
    hidden = {"src/module_0.py"}
    fixture = mut.build_synthetic_fixture(
        tmp_path,
        num_points=50,
        chunks_per_file=5,
        use_chunks_db=False,
        hidden_at_bootstrap=hidden,
    )
    # The hidden-at-bootstrap file's points must NOT be queryable yet.
    hidden_point_id = fixture.point_ids_by_file["src/module_0.py"][0]
    results = fixture.store.search(
        query="unused",
        embedding_provider=mut._NeverInvokedEmbeddingProvider(),
        collection_name=fixture.collection_name,
        limit=50,
        precomputed_query_vector=mut._make_vector(0, mut.DEFAULT_VECTOR_DIM),
    )
    ids = {r["id"] for r in results}
    assert hidden_point_id not in ids


# ---------------------------------------------------------------------------
# Boundary call recorder: SHARDED_JSON loader
# ---------------------------------------------------------------------------


def test_json_loader_call_is_recorded_with_files_opened_and_vectors_materialized(
    tmp_path,
):
    mut = _mut()
    fixture = mut.build_synthetic_fixture(
        tmp_path, num_points=15, chunks_per_file=5, use_chunks_db=False
    )
    # A forced full rebuild always invokes the JSON loader exactly once,
    # touching every vector_*.json file on disk (15 points).
    # measure_forced_full_rebuild_metrics_only() already instruments its
    # OWN call internally and returns the resulting metrics directly --
    # wrapping it in a second instrument_boundary_calls() here would
    # replace the profiler set by the inner call, so the outer recorder
    # would never observe anything.
    metrics = mut.measure_forced_full_rebuild_metrics_only(fixture, use_chunks_db=False)
    assert metrics.call_counts.get("_load_vectors_from_json_files") == 1
    assert metrics.files_opened == 15
    assert metrics.vectors_materialized == 15
    assert metrics.bytes_read > 0
    assert metrics.call_counts.get("_load_vectors_from_chunks_db", 0) == 0


# ---------------------------------------------------------------------------
# Boundary call recorder: CHUNKS_DB loader
# ---------------------------------------------------------------------------


def test_chunks_db_loader_call_is_recorded_with_rows_scanned(tmp_path):
    mut = _mut()
    fixture = mut.build_synthetic_fixture(
        tmp_path, num_points=15, chunks_per_file=5, use_chunks_db=True
    )
    # See the SHARDED_JSON counterpart test above for why this calls the
    # measurement helper directly rather than double-wrapping it.
    metrics = mut.measure_forced_full_rebuild_metrics_only(fixture, use_chunks_db=True)
    assert metrics.call_counts.get("_load_vectors_from_chunks_db") == 1
    assert metrics.store_rows_scanned == 15
    assert metrics.vectors_materialized == 15
    assert metrics.bytes_read > 0
    assert metrics.call_counts.get("_load_vectors_from_json_files", 0) == 0


# ---------------------------------------------------------------------------
# The core scaling proof: incremental refresh calls NEITHER loader
# ---------------------------------------------------------------------------


def test_incremental_single_file_refresh_invokes_neither_full_loader_sharded_json(
    tmp_path,
):
    mut = _mut()
    fixture = mut.build_synthetic_fixture(
        tmp_path, num_points=50, chunks_per_file=5, use_chunks_db=False
    )
    result, metrics = mut.apply_single_file_change_refresh(
        fixture,
        target_file=fixture.file_paths[0],
        hide_file=fixture.file_paths[1],
        restore_file=fixture.file_paths[2],
    )
    assert result.get("hnsw_update") == "incremental"
    assert metrics.call_counts.get("_load_vectors_from_json_files", 0) == 0
    assert metrics.files_opened == 0
    assert metrics.vectors_materialized == 0


def test_incremental_single_file_refresh_invokes_neither_full_loader_chunks_db(
    tmp_path,
):
    mut = _mut()
    fixture = mut.build_synthetic_fixture(
        tmp_path, num_points=50, chunks_per_file=5, use_chunks_db=True
    )
    result, metrics = mut.apply_single_file_change_refresh(
        fixture,
        target_file=fixture.file_paths[0],
        hide_file=fixture.file_paths[1],
        restore_file=fixture.file_paths[2],
    )
    assert result.get("hnsw_update") == "incremental"
    assert metrics.call_counts.get("_load_vectors_from_chunks_db", 0) == 0
    assert metrics.store_rows_scanned == 0
    assert metrics.vectors_materialized == 0


# ---------------------------------------------------------------------------
# Part A boundaries: distinct_content_paths / fetch_points_for_paths
# ---------------------------------------------------------------------------


def test_part_a_boundaries_are_recorded_during_refresh(tmp_path):
    mut = _mut()
    fixture = mut.build_synthetic_fixture(
        tmp_path, num_points=50, chunks_per_file=5, use_chunks_db=True
    )
    _, metrics = mut.apply_single_file_change_refresh(
        fixture,
        target_file=fixture.file_paths[0],
        hide_file=fixture.file_paths[1],
        restore_file=fixture.file_paths[2],
    )
    assert metrics.call_counts.get("distinct_content_paths") == 1
    assert metrics.call_counts.get("fetch_points_for_paths") == 1
    # Targeted fetch for ONE file must return only that file's current
    # chunks, never the whole collection. Story #540's pre-existing
    # per-file orphan-cleanup (see the AC11(c)/(d) comments in
    # test_filesystem_vector_store_1575_part_c_decision_engine.py) replaces
    # ALL of target_file's previous chunks with the single new chunk
    # upserted for it in this refresh, so exactly 1 point remains stored
    # for that file -- still a small constant, independent of collection
    # size.
    assert metrics.fetched_points_returned == 1


# ---------------------------------------------------------------------------
# Exception safety
# ---------------------------------------------------------------------------


def test_profiler_is_restored_even_when_measured_block_raises():
    mut = _mut()
    prior = sys.getprofile()
    try:
        with mut.instrument_boundary_calls():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert sys.getprofile() is prior


def test_metrics_wall_time_and_rss_recorded_even_on_exception():
    mut = _mut()
    try:
        with mut.instrument_boundary_calls() as recorder:
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert recorder.metrics.instrumented_wall_time_seconds >= 0.0


# ---------------------------------------------------------------------------
# Known, documented omission: the SHARDED_JSON unique-file-count rescan.
# See this module's own docstring for the full rationale (project-owner
# FINAL decision to abandon the PathIndex fast-path shortcut for
# _calculate_and_save_unique_file_count -- see
# test_filesystem_vector_store_1575_sharded_json_shortcut_abandoned.py).
# Uses ``sys``/``Any``, both already imported at module top (lines 24/26).
# ---------------------------------------------------------------------------


def test_sharded_json_unique_file_count_rescan_runs_every_refresh_uninstrumented(
    tmp_path,
):
    """Anchors this module's docstring disclosure: the unique-file-count
    rescan is real and untracked, so it can't silently go stale. Uses an
    INDEPENDENT sys.setprofile counter (never instrument_boundary_calls()
    -- Finding 3's single-active-profiler lesson) around the uninstrumented
    wall-clock helper to prove _rebuild_path_index_from_disk runs exactly
    once per SHARDED_JSON refresh.
    """
    mut = _mut()
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    fixture = mut.build_synthetic_fixture(
        tmp_path, num_points=25, chunks_per_file=5, use_chunks_db=False
    )

    calls: list = []
    target_code = FilesystemVectorStore._rebuild_path_index_from_disk.__code__

    def _counter(frame: Any, event: str, arg: Any) -> None:
        if event == "call" and frame.f_code is target_code:
            calls.append(1)

    prior = sys.getprofile()
    try:
        sys.setprofile(_counter)
        mut.measure_single_file_change_refresh_wall_clock(
            fixture,
            target_file=fixture.file_paths[0],
            hide_file=fixture.file_paths[1],
            restore_file=fixture.file_paths[2],
        )
    finally:
        sys.setprofile(prior)

    assert len(calls) == 1, (
        "expected _rebuild_path_index_from_disk to run exactly once -- if "
        "this changed, update this module's docstring disclosure to match"
    )


def test_threading_setprofile_hook_is_not_clobbered():
    """Bug #1575 Finding 3 (dual review): instrument_boundary_calls() used
    to call threading.setprofile(prior_profile) in its teardown, where
    prior_profile was actually sys.getprofile()'s value (a DIFFERENT
    Python global from the thread-bootstrap profile hook threading.
    setprofile() controls) -- silently clobbering any real pre-existing
    threading profiler hook with the wrong value (frequently None).

    This test installs a distinct sentinel threading profile hook BEFORE
    using the context manager and asserts it survives untouched
    afterwards -- proving the fix (instrument_boundary_calls() no longer
    touches threading.setprofile at all).
    """
    import threading

    mut = _mut()

    def _sentinel_hook(frame, event, arg):
        return None

    threading.setprofile(_sentinel_hook)
    try:
        with mut.instrument_boundary_calls():
            pass
        # threading._profile_hook is the CPython-internal global that
        # threading.setprofile() sets -- there is no public getter in
        # Python 3.9, so reading it directly is the only way to prove the
        # hook was left untouched.
        assert threading._profile_hook is _sentinel_hook
    finally:
        threading.setprofile(None)
