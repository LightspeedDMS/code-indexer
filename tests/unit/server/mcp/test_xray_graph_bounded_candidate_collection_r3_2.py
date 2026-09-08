"""R3-2 (Codex re-review, ROUND 3): candidate-file collection must STOP
during the walk once a cap is reached, not collect-everything-then-slice.

`_collect_graph_candidate_files` previously walked the ENTIRE repository
tree, matching every file against include/exclude patterns and appending
every match to an unbounded list, before any `max_files` limit ever got a
chance to engage downstream (in Rust). On a repo with millions of matching
files this allocates the full path list before any admission control runs
-- OOM/timeout risk. This test proves the walk genuinely STOPS EARLY (by
counting how many candidate paths are actually EXAMINED against the glob
patterns, not merely asserting the final list is short -- a naive
collect-then-slice implementation would examine every file but still
return a short list).
"""

from __future__ import annotations

import fnmatch as fnmatch_module
from pathlib import Path
from unittest.mock import patch

from code_indexer.server.mcp.handlers.xray_graph import _collect_graph_candidate_files

_TOTAL_FILES = 10
_CAP = 3


def _build_flat_fixture(repo_root: Path, count: int) -> None:
    for i in range(count):
        (repo_root / f"file_{i:03d}.py").write_text(f"# file {i}\n")


def test_collection_stops_examining_files_once_cap_is_reached(
    tmp_path: Path,
) -> None:
    """With 10 real matching files and a cap of 3, the number of files
    actually EXAMINED against the include pattern must be bounded near
    the cap, not equal to the full file count -- proving the walk itself
    stops early rather than collecting everything and slicing the result
    afterward."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _build_flat_fixture(repo_root, _TOTAL_FILES)

    examined_count = {"value": 0}
    real_fnmatch = fnmatch_module.fnmatch

    def _counting_fnmatch(name: str, pattern: str) -> bool:
        examined_count["value"] += 1
        return real_fnmatch(name, pattern)

    with patch.object(fnmatch_module, "fnmatch", _counting_fnmatch):
        paths, truncated = _collect_graph_candidate_files(
            repo_root, ["*.py"], [], max_files=_CAP
        )

    # Guard: prove the instrumentation actually intercepted real calls --
    # otherwise a count of 0 would make the bounded-count assertion below
    # pass vacuously regardless of whether the implementation stops early.
    assert examined_count["value"] > 0, (
        "fnmatch patch never intercepted any call -- instrument the real "
        "matching seam _collect_graph_candidate_files uses"
    )
    assert len(paths) == _CAP, (
        f"expected exactly {_CAP} collected paths, got {len(paths)}"
    )
    assert truncated is True, (
        "collection must report that more files existed beyond the cap"
    )
    assert examined_count["value"] <= _CAP + 1, (
        f"the walk must STOP examining files once the cap is reached -- "
        f"examined {examined_count['value']} files against a cap of {_CAP} "
        f"out of {_TOTAL_FILES} total matching files, indicating the full "
        f"tree was walked before truncating"
    )


def test_collection_not_truncated_when_matches_are_within_cap(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _build_flat_fixture(repo_root, 2)

    paths, truncated = _collect_graph_candidate_files(
        repo_root, ["*.py"], [], max_files=_CAP
    )

    assert len(paths) == 2
    assert truncated is False


async def test_pipeline_ors_collection_truncation_into_final_result(
    tmp_path: Path,
) -> None:
    """Unit test of `_run_analyze_graph_pipeline`'s own orchestration
    logic: when `_resolve_repo_and_files` reports `collection_truncated
    =True` (Python's own candidate walk stopped early), the pipeline
    must OR that into the final result's `truncated_by_max_files`/
    `fact_graph_complete` fields -- even when the underlying (mocked)
    Rust backend itself reports a genuinely complete build. Mocks only
    the two legitimate collaboration boundaries: the internal
    `_resolve_repo_and_files` collaborator (controlled here to isolate
    THIS function's own merging logic) and the external Rust subprocess
    boundary (`RustNativeBackend.run_graph_analysis`)."""
    from unittest.mock import patch as _patch

    from code_indexer.server.mcp.handlers.xray_graph import (
        _run_analyze_graph_pipeline,
    )

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    backend_result = {
        "ok": True,
        "status": "ran_ok",
        "findings": [],
        "refine": [],
        "fact_graph_complete": True,
        "truncated_by_max_files": False,
        "build_status": "ok",
        "degradation": {"files_with_parse_errors": 0},
        "cached": False,
        "compile_ms": 1,
    }

    with (
        _patch(
            "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_and_files",
            return_value=(repo_root, ["A.java"], True, None),
        ),
        _patch(
            "code_indexer.xray.rust_backend.RustNativeBackend.run_graph_analysis",
            return_value=backend_result,
        ),
    ):
        result = await _run_analyze_graph_pipeline(
            evaluator_code=(
                "fn collect_facts(node: &OwnedNode, file: &str, index: &LocalIndex) "
                "-> Vec<UserFact> { Vec::new() }\n"
                "fn analyze_graph(g: &CodeGraph, facts: &FactIndex) -> GraphResult "
                "{ GraphResult::default() }\n"
            ),
            repo_alias="some-repo",
            include_patterns=[],
            exclude_patterns=[],
            timeout_seconds=30,
        )

    assert result["truncated_by_max_files"] is True, (
        f"collection_truncated=True must OR into the final result's "
        f"truncated_by_max_files field: {result}"
    )
    assert result["fact_graph_complete"] is False, (
        f"collection_truncated=True must downgrade fact_graph_complete: {result}"
    )
