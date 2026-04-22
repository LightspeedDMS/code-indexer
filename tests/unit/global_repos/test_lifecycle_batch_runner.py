"""
Unit tests for LifecycleBatchRunner — Story #876.

Tests:
  1. Sub-batch size formula: max(1, floor(0.5 * ttl * concurrency / est_secs))
  2. Lock acquired and released exactly once per sub-batch (not once per repo)
  3. complete_job called after all sub-batch release events (ordering verified)
  4. Debouncer signalled after all sub-batch release events (ordering verified)
  5. Lock failure on a later sub-batch raises LifecycleLockUnavailableError
     and does not call complete_job or signal the debouncer

Dependencies injected via constructor — no real Claude CLI, no real DB.
Stubs share a single EventLog for ordering assertions.
_make_runner is a plain helper (not a fixture) because it takes non-fixture
parameters (golden_repos_dir, log) to stay readable per test.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.global_repos.lifecycle_batch_runner import (
    LifecycleBatchRunner,
)
from code_indexer.global_repos.unified_response_parser import (
    CURRENT_LIFECYCLE_SCHEMA_VERSION,
    UnifiedResult,
)

# ---------------------------------------------------------------------------
# Named constants — self-documenting, no magic literals in tests
# ---------------------------------------------------------------------------

DEFAULT_TTL_SECONDS: int = 3600
DEFAULT_EST_SECS: int = 30
FORCED_SUB_BATCH_SIZE: int = 2
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

# ---------------------------------------------------------------------------
# Stubs with shared event log for ordering assertions
# ---------------------------------------------------------------------------


class _EventLog:
    """Shared ordered event log injected into all stubs."""

    def __init__(self) -> None:
        self.events: List[str] = []

    def record(self, event: str) -> None:
        self.events.append(event)


class _StubJobTracker:
    def __init__(self, log: _EventLog) -> None:
        self._log = log
        self.complete_calls: List[Dict[str, Any]] = []
        self.fail_calls: List[Dict[str, Any]] = []

    def update_status(self, job_id: str, **kwargs: Any) -> None:
        pass  # Not under test in these scenarios

    def complete_job(self, job_id: str, result: Optional[Dict] = None) -> None:
        self._log.record("complete_job")
        self.complete_calls.append({"job_id": job_id, "result": result})

    def fail_job(self, job_id: str, error: str) -> None:
        self._log.record("fail_job")
        self.fail_calls.append({"job_id": job_id, "error": error})


class _StubDebouncer:
    def __init__(self, log: _EventLog) -> None:
        self._log = log
        self.signal_count = 0

    def signal_dirty(self) -> None:
        self._log.record("signal_dirty")
        self.signal_count += 1


class _StubScheduler:
    """
    Configurable stub: acquire_sequence controls per-call results.
    If the sequence is exhausted, acquire_default is used.
    """

    def __init__(
        self,
        log: _EventLog,
        acquire_default: bool = True,
        acquire_sequence: Optional[List[bool]] = None,
    ) -> None:
        self._log = log
        self._acquire_default = acquire_default
        self._acquire_sequence = list(acquire_sequence) if acquire_sequence else []
        self.acquire_calls: List[tuple] = []
        self.release_calls: List[tuple] = []

    def acquire_write_lock(self, key: str, owner_name: str) -> bool:
        self.acquire_calls.append((key, owner_name))
        if self._acquire_sequence:
            result = self._acquire_sequence.pop(0)
        else:
            result = self._acquire_default
        if result:
            self._log.record(f"acquire:{key}")
        return result

    def release_write_lock(self, key: str, owner_name: str) -> None:
        self._log.record(f"release:{key}")
        self.release_calls.append((key, owner_name))


# ---------------------------------------------------------------------------
# Plain helper function — takes non-fixture parameters, not a pytest fixture
# ---------------------------------------------------------------------------


def _make_runner(
    golden_repos_dir: Path,
    log: _EventLog,
    acquire_default: bool = True,
    acquire_sequence: Optional[List[bool]] = None,
    sub_batch_size_override: Optional[int] = None,
) -> tuple:
    """
    Build a LifecycleBatchRunner with stub dependencies sharing the given log.
    Returns (runner, scheduler, job_tracker, debouncer).
    """
    scheduler = _StubScheduler(
        log=log, acquire_default=acquire_default, acquire_sequence=acquire_sequence
    )
    job_tracker = _StubJobTracker(log=log)
    debouncer = _StubDebouncer(log=log)

    def _noop_invoker(alias: str, repo_path: Path) -> UnifiedResult:
        return _VALID_RESULT

    runner = LifecycleBatchRunner(
        golden_repos_dir=golden_repos_dir,
        job_tracker=job_tracker,
        refresh_scheduler=scheduler,
        debouncer=debouncer,
        claude_cli_invoker=_noop_invoker,
        concurrency=DEFAULT_CONCURRENCY,
        ttl_seconds=DEFAULT_TTL_SECONDS,
        estimated_seconds_per_repo=DEFAULT_EST_SECS,
        sub_batch_size_override=sub_batch_size_override,
    )
    return runner, scheduler, job_tracker, debouncer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def golden_repos_dir(tmp_path: Path) -> Path:
    (tmp_path / "cidx-meta").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Sub-batch size formula
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ttl_seconds", "concurrency", "est_secs", "expected_size"),
    [
        (DEFAULT_TTL_SECONDS, 4, DEFAULT_EST_SECS, 240),  # floor(0.5*3600*4/30)=240
        (DEFAULT_TTL_SECONDS, 1, DEFAULT_EST_SECS, 60),  # floor(0.5*3600*1/30)=60
        (DEFAULT_TTL_SECONDS, 4, DEFAULT_TTL_SECONDS, 2),  # very slow: floor(0.5*4)=2
        (60, 1, DEFAULT_TTL_SECONDS, 1),  # tiny TTL → max(1,0)=1
    ],
    ids=["standard", "concurrency_1", "very_slow", "min_clamp"],
)
def test_sub_batch_size_formula(
    ttl_seconds: int, concurrency: int, est_secs: int, expected_size: int
) -> None:
    """Sub-batch size = max(1, floor(0.5 * ttl * concurrency / est_secs))."""
    result = LifecycleBatchRunner.compute_sub_batch_size(
        ttl_seconds=ttl_seconds,
        concurrency=concurrency,
        estimated_seconds_per_repo=est_secs,
    )
    assert result == expected_size


# ---------------------------------------------------------------------------
# 2. Lock acquired and released exactly once per sub-batch
# ---------------------------------------------------------------------------


def test_run_acquires_and_releases_lock_per_sub_batch(golden_repos_dir: Path) -> None:
    """
    After the Bug #876 per-alias lock fix: lock is acquired/released once
    per alias (N times total), not once per sub-batch.  Each lock key is
    "lifecycle:<alias>" rather than the global "cidx-meta".
    """
    aliases = ["a-global", "b-global", "c-global"]  # 3 repos → 3 per-alias acquires
    for alias in aliases:
        (golden_repos_dir / alias).mkdir(exist_ok=True)
    log = _EventLog()
    runner, scheduler, _, _ = _make_runner(
        golden_repos_dir, log, sub_batch_size_override=FORCED_SUB_BATCH_SIZE
    )
    runner.run(aliases, parent_job_id="job-1")

    # One acquire/release per alias
    assert len(scheduler.acquire_calls) == len(aliases)
    assert len(scheduler.release_calls) == len(aliases)
    for key, owner in scheduler.acquire_calls:
        assert key.startswith("lifecycle:"), (
            f"Expected per-alias key starting with 'lifecycle:', got {key!r}"
        )
        assert owner == "lifecycle_batch_runner"


# ---------------------------------------------------------------------------
# 3. complete_job called after all sub-batch release events
# ---------------------------------------------------------------------------


def test_run_calls_complete_job_after_all_sub_batches(golden_repos_dir: Path) -> None:
    """
    Ordering: every release event precedes complete_job in the shared event log.
    Proves complete_job fires only after all sub-batches have finished.
    """
    aliases = ["p-global", "q-global", "r-global"]
    log = _EventLog()
    runner, _, job_tracker, _ = _make_runner(
        golden_repos_dir, log, sub_batch_size_override=FORCED_SUB_BATCH_SIZE
    )
    runner.run(aliases, parent_job_id="job-2")

    assert len(job_tracker.complete_calls) == 1
    complete_idx = log.events.index("complete_job")
    release_indices = [i for i, e in enumerate(log.events) if e.startswith("release:")]
    assert release_indices, "Expected at least one release event"
    assert all(r < complete_idx for r in release_indices), (
        "complete_job must fire after all sub-batch releases"
    )


# ---------------------------------------------------------------------------
# 4. Debouncer signalled after all sub-batch release events
# ---------------------------------------------------------------------------


def test_run_signals_debouncer_after_all_sub_batches(golden_repos_dir: Path) -> None:
    """
    Ordering: signal_dirty appears after every release event in the event log.
    Proves the debouncer fires exactly once per run after all sub-batches join.
    """
    aliases = ["s-global", "t-global", "u-global", "v-global"]
    log = _EventLog()
    runner, _, _, debouncer = _make_runner(
        golden_repos_dir, log, sub_batch_size_override=FORCED_SUB_BATCH_SIZE
    )
    runner.run(aliases, parent_job_id="job-3")

    assert debouncer.signal_count == 1
    signal_idx = log.events.index("signal_dirty")
    release_indices = [i for i, e in enumerate(log.events) if e.startswith("release:")]
    assert release_indices, "Expected at least one release event"
    assert all(r < signal_idx for r in release_indices), (
        "signal_dirty must fire after all sub-batch releases"
    )


# ---------------------------------------------------------------------------
# 5. Lock failure on later sub-batch raises LifecycleLockUnavailableError
# ---------------------------------------------------------------------------


def test_run_completes_despite_per_alias_lock_failure(golden_repos_dir: Path) -> None:
    """
    After the Bug #876 per-alias lock fix: a lock failure for ONE alias inside
    _process_one_repo is treated as a per-repo exception — logged at ERROR
    level and swallowed — so the batch completes and complete_job IS called.

    acquire_sequence: x-global→True, y-global→False (lock fails), z-global→True.
    Sub-batch size 2: first batch [x-global, y-global], second batch [z-global].

    Verifications:
    - complete_job IS called (batch did not abort)
    - debouncer.signal_dirty IS called
    - y-global (lock failed) has NO .md file in cidx-meta (fail-closed)
    - x-global and z-global (locks succeeded) DO have .md files in cidx-meta
    """
    aliases = ["x-global", "y-global", "z-global"]
    for alias in aliases:
        (golden_repos_dir / alias).mkdir(exist_ok=True)
    log = _EventLog()
    runner, scheduler, job_tracker, debouncer = _make_runner(
        golden_repos_dir,
        log,
        acquire_sequence=[True, False, True],
        sub_batch_size_override=FORCED_SUB_BATCH_SIZE,
    )

    # Must NOT raise — per-repo lock failures are swallowed by _run_sub_batch
    runner.run(aliases, parent_job_id="job-4")

    assert len(job_tracker.complete_calls) == 1, (
        "complete_job must fire despite per-alias lock failure"
    )
    assert debouncer.signal_count == 1, (
        "Debouncer must fire despite per-alias lock failure"
    )

    cidx_meta = golden_repos_dir / "cidx-meta"
    assert not (cidx_meta / "y-global.md").exists(), (
        "y-global.md must NOT exist: lock failed for this alias"
    )
    assert (cidx_meta / "x-global.md").exists(), (
        "x-global.md must exist: lock succeeded for this alias"
    )
    assert (cidx_meta / "z-global.md").exists(), (
        "z-global.md must exist: lock succeeded for this alias"
    )


# ---------------------------------------------------------------------------
# 6. LifecycleBatchRunner emits current schema version in written .md
# ---------------------------------------------------------------------------


def test_process_one_repo_emits_current_schema_version(golden_repos_dir: Path) -> None:
    """
    The YAML frontmatter written by _process_one_repo must contain
    lifecycle_schema_version equal to CURRENT_LIFECYCLE_SCHEMA_VERSION imported
    from unified_response_parser, which is the single canonical source of truth.

    This test exists to catch the duplicate-constant bug: if lifecycle_batch_runner.py
    defines its own stale CURRENT_LIFECYCLE_SCHEMA_VERSION instead of importing the
    canonical value from unified_response_parser, this test will fail.
    """
    import yaml

    alias = "writer-test-global"
    (golden_repos_dir / alias).mkdir(exist_ok=True)

    log = _EventLog()
    runner, _, _, _ = _make_runner(golden_repos_dir, log)
    runner.run([alias], parent_job_id="job-v3")

    md_path = golden_repos_dir / "cidx-meta" / f"{alias}.md"
    assert md_path.exists(), f"{md_path} must have been written by the runner"

    content = md_path.read_text(encoding="utf-8")
    parts = content.split("---\n", maxsplit=2)
    assert len(parts) >= 3, "Written .md must have YAML frontmatter delimiters"
    fm = yaml.safe_load(parts[1])

    assert fm.get("lifecycle_schema_version") == CURRENT_LIFECYCLE_SCHEMA_VERSION, (
        f"Expected lifecycle_schema_version={CURRENT_LIFECYCLE_SCHEMA_VERSION}, "
        f"got {fm.get('lifecycle_schema_version')!r}. "
        "lifecycle_batch_runner.py must import CURRENT_LIFECYCLE_SCHEMA_VERSION from "
        "unified_response_parser, not define its own stale constant."
    )
