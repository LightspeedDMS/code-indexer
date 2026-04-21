"""
Unit test for Story #876 — unified lifecycle write path through a REAL
LifecycleBatchRunner.

Unlike the sibling test_golden_repo_manager_lifecycle_hook.py which patches
LifecycleBatchRunner entirely at the class level (preventing real execution),
this test intercepts the constructor via side_effect to capture each real
instance.  The capturing constructor immediately starts a patch.object patcher
on the real instance's run() method (wraps= so the real code still executes),
stores it, then the test stops all patchers in a try/finally block after the
SUT call so the spy mock can be queried.

Collaboration boundary:
  * job_tracker: REAL JobTracker on a temp SQLite DB.
  * refresh_scheduler: Stub with acquire_write_lock returning True.
  * lifecycle_debouncer: MagicMock.
  * mgr.lifecycle_invoker: stub callable (passed as claude_cli_invoker= to the
    real LifecycleBatchRunner constructor) returning a fixed UnifiedResult — no
    Claude CLI subprocess is launched.
  * LifecycleBatchRunner: REAL class — side_effect calls the real constructor
    and the real run() executes (confirmed by file creation + spy assertion).
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, List, Tuple
from unittest.mock import MagicMock, patch

import pytest
import yaml

from code_indexer.global_repos.lifecycle_batch_runner import (
    CURRENT_LIFECYCLE_SCHEMA_VERSION,
    LifecycleBatchRunner,
)
from code_indexer.global_repos.unified_response_parser import UnifiedResult
from code_indexer.server.services.job_tracker import JobTracker

_RUNNER_SITE = (
    "code_indexer.server.repositories.golden_repo_manager.LifecycleBatchRunner"
)

_REQUIRED_LIFECYCLE_KEYS = (
    "ci_system",
    "deployment_target",
    "language_ecosystem",
    "build_system",
    "testing_framework",
    "confidence",
)

_FIXED_LIFECYCLE: dict = {
    "ci_system": "GitHub Actions",
    "deployment_target": "AWS Lambda",
    "language_ecosystem": "Python",
    "build_system": "setuptools",
    "testing_framework": "pytest",
    "confidence": "high",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def atomic_db_path(tmp_path: Any) -> str:
    db = tmp_path / "test_unified_lifecycle.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS background_jobs (
            job_id TEXT PRIMARY KEY NOT NULL,
            operation_type TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            result TEXT,
            error TEXT,
            progress INTEGER NOT NULL DEFAULT 0,
            username TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            cancelled INTEGER NOT NULL DEFAULT 0,
            repo_alias TEXT,
            resolution_attempts INTEGER NOT NULL DEFAULT 0,
            claude_actions TEXT,
            failure_reason TEXT,
            extended_error TEXT,
            language_resolution_status TEXT,
            progress_info TEXT,
            metadata TEXT
        )"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_active_job_per_repo
            ON background_jobs(operation_type, repo_alias)
            WHERE status IN ('pending', 'running') AND repo_alias IS NOT NULL"""
        )
        conn.commit()
    return str(db)


@pytest.fixture
def real_job_tracker(atomic_db_path: str) -> JobTracker:
    return JobTracker(atomic_db_path)


@pytest.fixture
def stub_scheduler() -> MagicMock:
    sched = MagicMock()
    sched.acquire_write_lock.return_value = True
    sched.release_write_lock.return_value = None
    return sched


@pytest.fixture
def wired_manager(
    tmp_path: Any, real_job_tracker: JobTracker, stub_scheduler: MagicMock
) -> Any:
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    data_dir = tmp_path / "server-data"
    (data_dir / "golden-repos" / "cidx-meta").mkdir(parents=True)
    mgr = GoldenRepoManager(data_dir=str(data_dir))
    mgr.job_tracker = real_job_tracker
    # lifecycle_invoker is passed as claude_cli_invoker= to LifecycleBatchRunner
    mgr.lifecycle_invoker = lambda alias, repo_path: UnifiedResult(
        description=f"Description for {alias}",
        lifecycle=dict(_FIXED_LIFECYCLE),
    )
    mgr.lifecycle_debouncer = MagicMock()
    mgr._refresh_scheduler = stub_scheduler
    return mgr


# ---------------------------------------------------------------------------
# Assertion helpers (keep test function under 50 lines)
# ---------------------------------------------------------------------------


