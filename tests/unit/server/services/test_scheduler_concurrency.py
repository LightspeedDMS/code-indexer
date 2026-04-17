"""
Tests for Story #728 AC5/AC5b: Concurrency fix — ThreadPoolExecutor replaces
unbounded threading.Thread spawning in DescriptionRefreshScheduler.

4 tests:
1. test_executor_initialized_with_correct_max_workers — max_workers from config (3)
2. test_executor_default_max_workers_is_two — unset config yields real default (2)
3. test_shutdown_event_set_before_executor_shutdown — ordering + wait=True verified
4. test_submission_skipped_after_shutdown — control proves eligibility, then shutdown blocks
"""

import sys
import concurrent.futures
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SRC_ROOT = Path(__file__).parent.parent.parent.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_file(tmp_path):
    from code_indexer.server.storage.database_manager import DatabaseConnectionManager

    db = tmp_path / "test.db"
    mgr = DatabaseConnectionManager(str(db))
    conn = mgr.get_connection()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS description_refresh_tracking (
               repo_alias TEXT PRIMARY KEY, last_run TEXT, next_run TEXT,
               status TEXT DEFAULT 'pending', error TEXT,
               last_known_commit TEXT, last_known_files_processed INTEGER,
               last_known_indexed_at TEXT, created_at TEXT, updated_at TEXT,
               lifecycle_schema_version INTEGER DEFAULT 0)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS golden_repos_metadata (
               alias TEXT PRIMARY KEY, repo_url TEXT, default_branch TEXT,
               clone_path TEXT, created_at TEXT,
               enable_temporal INTEGER DEFAULT 0, temporal_options TEXT,
               category_id INTEGER, category_auto_assigned INTEGER DEFAULT 0)"""
    )
    conn.commit()
    mgr.close_all()
    return db


def _make_config(tmp_path, max_workers=None):
    """Build a mock config manager. When max_workers is None, leave max_concurrent_claude_cli at its real default."""
    from code_indexer.server.utils.config_manager import (
        ClaudeIntegrationConfig,
        ServerConfig,
    )

    cfg = ServerConfig(server_dir=str(tmp_path))
    cfg.claude_integration_config = ClaudeIntegrationConfig()
    cfg.claude_integration_config.description_refresh_enabled = True
    cfg.claude_integration_config.description_refresh_interval_hours = 24
    if max_workers is not None:
        cfg.claude_integration_config.max_concurrent_claude_cli = max_workers
    m = MagicMock()
    m.load_config.return_value = cfg
    return m


def _make_scheduler(db_file, mock_config, meta_dir=None):
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )

    return DescriptionRefreshScheduler(
        db_path=str(db_file),
        config_manager=mock_config,
        claude_cli_manager=MagicMock(),
        meta_dir=meta_dir,
    )


def _write_prior_md(meta_dir: Path, alias: str) -> None:
    """Write a valid prior .md so _get_refresh_prompt can build a prompt from real data."""
    (meta_dir / f"{alias}.md").write_text(
        "---\nlast_analyzed: 2025-01-01T00:00:00+00:00\n---\nMinimal prior.\n",
        encoding="utf-8",
    )


def _count_submit_calls_for_single_pass(scheduler) -> int:
    """
    Patch _executor.submit, run one scheduler pass, and return the call count.

    Only the executor's submit method is patched — the rest of the scheduler
    (prompt generation, phase tasks, etc.) runs normally.
    """
    calls = []
    mock_future = MagicMock()
    mock_future.add_done_callback = lambda fn: None

    with patch.object(
        scheduler._executor,
        "submit",
        side_effect=lambda fn, *a, **kw: calls.append(fn) or mock_future,
    ):
        scheduler._run_loop_single_pass()
    return len(calls)


def _seed_stale_repo(db_file, alias, clone_path):
    """Insert a stale repo into both golden_repos_metadata and description_refresh_tracking."""
    from code_indexer.server.storage.sqlite_backends import (
        DescriptionRefreshTrackingBackend,
        GoldenRepoMetadataSqliteBackend,
    )

    golden_backend = GoldenRepoMetadataSqliteBackend(str(db_file))
    try:
        golden_backend.ensure_table_exists()
        golden_backend.add_repo(
            alias=alias,
            repo_url="https://example.com/repo.git",
            default_branch="main",
            clone_path=clone_path,
            created_at="2025-01-01T00:00:00+00:00",
        )
    finally:
        golden_backend.close()

    tracking_backend = DescriptionRefreshTrackingBackend(str(db_file))
    try:
        tracking_backend.upsert_tracking(
            repo_alias=alias,
            last_run="2025-01-01T00:00:00+00:00",
            next_run="2025-01-01T01:00:00+00:00",  # past -> stale
            status="completed",
            updated_at="2025-01-01T00:00:00+00:00",
        )
    finally:
        tracking_backend.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExecutorInitialization:
    def test_executor_initialized_with_correct_max_workers(self, db_file, tmp_path):
        """
        (1) _executor is a ThreadPoolExecutor with max_workers = 3 when
        claude_integration_config.max_concurrent_claude_cli = 3 (non-default).
        """
        mock_config = _make_config(tmp_path, max_workers=3)
        scheduler = _make_scheduler(db_file, mock_config)

        assert hasattr(scheduler, "_executor"), (
            "DescriptionRefreshScheduler must have _executor attribute"
        )
        assert isinstance(scheduler._executor, concurrent.futures.ThreadPoolExecutor), (
            "_executor must be a ThreadPoolExecutor"
        )
        assert scheduler._executor._max_workers == 3, (
            f"Expected 3 workers, got {scheduler._executor._max_workers}"
        )
        scheduler._executor.shutdown(wait=False)

    def test_executor_default_max_workers_is_two(self, db_file, tmp_path):
        """
        (2) When max_concurrent_claude_cli is left at its real default (2),
        _executor uses exactly 2 workers.

        The fixture does NOT set max_concurrent_claude_cli so the real
        dataclass default is exercised.
        """
        from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

        # Verify the real default first so the test fails loudly if it changes
        real_default = ClaudeIntegrationConfig().max_concurrent_claude_cli
        assert real_default == 2, (
            f"ClaudeIntegrationConfig default changed to {real_default}; update this test"
        )

        mock_config = _make_config(tmp_path, max_workers=None)  # leave at real default
        scheduler = _make_scheduler(db_file, mock_config)

        assert isinstance(scheduler._executor, concurrent.futures.ThreadPoolExecutor)
        assert scheduler._executor._max_workers == real_default, (
            f"Expected {real_default} workers (real default), got {scheduler._executor._max_workers}"
        )
        scheduler._executor.shutdown(wait=False)


class TestShutdownOrdering:
    def test_shutdown_event_set_before_executor_shutdown(self, db_file, tmp_path):
        """
        (3) In stop(), _shutdown_event.set() must be called BEFORE
        _executor.shutdown(wait=True).

        Uses patch.object to spy on _executor.shutdown without monkey-patching.
        Verifies both the ordering and that wait=True is passed.
        """
        mock_config = _make_config(tmp_path, max_workers=2)
        scheduler = _make_scheduler(db_file, mock_config)

        calls = []  # list of (event_was_set, wait_arg)

        real_executor = scheduler._executor

        def _spy_shutdown(wait=True):
            calls.append((scheduler._shutdown_event.is_set(), wait))
            # Delegate with wait=False to avoid blocking the test
            real_executor.__class__.shutdown(real_executor, wait=False)

        with patch.object(real_executor, "shutdown", side_effect=_spy_shutdown):
            scheduler.stop()

        assert calls, "_executor.shutdown was never called during stop()"
        event_was_set, wait_arg = calls[0]
        assert event_was_set is True, (
            "_shutdown_event must be set BEFORE _executor.shutdown() is called"
        )
        assert wait_arg is True, (
            "stop() must call _executor.shutdown(wait=True) to drain queued tasks"
        )


class TestSubmissionAfterShutdown:
    def test_submission_skipped_after_shutdown(self, db_file, tmp_path):
        """
        (4) After _shutdown_event is set, _run_loop_single_pass must not submit
        any new tasks to _executor.

        Setup: real meta_dir + valid prior .md so _get_refresh_prompt succeeds
        without mocking SUT internals.

        Control condition: shutdown_event CLEAR → submission attempted.
        Subject condition: shutdown_event SET → submission NOT attempted.
        """
        fake_repo = tmp_path / "repo"
        fake_repo.mkdir()
        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        _seed_stale_repo(db_file, "shutdown-test", str(fake_repo))
        _write_prior_md(meta_dir, "shutdown-test")

        # --- Control: shutdown_event clear → submission attempted ---
        mock_config = _make_config(tmp_path, max_workers=2)
        scheduler_control = _make_scheduler(db_file, mock_config, meta_dir=meta_dir)
        control_count = _count_submit_calls_for_single_pass(scheduler_control)
        scheduler_control._executor.shutdown(wait=False)

        assert control_count > 0, (
            "Control condition failed: expected _executor.submit to be called "
            "when shutdown_event is clear and repo is stale with changes. "
            "Check that meta_dir and prior .md are wired correctly."
        )

        # --- Subject: shutdown_event set → submission NOT attempted ---
        mock_config2 = _make_config(tmp_path, max_workers=2)
        scheduler_subject = _make_scheduler(db_file, mock_config2, meta_dir=meta_dir)
        scheduler_subject._shutdown_event.set()
        subject_count = _count_submit_calls_for_single_pass(scheduler_subject)
        scheduler_subject._executor.shutdown(wait=False)

        assert subject_count == 0, (
            "_executor.submit must NOT be called after _shutdown_event is set, "
            f"but got {subject_count} calls"
        )
