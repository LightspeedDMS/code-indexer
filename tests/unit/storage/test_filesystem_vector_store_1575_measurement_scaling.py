"""Bug #1575 AC51/AC52: permanent scaling-invariance + correctness
regression test for the visibility-epoch decision engine (Part C) and the
authoritative content-path enumeration (Part A).

This is the PERMANENT regression test (not a one-off script run) that
proves the scaling invariance described in the issue's Measurement
Methodology: the SAME single-file-change refresh, run against two
synthetic fixtures of substantially different total size, produces
``files_opened``/``store_rows_scanned``/``vectors_materialized`` that are
BOUNDED BY A SMALL CONSTANT across the size difference -- not proportional
to it. Both storage layouts (SHARDED_JSON and CHUNKS_DB) are covered,
matching AC15/AC16's dual-layout requirement.

AC52 (cost + correctness pairing, mandatory): the test function ALSO
asserts, for the SAME refreshed collection, real query-result correctness
-- hidden files remain hidden, visible files remain visible, restored
files become visible again, and the content change itself is queryable.
This reuses the AC1/AC2/AC8/AC11-style real-search-result-membership
pattern established in
``test_filesystem_vector_store_1575_part_c_decision_engine.py`` and
``test_hnsw_branch_isolation.py`` -- never code inspection.

AC41 (discriminating power): the test also runs
``measure_forced_full_rebuild_metrics_only()`` -- a DIRECT call to
``HNSWIndexManager.rebuild_from_vectors()``, the exact call every refresh
made UNCONDITIONALLY before Bug #1575 Part C's decision engine existed --
against the SAME (already-refreshed) fixture, proving the assertions above
are not vacuously true: the "before" numbers scale with collection size
while the "after" numbers measured against the current shipped decision
engine do not. This full-rebuild call is DESTRUCTIVE (overwrites
``hnsw_index.bin``), but it only ever touches ``pytest``'s own ``tmp_path``
fixture directory -- an ephemeral per-test directory pytest removes
regardless of pass/fail -- so no shared or production resource is ever at
risk and no additional cleanup/restoration is needed.

Marked ``@pytest.mark.slow`` because the LARGE fixtures are legitimately
large (CLAUDE.md fast-test discipline).

Uses the SAME measurement utility as AC19/AC20/AC21's performance
demonstration (``scripts/analysis/measure_incremental_refresh_cost_1575.py``,
AC-M1) -- not a second, divergent measurement technique. That module lives
under ``scripts/analysis/`` (per AC-M1's explicit placement requirement),
outside the installable ``code_indexer`` package, so it has no importable
dotted module path. It is loaded via ``importlib.util.spec_from_file_location``
-- the SAME established, precedented technique
``tests/unit/scripts/test_temporal_vector_projection_1292.py`` already uses
in this codebase for the identical "load a script from scripts/analysis/ by
file path" problem (chosen there, and here, specifically so mypy is never
asked to resolve a dotted import for a path that only exists via a
test-time ``sys.path`` mutation -- the returned module is treated as
untyped, matching that file's own ``-> Any`` return type).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

_PROJECT_ROOT = Path(__file__).parents[3]
_SCRIPT_PATH = (
    _PROJECT_ROOT / "scripts" / "analysis" / "measure_incremental_refresh_cost_1575.py"
)

_CHUNKS_PER_FILE = 5
_SMALL_POINTS = 2_000
# SHARDED_JSON writes one file per vector durably to disk -- kept smaller
# than CHUNKS_DB's LARGE size to keep this permanent test's runtime
# reasonable while still being "substantially different" from SMALL (20x).
# CHUNKS_DB's bulk SQLite writes are fast enough to hit AC19's own
# >=100,000-point figure directly inside this permanent test (50x).
_LARGE_POINTS: Dict[bool, int] = {False: 40_000, True: 100_000}
# AC51's "bounded by a small constant" tolerance -- the incremental path's
# named-boundary-function metrics are expected to be EXACTLY 0 regardless
# of collection size (neither loader is ever called), so this is generous
# slack, not a number being fitted to make the test pass.
_BOUNDED_CONSTANT = 10
_QUERY_LIMIT = 10
_FIRST_CHUNK_INDEX = 0
_NEW_CONTENT_SEED = 999_999
# Loose proportionality check on the "before" (forced-full-rebuild)
# numbers only -- never on "after". Real full-table/full-directory scans
# can undercount slightly (e.g. a handful of points were replaced by the
# refresh itself), so 50% of the naive linear-scaling expectation is used
# as a conservative, non-flaky floor.
_PROPORTIONALITY_TOLERANCE = 0.5

# Synthetic file roles used by every case (module_0 is the content-change
# target; the others are distinct files so touched vs. untouched behavior
# is unambiguous).
_TARGET_FILE = "src/module_0.py"
_RESTORE_FILE = "src/module_1.py"
_HIDE_FILE = "src/module_2.py"
_ALREADY_HIDDEN_UNTOUCHED_FILE = "src/module_3.py"
_UNTOUCHED_VISIBLE_FILE = "src/module_4.py"


def _load_measurement_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "measure_incremental_refresh_cost_1575", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _seed_for(
    fixture: Any, file_path: str, chunk_index: int = _FIRST_CHUNK_INDEX
) -> int:
    """The deterministic vector seed the fixture builder assigned to
    ``file_path``'s ``chunk_index``-th point -- see
    ``build_synthetic_fixture``'s point-generation loop, which increments
    a single global seed counter in (file_index, chunk_index) order."""
    file_index = fixture.file_paths.index(file_path)
    return int(file_index * fixture.chunks_per_file + chunk_index)


def _point_id(
    fixture: Any, file_path: str, chunk_index: int = _FIRST_CHUNK_INDEX
) -> str:
    """The deterministic point id ``build_synthetic_fixture`` assigned to
    ``file_path``'s ``chunk_index``-th point."""
    return f"pt_{fixture.file_paths.index(file_path)}_{chunk_index}"


