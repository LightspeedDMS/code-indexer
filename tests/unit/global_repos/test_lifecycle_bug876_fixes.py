"""
Regression tests for Bug #876 fixes.

Three distinct bugs addressed:

  Bug A — Global lock contention:
    When N repos are added concurrently, all N lifecycle_registration jobs
    call runner.run([alias]) which acquires "cidx-meta" as a GLOBAL lock.
    All N threads compete for the same lock; N-1 get False immediately
    (non-blocking acquire) and raise LifecycleLockUnavailableError.

    Fix: use a per-alias lock key (e.g. "lifecycle:<alias>") so each
    single-repo job has its own lock namespace and jobs never contend.

  Bug B — JSON preamble not stripped:
    Claude CLI may output text before the JSON object despite the prompt
    saying "no preamble".  UnifiedResponseParser._clean_claude_output
    strips ANSI escapes but NOT prose before '{'.  json.loads then
    receives a non-empty string that doesn't start with '{' and raises
    "Expecting value: line 1 column 1 (char 0)".

    Fix: after cleaning, strip everything before the first '{'.

  Bug C — lifecycle_schema_version never written to DB:
    upsert_tracking's valid_fields dict does not include
    lifecycle_schema_version, so the column stays at 0 even after a
    successful lifecycle run.  The E2E operator check
      SELECT lifecycle_schema_version FROM description_refresh_tracking
    always returns 0, not >= 2.

    Fix: add lifecycle_schema_version to valid_fields AND call
    upsert_tracking(..., lifecycle_schema_version=<version>) from
    LifecycleBatchRunner._process_one_repo after a successful write.

MOCK BOUNDARY MAP:
  REAL: LifecycleBatchRunner, UnifiedResponseParser, WriteLockManager,
        DescriptionRefreshTrackingBackend (via real SQLite).
  STUBBED: claude_cli_invoker (never spawns real subprocess),
           job_tracker (in-memory stub), debouncer (in-memory stub).

Tests covered (seven total):
  (a) concurrent different-alias lifecycle jobs must not contend on the lock
  (b) parser must strip prose preamble before JSON
  (c) parser must handle ANSI+prose preamble before JSON
  (d) parser must reject all-prose input with no JSON object
  (e) parser must accept clean JSON with no preamble (regression guard)
  (f) LifecycleBatchRunner must write lifecycle_schema_version>=2 to DB on success
  (g) failed invoker must not update lifecycle_schema_version in DB
"""

from __future__ import annotations

import queue
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.global_repos.lifecycle_batch_runner import (
    LifecycleBatchRunner,
    LifecycleLockUnavailableError,
)
from code_indexer.global_repos.unified_response_parser import (
    UnifiedResponseParser,
    UnifiedResponseParseError,
    UnifiedResult,
)


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

_VALID_LIFECYCLE = {
    "ci_system": "github-actions",
    "deployment_target": "kubernetes",
    "language_ecosystem": "python/poetry",
    "build_system": "poetry",
    "testing_framework": "pytest",
    "confidence": "high",
}

_VALID_RESULT = UnifiedResult(
    description="A test repository description.",
    lifecycle=_VALID_LIFECYCLE,
)

_VALID_JSON_OUTPUT = (
    '{"description": "A test repository description.", '
    '"lifecycle": {"ci_system": "github-actions", "deployment_target": "kubernetes", '
    '"language_ecosystem": "python/poetry", "build_system": "poetry", '
    '"testing_framework": "pytest", "confidence": "high"}}'
)


class _StubJobTracker:
    """Minimal job tracker for unit tests."""

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
        self.dirty_signals: int = 0

    def signal_dirty(self) -> None:
        self.dirty_signals += 1


