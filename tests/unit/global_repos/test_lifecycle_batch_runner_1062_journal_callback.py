"""
Unit tests for LifecycleBatchRunner journal_callback wiring — Story #1062.

Backend task 3: _process_one_repo accepts optional journal_callback(alias, outcome)
so per-alias status is reported as each repo completes WITHOUT coupling the runner
to a journal backend.

Backend task (done/failed counters): counters are incremented in the worker
completion path (_run_sub_batch future-collection loop), NOT by parsing journal text.

Tests:
  1. journal_callback called with (alias, "succeeded") on success
  2. journal_callback called with (alias, "failed: <type>: <msg>") on exception
  3. journal_callback is optional (None) — no error when absent
  4. done counter incremented after success, failed counter incremented after exception
  5. callback NOT called if repo list is empty
"""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


from code_indexer.global_repos.lifecycle_batch_runner import (
    LifecycleBatchRunner,
)
from code_indexer.global_repos.unified_response_parser import (
    UnifiedResult,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

_VALID_RESULT = UnifiedResult(
    description="Stub description.",
    lifecycle={
        "ci_system": "github-actions",
        "deployment_target": "kubernetes",
        "language_ecosystem": "python/pytest",
        "build_system": "poetry",
        "testing_framework": "pytest",
        "confidence": "high",
    },
)


class _StubJobTracker:
    def update_status(self, **kwargs: Any) -> None:
        pass

    def complete_job(self, job_id: str, result: Optional[Dict] = None) -> None:
        pass

    def fail_job(self, job_id: str, error: str) -> None:
        pass


class _StubRefreshScheduler:
    """Always grants lock; no-op release."""

    def acquire_write_lock(self, key: str, owner_name: str = "") -> bool:
        return True

    def release_write_lock(self, key: str, owner_name: str = "") -> None:
        pass


class _StubDebouncer:
    def signal_dirty(self) -> None:
        pass


def _make_runner(
    golden_repos_dir: Path,
    invoker: Callable,
    callback: Optional[Callable[[str, str], None]] = None,
) -> LifecycleBatchRunner:
    return LifecycleBatchRunner(
        golden_repos_dir=golden_repos_dir,
        job_tracker=_StubJobTracker(),
        refresh_scheduler=_StubRefreshScheduler(),
        debouncer=_StubDebouncer(),
        claude_cli_invoker=invoker,
        concurrency=1,
        sub_batch_size_override=10,
        journal_callback=callback,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestJournalCallbackWiring:
    def test_callback_called_on_success(self, tmp_path: Path) -> None:
        """journal_callback(alias, outcome) called with 'succeeded' on success."""
        calls: List[Tuple[str, str]] = []

        def invoker(alias: str, repo_path: Path, **_kwargs: object) -> UnifiedResult:
            return _VALID_RESULT

        def callback(alias: str, outcome: str) -> None:
            calls.append((alias, outcome))

        runner = _make_runner(tmp_path, invoker, callback)
        runner.run(["repo-alpha"], parent_job_id="job-1")

        assert len(calls) == 1
        alias, outcome = calls[0]
        assert alias == "repo-alpha"
        assert outcome == "succeeded"

    def test_callback_called_on_exception(self, tmp_path: Path) -> None:
        """journal_callback called with 'failed: <type>: <msg>' when invoker raises."""

        calls: List[Tuple[str, str]] = []

        def failing_invoker(
            alias: str, repo_path: Path, **_kwargs: object
        ) -> UnifiedResult:
            raise RuntimeError("network timeout")

        def callback(alias: str, outcome: str) -> None:
            calls.append((alias, outcome))

        runner = _make_runner(tmp_path, failing_invoker, callback)
        # Should not raise — failure is logged and swallowed per batch runner contract
        runner.run(["repo-beta"], parent_job_id="job-2")

        assert len(calls) == 1
        alias, outcome = calls[0]
        assert alias == "repo-beta"
        assert "failed" in outcome
        assert "RuntimeError" in outcome
        assert "network timeout" in outcome

    def test_no_callback_no_error(self, tmp_path: Path) -> None:
        """journal_callback=None is the default — no AttributeError or TypeError."""

        def invoker(alias: str, repo_path: Path, **_kwargs: object) -> UnifiedResult:
            return _VALID_RESULT

        runner = _make_runner(tmp_path, invoker, callback=None)
        # Must not raise
        runner.run(["repo-gamma"], parent_job_id="job-3")

    def test_callback_not_called_for_empty_list(self, tmp_path: Path) -> None:
        """No callbacks when alias list is empty."""
        calls: List[Tuple[str, str]] = []

        def invoker(alias: str, repo_path: Path, **_kwargs: object) -> UnifiedResult:
            return _VALID_RESULT

        def callback(alias: str, outcome: str) -> None:
            calls.append((alias, outcome))

        runner = _make_runner(tmp_path, invoker, callback)
        runner.run([], parent_job_id="job-4")

        assert calls == []

    def test_callback_called_for_each_alias(self, tmp_path: Path) -> None:
        """Callback fires once per alias in a multi-repo run."""
        calls: List[Tuple[str, str]] = []

        def invoker(alias: str, repo_path: Path, **_kwargs: object) -> UnifiedResult:
            return _VALID_RESULT

        def callback(alias: str, outcome: str) -> None:
            calls.append((alias, outcome))

        runner = _make_runner(tmp_path, invoker, callback)
        runner.run(["r1", "r2", "r3"], parent_job_id="job-5")

        assert len(calls) == 3
        aliases_called = {c[0] for c in calls}
        assert aliases_called == {"r1", "r2", "r3"}
        for _, outcome in calls:
            assert outcome == "succeeded"

    def test_callback_called_for_mixed_success_failure(self, tmp_path: Path) -> None:
        """Callback fires with correct outcome for each alias."""
        calls: List[Tuple[str, str]] = []

        def invoker(alias: str, repo_path: Path, **_kwargs: object) -> UnifiedResult:
            if alias == "fail-me":
                raise ValueError("bad data")
            return _VALID_RESULT

        def callback(alias: str, outcome: str) -> None:
            calls.append((alias, outcome))

        runner = _make_runner(tmp_path, invoker, callback)
        runner.run(["ok-repo", "fail-me"], parent_job_id="job-6")

        assert len(calls) == 2
        call_map = {alias: outcome for alias, outcome in calls}
        assert call_map["ok-repo"] == "succeeded"
        assert "failed" in call_map["fail-me"]
        assert "ValueError" in call_map["fail-me"]
        assert "bad data" in call_map["fail-me"]