def _build_and_refresh(
    tmp_path: Path, mut: Any, *, use_chunks_db: bool, num_points: int, suffix: str
) -> Any:
    """Build one synthetic fixture and perform the ONE measured
    single-file-change refresh (content change + hide + restore).
    Returns ``(fixture, end_indexing_result, after_metrics)``.
    """
    base_path = tmp_path / f"fixture_{suffix}"
    hidden_at_bootstrap = {_RESTORE_FILE, _ALREADY_HIDDEN_UNTOUCHED_FILE}

    fixture = mut.build_synthetic_fixture(
        base_path,
        num_points=num_points,
        chunks_per_file=_CHUNKS_PER_FILE,
        use_chunks_db=use_chunks_db,
        hidden_at_bootstrap=hidden_at_bootstrap,
    )
    after_result, after_metrics = mut.apply_single_file_change_refresh(
        fixture,
        target_file=_TARGET_FILE,
        hide_file=_HIDE_FILE,
        restore_file=_RESTORE_FILE,
    )
    return fixture, after_result, after_metrics


def _assert_refresh_correctness(fixture: Any, mut: Any) -> None:
    """AC52: real query-result correctness for the SAME refreshed
    collection ``_build_and_refresh`` just produced -- hidden stays
    hidden, visible stays visible, restored becomes visible, and the
    content change itself is queryable. Real ``search()`` calls only,
    never code inspection.
    """

    def _ids_for_seed(seed: int) -> Any:
        vector = mut._make_vector(seed, fixture.vector_dim)
        return mut.query_ids_for_vector(fixture, vector, limit=_QUERY_LIMIT)

    new_point_id = fixture.point_ids_by_file[_TARGET_FILE][-1]
    assert new_point_id in _ids_for_seed(_NEW_CONTENT_SEED), (
        "the new content change must be queryable"
    )

    old_target_ids = _ids_for_seed(_seed_for(fixture, _TARGET_FILE))
    assert _point_id(fixture, _TARGET_FILE) not in old_target_ids, (
        "target_file's PRIOR chunks must be gone (Story #540 per-file "
        "orphan-cleanup replaced them with the new chunk)"
    )

    # Bug #1575 Finding 2 (AC4): ALL chunks of hide_file/restore_file must
    # be updated, not only the first -- each synthetic file has
    # fixture.chunks_per_file chunks (5 by default), so checking only
    # chunk_index 0 would miss a bug where non-first chunks are silently
    # left in their old visibility state.
    for chunk_index in range(fixture.chunks_per_file):
        hide_ids = _ids_for_seed(_seed_for(fixture, _HIDE_FILE, chunk_index))
        assert _point_id(fixture, _HIDE_FILE, chunk_index) not in hide_ids, (
            f"hide_file chunk {chunk_index} must become hidden by this refresh"
        )

    for chunk_index in range(fixture.chunks_per_file):
        restore_ids = _ids_for_seed(_seed_for(fixture, _RESTORE_FILE, chunk_index))
        assert _point_id(fixture, _RESTORE_FILE, chunk_index) in restore_ids, (
            f"restore_file chunk {chunk_index} must become visible again by "
            f"this refresh"
        )

    already_hidden_ids = _ids_for_seed(
        _seed_for(fixture, _ALREADY_HIDDEN_UNTOUCHED_FILE)
    )
    assert (
        _point_id(fixture, _ALREADY_HIDDEN_UNTOUCHED_FILE) not in already_hidden_ids
    ), "a file hidden BEFORE this refresh and never touched must stay hidden"

    untouched_ids = _ids_for_seed(_seed_for(fixture, _UNTOUCHED_VISIBLE_FILE))
    assert _point_id(fixture, _UNTOUCHED_VISIBLE_FILE) in untouched_ids, (
        "a file never touched by this refresh must remain visible"
    )


