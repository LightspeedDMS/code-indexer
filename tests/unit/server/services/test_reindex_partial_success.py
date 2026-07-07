"""C8: a single index-type failure must not fail the whole reindex.

Per-type execution is already isolated (each type is attempted). _execute_indexing_job
must therefore report a mixed run as a PARTIAL success (success=True, partial=True,
failed_types surfaced) instead of a hard failure, and only mark success=False when
EVERY type failed.
"""

from unittest.mock import MagicMock, patch

from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)


def _run(results):
    mgr = ActivatedRepoIndexManager.__new__(ActivatedRepoIndexManager)
    mgr.logger = MagicMock()
    with (
        patch.object(mgr, "_execute_all_index_types", return_value=results),
        patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(0, 0),
        ),
        patch(
            "code_indexer.services.progress_phase_allocator.ProgressPhaseAllocator"
        ) as PA,
    ):
        PA.return_value.phase_start.return_value = 10
        PA.return_value.phase_end.return_value = 90
        return mgr._execute_indexing_job(
            repo_alias="r",
            repo_path="/p",
            index_types=list(results.keys()),
            clear=False,
            progress_callback=None,
        )


def test_all_success():
    out = _run({"semantic": {"success": True}, "fts": {"success": True}})
    assert out["success"] is True
    assert out["partial"] is False
    assert out["failed_types"] is None


def test_partial_success_does_not_fail_whole_reindex():
    out = _run(
        {
            "semantic": {"success": True},
            "fts": {"success": True},
            "temporal": {"success": False, "error": "timed out"},
        }
    )
    assert out["success"] is True  # C8: one type's failure must not fail the job
    assert out["partial"] is True
    assert out["failed_types"] == ["temporal"]


def test_all_failed_is_hard_failure():
    out = _run(
        {
            "semantic": {"success": False, "error": "x"},
            "fts": {"success": False, "error": "y"},
        }
    )
    assert out["success"] is False
    assert out["partial"] is False
    assert set(out["failed_types"]) == {"semantic", "fts"}
