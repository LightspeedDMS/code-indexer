"""
Front-door integration test for #1094.

Drives DescriptionRefreshScheduler._run_loop_single_pass end-to-end with:
  - a stale repo whose tracking row has last_known_commit = None (DB reset /
    lifecycle backfill), and
  - an existing non-empty cidx-meta/<alias>.md,

and proves that:
  1. The NULL last_known_commit fires a refresh (revert of #1093 Fix A), AND
  2. the existing description body is forwarded all the way to the Claude
     invoker as the `existing_description` kwarg, so the LLM REFINES it instead
     of regenerating from scratch.

This is the regression guard for the whole story: it exercises the real call
chain _run_loop_single_pass -> has_changes_since_last_run -> _has_existing_
description -> refresh_task -> _run_lifecycle_via_batch_runner ->
LifecycleBatchRunner.run -> _process_one_repo -> invoker.

Real files + real split_frontmatter_and_body (Messi Rule #1).  Only the Claude
invoker boundary and the executor/debouncer/lock collaborators are stubs.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Dict, List, Tuple

from code_indexer.global_repos.unified_response_parser import UnifiedResult


_LIFECYCLE: Dict[str, Any] = {
    "ci_system": "github-actions",
    "deployment_target": "kubernetes",
    "language_ecosystem": "python/poetry",
    "build_system": "poetry",
    "testing_framework": "pytest",
    "confidence": "high",
}


class _RecordingInvoker:
    def __init__(self) -> None:
        self.calls: List[Tuple[tuple, dict]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> UnifiedResult:
        self.calls.append((args, dict(kwargs)))
        return UnifiedResult(
            description="Refined body produced by the refresh-aware invoker.",
            lifecycle=dict(_LIFECYCLE),
        )


class _SyncExecutor:
    """Runs submitted callables immediately and returns a completed Future."""

    def submit(self, fn, *args, **kwargs) -> Future:
        fut: Future = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except Exception as exc:  # pragma: no cover - surfaced via Future
            fut.set_exception(exc)
        return fut


class _StubScheduler:
    def acquire_write_lock(self, key: str, owner_name: str) -> bool:
        return True

    def release_write_lock(self, key: str, owner_name: str) -> None:
        pass


class _StubDebouncer:
    def signal_dirty(self) -> None:
        pass


class _StubJobTracker:
    def register_job(self, *a: Any, **k: Any) -> None:
        pass

    def update_status(self, *a: Any, **k: Any) -> None:
        pass

    def complete_job(self, *a: Any, **k: Any) -> None:
        pass

    def fail_job(self, *a: Any, **k: Any) -> None:
        pass


class _StubTrackingBackend:
    def __init__(self) -> None:
        self.upserts: List[dict] = []

    def upsert_tracking(self, **kwargs: Any) -> None:
        self.upserts.append(kwargs)


def _build_scheduler(
    tmp_path: Path, invoker: _RecordingInvoker, alias: str, clone_path: str
) -> Any:
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )

    sched = object.__new__(DescriptionRefreshScheduler)

    golden_repos_dir = tmp_path
    meta_dir = tmp_path / "cidx-meta"

    # Collaborators
    sched._golden_repos_dir = golden_repos_dir
    sched._meta_dir = meta_dir
    sched._lifecycle_invoker = invoker
    sched._lifecycle_debouncer = _StubDebouncer()
    sched._refresh_scheduler = _StubScheduler()
    sched._job_tracker = _StubJobTracker()
    sched._tracking_backend = _StubTrackingBackend()
    sched._executor = _SyncExecutor()
    sched._claude_cli_manager = object()  # truthy: enables the refresh branch

    # Events / counters used by _run_loop_single_pass
    sched._lifecycle_backfill_running = threading.Event()
    sched._description_backfill_running = threading.Event()
    sched._shutdown_event = threading.Event()
    sched._prompt_failure_counts = defaultdict(int)
    sched._warned_missing_desc = set()

    # Config manager: only used for concurrency + next_run lookups.
    class _Cfg:
        class claude_integration_config:  # noqa: N801
            max_concurrent_claude_cli = 1
            description_refresh_interval_hours = 24

    class _ConfigManager:
        def load_config(self) -> Any:
            return _Cfg()

    sched._config_manager = _ConfigManager()

    # Stale-repo source: one repo with NULL last_known_commit.
    stale = [
        {
            "repo_alias": alias,
            "clone_path": clone_path,
            "last_known_commit": None,
        }
    ]
    sched.get_stale_repos = lambda: stale  # type: ignore[assignment]
    sched.calculate_next_run = lambda a: "2099-01-01T00:00:00+00:00"  # type: ignore[assignment]

    return sched


def test_null_marker_refresh_forwards_existing_body_end_to_end(
    tmp_path: Path,
) -> None:
    alias = "evolution"

    # Real golden repo clone with provider metadata (current_commit set).
    clone = tmp_path / alias
    (clone / ".code-indexer").mkdir(parents=True)
    (clone / ".code-indexer" / "metadata-voyage-code-3.json").write_text(
        json.dumps({"current_commit": "deadbeef", "files_processed": 7})
    )
    # The repo dir itself is what LifecycleBatchRunner passes as repo_path.

    # Existing non-empty cidx-meta/<alias>.md with a specific, verifiable claim.
    meta_dir = tmp_path / "cidx-meta"
    meta_dir.mkdir()
    (meta_dir / f"{alias}.md").write_text(
        "---\n"
        "last_analyzed: 2020-06-01T00:00:00+00:00\n"
        "lifecycle:\n  ci_system: github-actions\n"
        "lifecycle_schema_version: 4\n"
        "---\n\n"
        "# Evolution Engine\n\n"
        "Implements the QuasarReplication protocol via the orbital-sync module.\n"
    )

    invoker = _RecordingInvoker()
    sched = _build_scheduler(tmp_path, invoker, alias, str(clone))

    sched._run_loop_single_pass()

    # The refresh must have fired (NULL marker) AND reached the invoker.
    assert len(invoker.calls) == 1, (
        f"expected exactly one invoker call, got {len(invoker.calls)}"
    )
    _args, kwargs = invoker.calls[0]
    assert kwargs.get("existing_description"), (
        "the existing .md body must be forwarded to the invoker for refinement"
    )
    assert "QuasarReplication" in kwargs["existing_description"]
    assert "orbital-sync" in kwargs["existing_description"]
    # And the stale last_analyzed marker is forwarded for change-scoping.  YAML
    # parses the frontmatter timestamp into a datetime, so str() uses a space
    # separator — compare the parsed instant rather than the literal format.
    from datetime import datetime, timezone

    forwarded = kwargs.get("last_analyzed")
    assert forwarded is not None
    assert datetime.fromisoformat(str(forwarded)) == datetime(
        2020, 6, 1, 0, 0, 0, tzinfo=timezone.utc
    )


def test_null_marker_with_no_md_does_not_forward_body(tmp_path: Path) -> None:
    """NULL marker still fires, but with no .md the gate skips before invoking."""
    alias = "fresh-repo"
    clone = tmp_path / alias
    (clone / ".code-indexer").mkdir(parents=True)
    (clone / ".code-indexer" / "metadata-voyage-code-3.json").write_text(
        json.dumps({"current_commit": "cafebabe"})
    )
    (tmp_path / "cidx-meta").mkdir()  # empty meta dir, no .md for this alias

    invoker = _RecordingInvoker()
    sched = _build_scheduler(tmp_path, invoker, alias, str(clone))

    sched._run_loop_single_pass()

    # _has_existing_description gate => no .md => skip without invoking.
    assert len(invoker.calls) == 0