def _run_scaling_case(
    tmp_path: Path, mut: Any, *, use_chunks_db: bool, num_points: int, suffix: str
) -> Any:
    """Full per-fixture-size case: build + refresh (measured), assert
    correctness for that refresh (AC52), then measure the forced-full-
    rebuild baseline (AC41 discriminating power). Returns
    ``(after_metrics, before_metrics)``.
    """
    fixture, after_result, after_metrics = _build_and_refresh(
        tmp_path, mut, use_chunks_db=use_chunks_db, num_points=num_points, suffix=suffix
    )

    # Part C's decision engine must actually have taken the incremental
    # path -- if it silently fell back to a full rebuild, the bounded-cost
    # assertions in the caller would be meaningless (this pins AC41's
    # "would fail against old code" property structurally: the OLD,
    # pre-Part-C code had no such branch at all -- every call was a full
    # rebuild).
    assert after_result.get("hnsw_update") == "incremental"

    _assert_refresh_correctness(fixture, mut)

    # DESTRUCTIVE (overwrites hnsw_index.bin) -- always last, and always
    # confined to pytest's own ephemeral tmp_path (see module docstring).
    before_metrics = mut.measure_forced_full_rebuild_metrics_only(
        fixture, use_chunks_db=use_chunks_db
    )
    return after_metrics, before_metrics


def _assert_after_bounded(
    *, loader_call_name: str, metric_name: str, after_small: Any, after_large: Any
) -> None:
    """AC51: bounded by a small constant across the size difference, NOT
    proportional to it -- the current shipped incremental path never
    invokes the named full-loader boundary at all for a single-file
    change, regardless of collection size.
    """
    after_small_val = getattr(after_small, metric_name)
    after_large_val = getattr(after_large, metric_name)

    assert after_small.call_counts.get(loader_call_name, 0) == 0
    assert after_large.call_counts.get(loader_call_name, 0) == 0
    assert after_small_val == 0
    assert after_large_val == 0
    assert after_large_val <= after_small_val + _BOUNDED_CONSTANT
    assert after_small.vectors_materialized == 0
    assert after_large.vectors_materialized == 0

    # Part A boundaries: a targeted fetch for ONE file returns a small,
    # size-independent count (Story #540 replaced target_file's chunks
    # with exactly 1 new chunk in this refresh).
    assert after_small.fetched_points_returned == after_large.fetched_points_returned
    assert after_large.fetched_points_returned <= _BOUNDED_CONSTANT


