"""
Unit tests for Bug #1336: LifecycleBatchRunner skips orphaned golden aliases
during lifecycle_backfill (and the shared description_refresh call site)
instead of failing the whole job.

An orphaned golden alias is a registry row present in the golden repos
backend, but whose on-disk clone directory at {golden_repos_dir}/{alias} is
absent (e.g. after a partial provisioning failure, or after the #1317
reconciler removed the clone while a stale lifecycle_backfill sweep still
referenced it).

Before this fix: _process_one_repo() computes repo_path = golden_repos_dir /
alias (no existence check) and passes it straight to claude_cli_invoker(). The
real LifecycleClaudeCliInvoker._validate_repo_inputs() raises:
    ValueError: repo_path does not exist for alias '<alias>': <repo_path>
This propagates out of _process_one_repo(), is recorded as a per-alias
failure by _run_sub_batch(), and when ALL aliases in a run() call fail (the
common startup-sweep case where every referenced alias is orphaned), run()
calls job_tracker.fail_job(), producing a FAILED "lifecycle_backfill" job --
exactly the disease observed on staging for the flask/uvicorn/starlette/httpx
aliases (Bug #1336, related to #1317).

Fix: _process_one_repo() catches the ValueError raised by claude_cli_invoker()
specifically, logs a WARNING identifying the alias as orphaned, and returns
without writing any cidx-meta/<alias>.md file and without re-raising -- the
sub-batch treats it as a completed (non-failed) unit of work, so run()'s
all-failed threshold is never tripped by orphans alone, and the job succeeds.

Orphan CLEANUP (removing the stale registry row) is explicitly out of scope
here -- it is delegated to the #1317 reconciler. This fix only makes the
runner tolerant of orphans; it must never delete anything itself.
"""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from code_indexer.global_repos.lifecycle_batch_runner import LifecycleBatchRunner
from code_indexer.global_repos.unified_response_parser import UnifiedResult

DEFAULT_TTL_SECONDS: int = 3600
DEFAULT_EST_SECS: int = 30
DEFAULT_CONCURRENCY: int = 2

_VALID_RESULT = UnifiedResult(
    description="Test repo description.",
    lifecycle={
        "ci_system": "github-actions",
        "deployment_target": "kubernetes",
        "language_ecosystem": "python/poetry",
        "build_system": "poetry",
        "testing_framework": "pytest",
        "confidence": "high",
    },
)


class _StubJobTracker:
    def __init__(self) -> None:
        self.complete_calls: List[Dict[str, Any]] = []
        self.fail_calls: List[Dict[str, Any]] = []

    def update_status(self, job_id: str, **kwargs: Any) -> None:
        pass

    def complete_job(self, job_id: str, result: Optional[Dict] = None) -> None:
        self.complete_calls.append({"job_id": job_id, "result": result})

    def fail_job(self, job_id: str, error: str) -> None:
        self.fail_calls.append({"job_id": job_id, "error": error})


class _StubDebouncer:
    def __init__(self) -> None:
        self.signal_count = 0

    def signal_dirty(self) -> None:
        self.signal_count += 1


class _StubScheduler:
    def acquire_write_lock(self, key: str, owner_name: str) -> bool:
        return True

    def release_write_lock(self, key: str, owner_name: str) -> None:
        pass


def _orphan_aware_invoker(
    alias: str, repo_path: Path, **_kwargs: object
) -> UnifiedResult:
    """Real-shaped stand-in for LifecycleClaudeCliInvoker: raises the exact
    ValueError the production invoker raises for a missing repo_path, without
    needing a live CliDispatcher/Claude CLI. Mirrors
    LifecycleClaudeCliInvoker._validate_repo_inputs()'s contract exactly."""
    path_obj = Path(repo_path)
    if not path_obj.exists():
        raise ValueError(f"repo_path does not exist for alias {alias!r}: {path_obj}")
    return _VALID_RESULT


def _make_runner(
    golden_repos_dir: Path,
    invoker: Optional[Callable[..., UnifiedResult]] = None,
) -> tuple:
    job_tracker = _StubJobTracker()
    debouncer = _StubDebouncer()
    scheduler = _StubScheduler()
    runner = LifecycleBatchRunner(
        golden_repos_dir=golden_repos_dir,
        job_tracker=job_tracker,
        refresh_scheduler=scheduler,
        debouncer=debouncer,
        claude_cli_invoker=invoker or _orphan_aware_invoker,
        concurrency=DEFAULT_CONCURRENCY,
        ttl_seconds=DEFAULT_TTL_SECONDS,
        estimated_seconds_per_repo=DEFAULT_EST_SECS,
    )
    return runner, job_tracker, debouncer


@pytest.fixture()
def golden_repos_dir(tmp_path: Path) -> Path:
    (tmp_path / "cidx-meta").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# Mixed batch: orphan + valid
# ---------------------------------------------------------------------------


