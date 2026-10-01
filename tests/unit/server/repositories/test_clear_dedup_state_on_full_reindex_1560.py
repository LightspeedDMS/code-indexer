"""
Codex review Finding F6: `dedup_state.clear_dedup_state()` has ZERO
production callers. The maintainer's explicit words: "you need to
ensure that a re-index clears the error condition also from the health
check". As written, a successful full re-index could never clear the
persisted dedup-outcome row, so /health stays DEGRADED forever.

`GoldenRepoManager.add_indexes_to_golden_repo()`'s "semantic" branch
ALWAYS passes `--clear` (Bug #468: "forces full rebuild for already-
indexed repos") -- so ANY call with "semantic" in `index_types` IS, by
construction, a full re-index. This is therefore the correct, and only
correct, production call site for AC8's "tied by the caller to a
successful full re-index's completion marker/generation".

This test drives the REAL production entry point
(`add_index_to_golden_repo`) with a REAL `GoldenRepoManager` (real
`_sqlite_backend`, the SAME backend `dedup_state.py`'s wrapper functions
read/write through `getattr(golden_repo_manager, "_sqlite_backend")`)
and a REAL synchronous `background_job_manager` test double that
actually invokes the submitted closure (mirrors this story's own
`_RealGateBackgroundJobManager` convention) -- proving the wiring
ACTUALLY RUNS via the production call path, not merely that
`clear_dedup_state()` works when called directly.

The only test-doubled boundary is the actual `cidx init`/`cidx index
--clear` SUBPROCESS invocation (an external-process boundary, the one
category this project's mocking hierarchy tolerates a stand-in for) --
everything else, including the real dedup-state backend write/read and
the real CoW-snapshot-skip branch (no `_refresh_scheduler` configured),
executes for real.
"""

from pathlib import Path
from typing import Any, List

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.services.fleet_migration.dedup_state import (
    get_dedup_state,
    record_dedup_outcome,
)


class _SyncBackgroundJobManager:
    """Executes the submitted closure SYNCHRONOUSLY and for real -- no
    real thread pool, but the closure itself (background_worker) runs
    completely unmocked."""

    def submit_job(
        self,
        operation_type: str,
        func,
        *args: Any,
        submitter_username: str,
        is_admin: bool = False,
        repo_alias=None,
        **kwargs: Any,
    ) -> str:
        func()
        return "job-sync"


def _make_manager_with_real_repo(tmp_path: Path) -> GoldenRepoManager:
    manager = GoldenRepoManager(data_dir=str(tmp_path))
    manager.background_job_manager = _SyncBackgroundJobManager()

    repo_path = tmp_path / "golden-repos" / "click"
    repo_path.mkdir(parents=True)
    (repo_path / ".code-indexer").mkdir()

    manager._sqlite_backend.add_repo(
        alias="click",
        repo_url="https://github.com/example/click.git",
        default_branch="main",
        clone_path=str(repo_path),
        created_at="2024-01-01T00:00:00Z",
    )
    return manager


def _fake_run_with_popen_progress(
    command: List[str],
    phase_name: str,
    allocator,
    progress_callback,
    all_stdout: List[str],
    all_stderr: List[str],
    cwd,
    error_label=None,
    last_reported=None,
    env=None,
    orphan_event_callback=None,
) -> int:
    """Stands in for the real `cidx index --clear` subprocess -- the
    ONE external-process boundary this test doubles; everything else in
    add_indexes_to_golden_repo()'s background_worker runs for real."""
    return 0


@pytest.fixture(autouse=True)
def _fake_subprocess_boundaries(monkeypatch):
    import subprocess

    from code_indexer.services import progress_subprocess_runner

    monkeypatch.setattr(
        progress_subprocess_runner,
        "run_with_popen_progress",
        _fake_run_with_popen_progress,
    )

    real_run = subprocess.run

    def _fake_subprocess_run(command, *args, **kwargs):
        is_fakeable_cidx_call = (
            len(command) > 1
            and command[0] == "cidx"
            and command[1] in ("init", "index")
        )
        if is_fakeable_cidx_call:

            class _FakeCompletedProcess:
                returncode = 0
                stdout = "already exists"
                stderr = ""

            return _FakeCompletedProcess()
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(
        "code_indexer.server.repositories.golden_repo_manager.subprocess.run",
        _fake_subprocess_run,
    )


class TestFullReindexClearsPersistedDedupState:
    def test_semantic_add_index_clears_an_active_dedup_row(
        self, tmp_path: Path
    ) -> None:
        manager = _make_manager_with_real_repo(tmp_path)
        record_dedup_outcome(
            manager,
            "click",
            duplicate_groups=1,
            records_before=10,
            records_deleted=1,
            winner_kept_groups=1,
            whole_group_deleted_groups=0,
            collection_total=10,
        )
        active_state = get_dedup_state(manager, "click")
        assert active_state is not None
        assert active_state["cleared_at"] is None

        job_id = manager.add_index_to_golden_repo(
            alias="click", index_type="semantic", submitter_username="admin"
        )

        assert job_id == "job-sync"
        cleared_state = get_dedup_state(manager, "click")
        assert cleared_state is not None
        assert cleared_state["cleared_at"] is not None

    def test_fts_only_add_index_does_not_clear_dedup_state(
        self, tmp_path: Path
    ) -> None:
        """Regression: an index_type that never touches the affected
        semantic data (fts-only) must NOT clear a dedup-outcome row --
        only a genuine full SEMANTIC re-index (--clear) does."""
        manager = _make_manager_with_real_repo(tmp_path)
        record_dedup_outcome(
            manager,
            "click",
            duplicate_groups=1,
            records_before=10,
            records_deleted=1,
            winner_kept_groups=1,
            whole_group_deleted_groups=0,
            collection_total=10,
        )

        manager.add_index_to_golden_repo(
            alias="click", index_type="fts", submitter_username="admin"
        )

        state = get_dedup_state(manager, "click")
        assert state is not None
        assert state["cleared_at"] is None
