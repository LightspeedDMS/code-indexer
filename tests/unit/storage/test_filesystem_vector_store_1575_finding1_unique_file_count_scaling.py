"""Bug #1575 Finding 1 (dual review: Claude opus + Codex, both independently
reproduced): ``FilesystemVectorStore._calculate_and_save_unique_file_count``
did a full O(N) rescan of the ENTIRE collection on every ``end_indexing()``
call -- for SHARDED_JSON it opened every remaining ``vector_*.json`` file
(one open() per point currently in the collection); for CHUNKS_DB it ran an
unconditional ``SELECT DISTINCT path FROM chunks`` full-index scan -- even
though a single-file-change refresh only ever touches a handful of points.

This defeated the entire point of Bug #1575 Part A/B/C: the HNSW rebuild is
correctly bounded, but the OVERALL refresh still did O(N) work via this
uninstrumented unique-file-count bookkeeping path, invisible to the existing
AC51 test because that test only wraps FOUR specific boundary functions
(neither of which is ``_calculate_and_save_unique_file_count``).

Finding 1's original fix introduced a live-session PathIndex-cache fast-path
shortcut for BOTH layouts to bound this cost. That shortcut went through 6
consecutive dual-review rounds, each finding a NEW distinct correctness bug
in it, and was ultimately ABANDONED ENTIRELY by project-owner decision --
first for CHUNKS_DB (round 5's Fix 1, see
``test_filesystem_vector_store_1575_chunks_db_revert.py``), and later for
SHARDED_JSON too (see
``test_filesystem_vector_store_1575_sharded_json_shortcut_abandoned.py``).
Both layouts now ALWAYS compute the authoritative, from-storage answer on
every call -- correctness over speed. The SHARDED_JSON boundedness
assertion this file used to make is therefore GONE (that property no
longer holds by design, and re-observing it would mean the shortcut
regressed back in); the CHUNKS_DB boundedness assertions below remain
unaffected, since that layout was never claiming a bounded PER-QUERY cost
in the first place -- only a bounded CALL COUNT (exactly once per refresh,
never scaling with additional shortcut-consultation calls), which holds
whether or not any shortcut exists. The storage-agnostic correctness
assertion below also remains from this file's original scope.

Real ``FilesystemVectorStore`` + real filesystem/SQLite throughout -- no
mocking of the code under test. Uses an identity-matched ``sys.setprofile``
call counter (CHUNKS_DB), the same observation technique the dual review
used to reproduce this bug and that this codebase's own AC-M1 measurement
harness already established as the non-invasive way to observe production
code from outside (Messi Rule #17: never patch/subclass the observed
functions).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

# NOTE on `Any`: the measurement harness lives under scripts/analysis/ (no
# importable dotted module path -- AC-M1's explicit placement requirement)
# and is loaded via importlib.util.spec_from_file_location, exactly like
# test_filesystem_vector_store_1575_measurement_scaling.py already does for
# the identical reason (documented in that file's own module docstring):
# mypy cannot resolve a dotted import for a path that only exists via a
# test-time sys.path mutation, so the loaded module and everything derived
# from it (fixtures, results) is treated as untyped at this one dynamic
# boundary. This is not a general type-safety escape -- every other value
# in this file keeps its concrete type.

_PROJECT_ROOT = Path(__file__).parents[3]
_SCRIPT_PATH = (
    _PROJECT_ROOT / "scripts" / "analysis" / "measure_incremental_refresh_cost_1575.py"
)

_CHUNKS_PER_FILE = 5
_SMALL_POINTS = 100  # 20 files
_LARGE_POINTS = 2_000  # 400 files
_BOUNDED_CONSTANT = 10

_TARGET_FILE = "src/module_0.py"
_RESTORE_FILE = "src/module_1.py"
_HIDE_FILE = "src/module_2.py"


def _load_measurement_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "measure_incremental_refresh_cost_1575_finding1_unique_file_count",
        _SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _build_fixture(
    mut: Any, *, use_chunks_db: bool, num_points: int, tmp_path: Path, suffix: str
) -> Any:
    """Build the fixture (bootstrap indexing -- NOT part of any measured
    refresh cost). Callers must activate measurement AFTER this returns,
    then measure ONLY the subsequent refresh call -- never fixture
    construction, which legitimately opens/writes O(N) files on its own
    and would otherwise make both "small" and "large" measurements
    proportional to N regardless of whether the refresh itself is fixed.
    """
    base_path = tmp_path / f"fixture_{suffix}"
    return mut.build_synthetic_fixture(
        base_path,
        num_points=num_points,
        chunks_per_file=_CHUNKS_PER_FILE,
        use_chunks_db=use_chunks_db,
        hidden_at_bootstrap={_RESTORE_FILE},
    )


def _refresh(mut: Any, fixture: Any) -> Any:
    return mut.apply_single_file_change_refresh(
        fixture,
        target_file=_TARGET_FILE,
        hide_file=_HIDE_FILE,
        restore_file=_RESTORE_FILE,
    )


# ---------------------------------------------------------------------------
# SHARDED_JSON: no boundedness property is measured here any longer. The
# project owner's final decision abandoned the fast-path shortcut for this
# layout entirely (matching CHUNKS_DB below) -- every call now performs the
# authoritative, from-disk rescan by design, so a "bounded, not
# proportional" assertion would now assert something FALSE. See
# test_filesystem_vector_store_1575_sharded_json_shortcut_abandoned.py for
# this layout's replacement correctness coverage.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CHUNKS_DB: project-owner scoping decision (4th dual-review round) REVERTED
# the fast-path shortcut for this layout entirely -- ChunkStore.distinct_paths()
# (one SELECT DISTINCT path query, measured at ~4.5ms even on a 24,000-row
# collection) is now ALWAYS invoked exactly once per
# _calculate_and_save_unique_file_count() call, unconditionally, regardless
# of collection size. This is still BOUNDED (a fixed, small number of calls
# per refresh -- not proportional to N), just no longer ZERO: the shortcut
# never bought anything meaningful for this layout while leaving a real
# staleness risk (a killed/crashed session's present-but-stale
# path_index.bin would otherwise be trusted forever for CHUNKS_DB, since
# unlike SHARDED_JSON this layout has no authoritative self-healing
# fallback for it). See
# test_filesystem_vector_store_1575_chunks_db_revert.py for the dedicated
# regression test proving this.
# ---------------------------------------------------------------------------


# NOTE: a second, independent sys.setprofile-based counter here would be
# silently SHADOWED by apply_single_file_change_refresh()'s own internal
# instrument_boundary_calls() for the entire measured call (only one
# profiler can be active at a time -- this is the exact single-active-
# profiler lesson Bug #1575 Finding 3 documents). So this uses the
# harness's OWN call-count metric instead: ChunkStore.distinct_paths was
# added as a 5th tracked boundary in _TARGET_CODE_OBJECTS specifically so
# this cost is observable through the harness's existing, non-conflicting
# mechanism.

_EXPECTED_DISTINCT_PATHS_CALLS_PER_REFRESH = 1


def test_chunks_db_distinct_paths_invoked_exactly_once_bounded_not_proportional_small(
    tmp_path,
):
    mut = _load_measurement_module()
    fixture = _build_fixture(
        mut,
        use_chunks_db=True,
        num_points=_SMALL_POINTS,
        tmp_path=tmp_path,
        suffix="cdb_small",
    )
    _, metrics = _refresh(mut, fixture)
    assert (
        metrics.call_counts.get("distinct_paths", 0)
        == _EXPECTED_DISTINCT_PATHS_CALLS_PER_REFRESH
    ), (
        "ChunkStore.distinct_paths() must be invoked EXACTLY ONCE per "
        "single-file-change incremental refresh for CHUNKS_DB (the "
        "reverted, always-authoritative direct query) -- never zero "
        "(that would mean the reverted shortcut regressed back in) and "
        "never more than once (that would mean it scales with the number "
        "of files touched, not the collection as a whole)"
    )


def test_chunks_db_distinct_paths_invoked_exactly_once_bounded_not_proportional_large(
    tmp_path,
):
    mut = _load_measurement_module()
    fixture = _build_fixture(
        mut,
        use_chunks_db=True,
        num_points=_LARGE_POINTS,
        tmp_path=tmp_path,
        suffix="cdb_large",
    )
    _, metrics = _refresh(mut, fixture)
    assert (
        metrics.call_counts.get("distinct_paths", 0)
        == _EXPECTED_DISTINCT_PATHS_CALLS_PER_REFRESH
    ), (
        "ChunkStore.distinct_paths() must be invoked EXACTLY ONCE per "
        "single-file-change incremental refresh for CHUNKS_DB, regardless "
        "of collection size (bounded, not proportional to N)"
    )


# ---------------------------------------------------------------------------
# Correctness: the fix must not change the actual computed unique_file_count
# ---------------------------------------------------------------------------


def test_unique_file_count_value_unaffected_by_the_fix(tmp_path):
    """The live-session PathIndex shortcut must report the SAME
    unique_file_count as the full-rescan path would have -- proven by
    reading collection_meta.json after a real refresh.

    target_file's old chunks are replaced (Story #540 orphan cleanup) and
    hide/restore only flip visibility -- neither operation adds or removes
    a distinct file *path* -- so the expected count is unchanged from the
    fixture's own total file count.
    """
    mut = _load_measurement_module()
    fixture = _build_fixture(
        mut,
        use_chunks_db=False,
        num_points=_SMALL_POINTS,
        tmp_path=tmp_path,
        suffix="correctness",
    )
    _refresh(mut, fixture)

    import json

    collection_path = fixture.base_path / fixture.collection_name
    meta = json.loads((collection_path / "collection_meta.json").read_text())

    expected_unique_files = len(fixture.file_paths)
    assert meta["unique_file_count"] == expected_unique_files