class TestMixedBatchOrphanSkip:
    def test_orphan_skipped_with_warning_valid_processed_job_succeeds(
        self, golden_repos_dir: Path, caplog: Any
    ) -> None:
        (golden_repos_dir / "valid-repo").mkdir()
        # Deliberately do NOT create golden_repos_dir / "orphan-repo".

        runner, job_tracker, debouncer = _make_runner(golden_repos_dir)

        import logging

        with caplog.at_level(logging.WARNING):
            failed = runner.run(
                ["valid-repo", "orphan-repo"], parent_job_id="job-mixed"
            )

        assert "orphan-repo" not in failed, (
            f"Bug #1336: orphaned alias must be SKIPPED, not recorded as a "
            f"failure. Got failed={failed}"
        )
        assert job_tracker.complete_calls, (
            "Bug #1336: job must SUCCEED (complete_job called) when only "
            "orphans and valid aliases are present -- no genuine failures."
        )
        assert not job_tracker.fail_calls, (
            f"Bug #1336: job must not be marked FAILED. fail_calls={job_tracker.fail_calls}"
        )

        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("orphan-repo" in m for m in warning_messages), (
            f"Expected a WARNING mentioning the orphaned alias, got: {warning_messages}"
        )

        valid_md = golden_repos_dir / "cidx-meta" / "valid-repo.md"
        assert valid_md.exists(), (
            "The valid alias must still be processed normally (cidx-meta/"
            "valid-repo.md written)."
        )

    def test_orphan_leaves_no_cidx_meta_file(self, golden_repos_dir: Path) -> None:
        """Bug #1336 safety: skipping an orphan must never write a partial/
        placeholder cidx-meta/<alias>.md file."""
        runner, _job_tracker, _debouncer = _make_runner(golden_repos_dir)

        runner.run(["orphan-repo"], parent_job_id="job-orphan-only")

        orphan_md = golden_repos_dir / "cidx-meta" / "orphan-repo.md"
        assert not orphan_md.exists()
        assert not (golden_repos_dir / "orphan-repo").exists(), (
            "Bug #1336: orphan skip must never create the missing clone directory."
        )


# ---------------------------------------------------------------------------
# Fully-valid batch: unaffected regression check
# ---------------------------------------------------------------------------


class TestFullyValidBatchUnaffected:
    def test_fully_valid_batch_processes_all_aliases_normally(
        self, golden_repos_dir: Path, caplog: Any
    ) -> None:
        for alias in ("repo-a", "repo-b"):
            (golden_repos_dir / alias).mkdir()

        runner, job_tracker, debouncer = _make_runner(golden_repos_dir)

        import logging

        with caplog.at_level(logging.WARNING):
            failed = runner.run(["repo-a", "repo-b"], parent_job_id="job-valid")

        assert failed == {}
        assert job_tracker.complete_calls
        assert not job_tracker.fail_calls
        for alias in ("repo-a", "repo-b"):
            assert (golden_repos_dir / "cidx-meta" / f"{alias}.md").exists()

        orphan_warnings = [
            r.message
            for r in caplog.records
            if r.levelno == logging.WARNING and "orphan" in r.message.lower()
        ]
        assert orphan_warnings == [], (
            f"No orphan warnings expected for a fully-valid batch, got: {orphan_warnings}"
        )


# ---------------------------------------------------------------------------
# All-orphaned batch: job still succeeds (no genuine failures)
# ---------------------------------------------------------------------------


class TestAllOrphanedBatchSucceeds:
    def test_all_orphaned_batch_job_succeeds(self, golden_repos_dir: Path) -> None:
        runner, job_tracker, _debouncer = _make_runner(golden_repos_dir)

        failed = runner.run(["ghost-a", "ghost-b"], parent_job_id="job-all-orphan")

        assert failed == {}
        assert job_tracker.complete_calls
        assert not job_tracker.fail_calls


# ---------------------------------------------------------------------------
# Non-orphan ValueError must still be a genuine failure (narrow-catch guard)
# ---------------------------------------------------------------------------


class TestGenuineFailureStillFails:
    def test_non_orphan_value_error_is_still_recorded_as_failure(
        self, golden_repos_dir: Path
    ) -> None:
        """A ValueError unrelated to a missing clone directory (e.g. a Claude
        CLI/parsing failure) must NOT be swallowed by the Bug #1336 orphan
        guard -- it must still be recorded as a genuine per-alias failure."""
        (golden_repos_dir / "broken-repo").mkdir()

        def _bad_data_invoker(
            alias: str, repo_path: Path, **_kwargs: object
        ) -> UnifiedResult:
            raise ValueError("bad data: malformed response")

        runner, job_tracker, _debouncer = _make_runner(
            golden_repos_dir, invoker=_bad_data_invoker
        )

        failed = runner.run(["broken-repo"], parent_job_id="job-genuine-fail")

        assert "broken-repo" in failed, (
            "A non-orphan ValueError must still be recorded as a failure, "
            f"got failed={failed}"
        )
        assert "bad data" in failed["broken-repo"]
        assert job_tracker.fail_calls, (
            "Bug #1336's orphan guard must not mask a genuine failure -- "
            "job must still be marked FAILED when the only alias in the "
            "batch genuinely fails."
        )


# ---------------------------------------------------------------------------
# Idempotency / safety: no deletion side effects
# ---------------------------------------------------------------------------


class TestNoOrphanCleanupPerformed:
    def test_orphan_skip_never_deletes_anything(self, golden_repos_dir: Path) -> None:
        """Bug #1336 delegates orphan CLEANUP to the #1317 reconciler --
        LifecycleBatchRunner must only skip, never remove registry rows or
        filesystem state."""
        runner, _job_tracker, _debouncer = _make_runner(golden_repos_dir)

        before = set(golden_repos_dir.iterdir())
        runner.run(["orphan-repo"], parent_job_id="job-safety")
        after = set(golden_repos_dir.iterdir())

        assert before == after, (
            f"Bug #1336: orphan skip must not alter golden_repos_dir contents. "
            f"before={before}, after={after}"
        )