def _make_stub_scheduler(golden_repos_dir: Path):
    """Build a real RefreshScheduler-compatible stub backed by WriteLockManager."""
    from code_indexer.global_repos.write_lock_manager import WriteLockManager

    mgr = WriteLockManager(golden_repos_dir)

    class _StubScheduler:
        def acquire_write_lock(self, alias: str, owner_name: str = "") -> bool:
            return bool(mgr.acquire(alias, owner_name=owner_name))

        def release_write_lock(self, alias: str, owner_name: str = "") -> bool:
            return bool(mgr.release(alias, owner_name=owner_name))

    return _StubScheduler()


# ---------------------------------------------------------------------------
# (a) Bug A: per-alias locking — two concurrent jobs for DIFFERENT aliases
# must NOT raise LifecycleLockUnavailableError
# ---------------------------------------------------------------------------


def test_concurrent_lifecycle_jobs_different_aliases_do_not_contend(tmp_path):
    """
    Two LifecycleBatchRunner.run([alias]) calls for DIFFERENT aliases must
    complete successfully when run concurrently.

    Before the fix: both jobs call acquire_write_lock("cidx-meta") — the
    global key.  The second job gets False and raises
    LifecycleLockUnavailableError.

    After the fix: each job acquires a per-alias key ("lifecycle:alias-a",
    "lifecycle:alias-b"), so they proceed in parallel with no contention.
    """
    cidx_meta = tmp_path / "cidx-meta"
    cidx_meta.mkdir()

    for alias in ("alias-a", "alias-b"):
        (tmp_path / alias).mkdir()

    scheduler = _make_stub_scheduler(tmp_path)
    result_queue: queue.Queue = queue.Queue()

    # Barrier maximises the lock contention window: both threads enter the
    # invoker simultaneously, so both must have acquired their locks already.
    # This is only reachable after the fix (before the fix, the second thread
    # fails at lock acquisition and never calls the invoker).
    barrier = threading.Barrier(2)

    def run_one(alias: str) -> None:
        job_tracker = _StubJobTracker()
        debouncer = _StubDebouncer()

        def invoker(a: str, repo_path: Path) -> UnifiedResult:
            barrier.wait(timeout=5)
            return _VALID_RESULT

        runner = LifecycleBatchRunner(
            golden_repos_dir=tmp_path,
            job_tracker=job_tracker,
            refresh_scheduler=scheduler,
            debouncer=debouncer,
            claude_cli_invoker=invoker,
            sub_batch_size_override=1,
        )
        try:
            runner.run([alias], parent_job_id=f"job-{alias}")
            result_queue.put(("ok", alias))
        except LifecycleLockUnavailableError as exc:
            result_queue.put(("lock_error", alias, str(exc)))
        except Exception as exc:
            result_queue.put(("error", alias, str(exc)))

    threads = [
        threading.Thread(target=run_one, args=("alias-a",), daemon=True),
        threading.Thread(target=run_one, args=("alias-b",), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    results = []
    while not result_queue.empty():
        results.append(result_queue.get_nowait())

    errors = [r for r in results if r[0] != "ok"]
    assert not errors, (
        f"Lock contention between jobs for different aliases: {errors}\n"
        "Fix: use per-alias lock key, not global 'cidx-meta'."
    )
    assert len(results) == 2, f"Expected 2 results, got {len(results)}: {results}"

    assert (cidx_meta / "alias-a.md").exists(), "alias-a.md not written"
    assert (cidx_meta / "alias-b.md").exists(), "alias-b.md not written"


# ---------------------------------------------------------------------------
# (b) Bug B: prose preamble stripping in UnifiedResponseParser
# ---------------------------------------------------------------------------


def test_parser_accepts_json_with_prose_preamble():
    """
    UnifiedResponseParser.parse must succeed when Claude outputs prose
    text before the JSON object.

    Before the fix: json.loads receives the full string starting with prose
    text and raises JSONDecodeError.

    After the fix: the parser strips everything before the first '{'.
    """
    raw = (
        "I'll analyze this repository now.\n\n"
        "Here is the result:\n\n" + _VALID_JSON_OUTPUT
    )

    result = UnifiedResponseParser.parse(raw)

    assert result.description == "A test repository description."
    assert result.lifecycle["confidence"] == "high"
    assert result.lifecycle["ci_system"] == "github-actions"


# ---------------------------------------------------------------------------
# (c) Bug B variant: ANSI escapes then prose then JSON
# ---------------------------------------------------------------------------


def test_parser_accepts_json_preceded_by_ansi_then_prose():
    """
    Parser must handle ANSI escapes followed by prose, followed by JSON.
    Real-world pattern: script wrapper emits ANSI colour codes, then
    Claude outputs a sentence before the JSON.
    """
    ansi_prefix = "\x1b[0m\x1b[32m"
    prose = "Analyzing the codebase...\n"
    raw = ansi_prefix + prose + _VALID_JSON_OUTPUT

    result = UnifiedResponseParser.parse(raw)

    assert result.lifecycle["confidence"] == "high"


# ---------------------------------------------------------------------------
# (d) Bug B: all-prose input must be rejected (fail-closed)
# ---------------------------------------------------------------------------


def test_parser_rejects_all_prose_no_json_object():
    """
    If the output contains no JSON object at all, parser raises
    UnifiedResponseParseError (fail-closed: no partial result returned).
    """
    raw = "I cannot find any lifecycle information in this repository."

    with pytest.raises(UnifiedResponseParseError):
        UnifiedResponseParser.parse(raw)


# ---------------------------------------------------------------------------
# (e) Regression guard: parser must still accept clean JSON with no preamble
# ---------------------------------------------------------------------------


def test_parser_accepts_clean_json_no_preamble():
    """
    The preamble-stripping fix must not break clean JSON with no preamble.
    """
    result = UnifiedResponseParser.parse(_VALID_JSON_OUTPUT)

    assert result.description == "A test repository description."


# ---------------------------------------------------------------------------
# (f) Bug C: lifecycle_schema_version written to DB after successful run
# ---------------------------------------------------------------------------


def _create_tracking_db(db_path: Path) -> None:
    """Create a minimal description_refresh_tracking table for the test."""
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS description_refresh_tracking (
                repo_alias TEXT PRIMARY KEY NOT NULL,
                last_run TEXT,
                next_run TEXT,
                status TEXT,
                error TEXT,
                last_known_commit TEXT,
                last_known_files_processed INTEGER,
                last_known_indexed_at TEXT,
                created_at TEXT,
                updated_at TEXT,
                lifecycle_schema_version INTEGER DEFAULT 0
            )
            """
        )
        conn.commit()


def _get_tracking_row(db_path: Path, alias: str) -> Optional[Dict[str, Any]]:
    with closing(sqlite3.connect(str(db_path))) as conn:
        row = conn.execute(
            "SELECT status, lifecycle_schema_version FROM description_refresh_tracking "
            "WHERE repo_alias = ?",
            (alias,),
        ).fetchone()
    if row is None:
        return None
    return {"status": row[0], "lifecycle_schema_version": row[1]}


def test_lifecycle_schema_version_written_to_db_after_successful_run(tmp_path):
    """
    After LifecycleBatchRunner.run([alias]) completes successfully,
    description_refresh_tracking.lifecycle_schema_version must be >= 2.

    Before the fix:
      - upsert_tracking's valid_fields did not include lifecycle_schema_version
      - The column stayed at 0 for all processed aliases

    After the fix:
      - LifecycleBatchRunner calls upsert_tracking(
            repo_alias=alias,
            lifecycle_schema_version=CURRENT_LIFECYCLE_SCHEMA_VERSION,
            status='completed',
        )
      - CURRENT_LIFECYCLE_SCHEMA_VERSION must be >= 2
    """
    from code_indexer.global_repos.lifecycle_batch_runner import (
        CURRENT_LIFECYCLE_SCHEMA_VERSION,
    )
    from code_indexer.server.storage.sqlite_backends import (
        DescriptionRefreshTrackingBackend,
    )

    assert CURRENT_LIFECYCLE_SCHEMA_VERSION >= 2, (
        f"CURRENT_LIFECYCLE_SCHEMA_VERSION is {CURRENT_LIFECYCLE_SCHEMA_VERSION}, "
        "must be >= 2 per Story #876 contract"
    )

    db_path = tmp_path / "test.db"
    _create_tracking_db(db_path)
    tracking_backend = DescriptionRefreshTrackingBackend(str(db_path))

    tracking_backend.upsert_tracking(
        repo_alias="test-repo",
        status="pending",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )

    cidx_meta = tmp_path / "cidx-meta"
    cidx_meta.mkdir()
    (tmp_path / "test-repo").mkdir()

    scheduler = _make_stub_scheduler(tmp_path)
    job_tracker = _StubJobTracker()
    debouncer = _StubDebouncer()

    def invoker(alias: str, repo_path: Path) -> UnifiedResult:
        return _VALID_RESULT

    runner = LifecycleBatchRunner(
        golden_repos_dir=tmp_path,
        job_tracker=job_tracker,
        refresh_scheduler=scheduler,
        debouncer=debouncer,
        claude_cli_invoker=invoker,
        tracking_backend=tracking_backend,
        sub_batch_size_override=1,
    )

    runner.run(["test-repo"], parent_job_id="job-001")

    row = _get_tracking_row(db_path, "test-repo")
    assert row is not None, "No tracking row found after run"
    assert row["lifecycle_schema_version"] >= 2, (
        f"lifecycle_schema_version is {row['lifecycle_schema_version']} after run, "
        "expected >= 2.\n"
        "Fix: LifecycleBatchRunner must call upsert_tracking with "
        "lifecycle_schema_version=CURRENT_LIFECYCLE_SCHEMA_VERSION after success."
    )
    assert row["status"] == "completed", (
        f"Expected status='completed', got {row['status']!r}"
    )


# ---------------------------------------------------------------------------
# (g) Fail-closed: invoker failure must not update lifecycle_schema_version
# ---------------------------------------------------------------------------


def test_lifecycle_schema_version_not_updated_on_invoker_failure(tmp_path):
    """
    When the invoker raises for an alias, lifecycle_schema_version must
    NOT be updated to >= 2 for that alias (fail-closed contract).
    """
    from code_indexer.server.storage.sqlite_backends import (
        DescriptionRefreshTrackingBackend,
    )

    db_path = tmp_path / "test.db"
    _create_tracking_db(db_path)
    tracking_backend = DescriptionRefreshTrackingBackend(str(db_path))

    tracking_backend.upsert_tracking(
        repo_alias="broken-repo",
        status="pending",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )

    cidx_meta = tmp_path / "cidx-meta"
    cidx_meta.mkdir()
    (tmp_path / "broken-repo").mkdir()

    scheduler = _make_stub_scheduler(tmp_path)
    job_tracker = _StubJobTracker()
    debouncer = _StubDebouncer()

    def failing_invoker(alias: str, repo_path: Path) -> UnifiedResult:
        raise RuntimeError("Claude CLI failed")

    runner = LifecycleBatchRunner(
        golden_repos_dir=tmp_path,
        job_tracker=job_tracker,
        refresh_scheduler=scheduler,
        debouncer=debouncer,
        claude_cli_invoker=failing_invoker,
        tracking_backend=tracking_backend,
        sub_batch_size_override=1,
    )

    # Must NOT raise — per-repo errors are logged, not propagated
    runner.run(["broken-repo"], parent_job_id="job-fail")

    row = _get_tracking_row(db_path, "broken-repo")
    assert row is not None
    assert row["lifecycle_schema_version"] == 0, (
        f"lifecycle_schema_version should be 0 on failure, "
        f"got {row['lifecycle_schema_version']}"
    )