def _assert_before_proportional(
    *,
    loader_call_name: str,
    metric_name: str,
    small_points: int,
    large_points: int,
    before_small: Any,
    before_large: Any,
    after_large_val: int,
) -> None:
    """AC41: discriminating power, proven in THIS SAME run. The old,
    unconditional-full-rebuild behavior this test protects against IS
    reproduced directly via measure_forced_full_rebuild_metrics_only(),
    and its cost scales with collection size -- so ``_assert_after_bounded``
    above is not vacuously true for any fixed metric value.
    """
    before_small_val = getattr(before_small, metric_name)
    before_large_val = getattr(before_large, metric_name)

    assert before_small.call_counts.get(loader_call_name) == 1
    assert before_large.call_counts.get(loader_call_name) == 1
    assert before_small_val >= small_points * _PROPORTIONALITY_TOLERANCE
    assert before_large_val >= large_points * _PROPORTIONALITY_TOLERANCE
    assert before_large_val > after_large_val + _BOUNDED_CONSTANT
    size_ratio = large_points / small_points
    assert before_large_val >= before_small_val * (
        size_ratio * _PROPORTIONALITY_TOLERANCE
    )


@pytest.mark.slow
@pytest.mark.parametrize(
    "use_chunks_db", [False, True], ids=["sharded_json", "chunks_db"]
)
def test_ac51_scaling_invariance_bounded_not_proportional(tmp_path, use_chunks_db):
    mut = _load_measurement_module()
    large_points = _LARGE_POINTS[use_chunks_db]

    after_small, before_small = _run_scaling_case(
        tmp_path,
        mut,
        use_chunks_db=use_chunks_db,
        num_points=_SMALL_POINTS,
        suffix="small",
    )
    after_large, before_large = _run_scaling_case(
        tmp_path,
        mut,
        use_chunks_db=use_chunks_db,
        num_points=large_points,
        suffix="large",
    )

    metric_name = "store_rows_scanned" if use_chunks_db else "files_opened"
    loader_call_name = (
        "_load_vectors_from_chunks_db"
        if use_chunks_db
        else "_load_vectors_from_json_files"
    )

    _assert_after_bounded(
        loader_call_name=loader_call_name,
        metric_name=metric_name,
        after_small=after_small,
        after_large=after_large,
    )
    _assert_before_proportional(
        loader_call_name=loader_call_name,
        metric_name=metric_name,
        small_points=_SMALL_POINTS,
        large_points=large_points,
        before_small=before_small,
        before_large=before_large,
        after_large_val=getattr(after_large, metric_name),
    )

    # AC20: wall_time/peak_rss are REPORTED measurements, never gated. The
    # after_small/after_large/before_small/before_large values printed
    # below carry the renamed instrumented_wall_time_seconds/
    # instrumented_peak_rss_delta_bytes fields (Bug #1575 Finding 4) --
    # profiler-inflated, explicitly labeled as such via the field name
    # itself. The HONEST, uninstrumented reading is obtained separately
    # immediately below, on a fresh twin fixture never touched by any
    # profiler, and is the ONLY wall-clock figure that should be quoted
    # from this report.
    layout_label = "CHUNKS_DB" if use_chunks_db else "SHARDED_JSON"
    honest_twin = mut.build_synthetic_fixture(
        tmp_path / f"fixture_honest_{use_chunks_db}",
        num_points=large_points,
        chunks_per_file=_CHUNKS_PER_FILE,
        use_chunks_db=use_chunks_db,
        hidden_at_bootstrap={_RESTORE_FILE, _ALREADY_HIDDEN_UNTOUCHED_FILE},
    )
    _, honest_wall_time_seconds, honest_peak_rss_delta_bytes = (
        mut.measure_single_file_change_refresh_wall_clock(
            honest_twin,
            target_file=_TARGET_FILE,
            hide_file=_HIDE_FILE,
            restore_file=_RESTORE_FILE,
        )
    )

    print(f"\n[AC19/AC20 report -- layout={layout_label}]")
    print(f"  small (n={_SMALL_POINTS}): after={after_small} before={before_small}")
    print(f"  large (n={large_points}): after={after_large} before={before_large}")
    print(
        f"  HONEST (uninstrumented) wall-clock at n={large_points}: "
        f"wall_time_seconds={honest_wall_time_seconds:.4f} "
        f"peak_rss_delta_bytes={honest_peak_rss_delta_bytes}"
    )