def _assert_frontmatter(md_path: Any) -> None:
    """Assert exactly len(_REQUIRED_LIFECYCLE_KEYS) lifecycle keys and schema_version >= CURRENT."""
    text = md_path.read_text(encoding="utf-8")
    parts = text.split("---")
    assert len(parts) >= 3, f"No frontmatter delimiters in {md_path}"
    fm = yaml.safe_load(parts[1])
    assert "lifecycle" in fm, f"'lifecycle' key missing. Keys: {list(fm.keys())}"
    lc = fm["lifecycle"]
    assert isinstance(lc, dict), f"lifecycle must be dict, got {type(lc).__name__}"
    expected_count = len(_REQUIRED_LIFECYCLE_KEYS)
    assert len(lc) == expected_count, (
        f"Expected exactly {expected_count} lifecycle keys, got {len(lc)}: {list(lc.keys())}"
    )
    for key in _REQUIRED_LIFECYCLE_KEYS:
        assert key in lc and lc[key], f"lifecycle.{key} missing or empty"
    version = fm.get("lifecycle_schema_version")
    assert isinstance(version, int) and version >= CURRENT_LIFECYCLE_SCHEMA_VERSION, (
        f"lifecycle_schema_version={version!r} < required {CURRENT_LIFECYCLE_SCHEMA_VERSION}"
    )


def _assert_no_bad_logs(caplog: Any) -> None:
    """Assert no skip-guard or lock-contention log entries."""
    skip = [
        r
        for r in caplog.records
        if "skipping lifecycle registration hook" in r.message.lower()
    ]
    assert not skip, (
        f"Staged-rollout skip guard log still fires: {[r.message for r in skip]}"
    )
    lock = [r for r in caplog.records if "write lock held" in r.message.lower()]
    assert not lock, (
        f"Write lock contention log still fires: {[r.message for r in lock]}"
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_unified_lifecycle_write_creates_file_with_required_keys(
    wired_manager: Any, caplog: Any, stub_scheduler: MagicMock
) -> None:
    """
    Real LifecycleBatchRunner (captured via constructor side_effect) + stub
    invoker writes cidx-meta/<alias>.md with the required lifecycle sub-keys
    and lifecycle_schema_version >= 2.  run() invocation is confirmed via a
    patch.object spy started on the captured instance.  All patchers are
    stopped in a finally block to guarantee cleanup.
    No skip-guard or lock-contention logs must appear.
    """
    alias = "test-unified-repo"
    cidx_meta = Path(wired_manager.golden_repos_dir) / "cidx-meta"

    # Stores (real_instance, run_patcher, run_spy) per constructor call.
    captured: List[Tuple[LifecycleBatchRunner, Any, MagicMock]] = []

    def _capturing_constructor(*args: Any, **kwargs: Any) -> LifecycleBatchRunner:
        real = LifecycleBatchRunner(*args, **kwargs)
        # wraps= ensures real run() executes; spy records the call for later assertion.
        patcher = patch.object(real, "run", wraps=real.run)
        run_spy = patcher.start()
        captured.append((real, patcher, run_spy))
        return real

    with patch(_RUNNER_SITE, side_effect=_capturing_constructor):
        try:
            with caplog.at_level(logging.DEBUG):
                wired_manager._register_lifecycle_after_registration(
                    alias, submitter_username="admin"
                )
        finally:
            # Guaranteed cleanup: stop all instance patchers regardless of exceptions.
            for _inst, patcher, _spy in captured:
                patcher.stop()

    assert len(captured) == 1, (
        f"Expected exactly one LifecycleBatchRunner constructed, got {len(captured)}"
    )
    _inst, _patcher, run_spy = captured[0]
    run_spy.assert_called_once()

    md_path = cidx_meta / f"{alias}.md"
    assert md_path.exists(), (
        f"cidx-meta/{alias}.md not created. Dir: {list(cidx_meta.iterdir())}"
    )
    _assert_frontmatter(md_path)
    _assert_no_bad_logs(caplog)

    keys = [
        (c.args[0] if c.args else c.kwargs.get("key", ""))
        for c in stub_scheduler.acquire_write_lock.call_args_list
    ]
    assert any(alias in str(k) for k in keys), (
        f"acquire_write_lock never called with alias key. Called with: {keys}"
    )
