"""
Unit tests for Story #724 AC13: DuplicateJobError from refresh collision is
logged at INFO level (not WARNING or ERROR).

Two test classes, one test each, sharing _Base setUp/tearDown:
  TestCollisionLogLevel    -- DuplicateJobError -> INFO log
  TestGenuineErrorLogLevel -- RuntimeError      -> ERROR log
"""

import logging
import tempfile
import shutil
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.server.repositories.background_jobs import DuplicateJobError

_ALIAS = "test-repo-global"
_GIT_URL = "git@github.com:test/repo.git"
# next_refresh = "0" makes the repo always due (0 < now)
_DUE_REPO = {
    "alias_name": _ALIAS,
    "repo_url": _GIT_URL,
    "next_refresh": "0",
    "enable_temporal": False,
    "enable_scip": False,
}


def _make_scheduler(tmp_path: Path, registry: Mock) -> RefreshScheduler:
    """Construct a real RefreshScheduler with minimal stubs."""
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir(parents=True, exist_ok=True)
    config_source = Mock()
    config_source.get_global_refresh_interval.return_value = 3600
    return RefreshScheduler(
        golden_repos_dir=str(golden_dir),
        config_source=config_source,
        query_tracker=Mock(spec=QueryTracker),
        cleanup_manager=Mock(spec=CleanupManager),
        registry=registry,
    )


def _make_registry() -> Mock:
    """Registry that returns one due git repo."""
    registry = Mock()
    registry.list_global_repos.return_value = [_DUE_REPO]
    registry.update_next_refresh.return_value = None
    return registry


def _stop_after_first_refresh(scheduler: RefreshScheduler):
    """Side-effect for update_next_refresh that stops the loop after one pass."""

    def _side_effect(alias_name, next_refresh):
        scheduler._running = False

    return _side_effect


class _Base(TestCase):
    """Shared setUp/tearDown."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._tmp_path = Path(self._tmp)
        self._registry = _make_registry()
        self._scheduler = _make_scheduler(self._tmp_path, self._registry)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestCollisionLogLevel(_Base):
    """DuplicateJobError from refresh collision is logged at INFO."""

    def test_refresh_collision_logs_at_info_level(self) -> None:
        """When _submit_refresh_job raises DuplicateJobError, the scheduler
        logs at INFO level with the alias name in the message."""
        dup_err = DuplicateJobError("global_repo_refresh", _ALIAS, "job-123")
        self._registry.update_next_refresh.side_effect = _stop_after_first_refresh(
            self._scheduler
        )
        self._scheduler._running = True

        with patch.object(self._scheduler, "cleanup_stale_write_mode_markers"):
            with patch.object(
                self._scheduler, "_submit_refresh_job", side_effect=dup_err
            ):
                with self.assertLogs(
                    "code_indexer.global_repos.refresh_scheduler", level=logging.DEBUG
                ) as log_ctx:
                    self._scheduler._scheduler_loop()

        info_with_alias = [
            r
            for r in log_ctx.records
            if r.levelno == logging.INFO and _ALIAS in r.getMessage()
        ]
        self.assertTrue(
            info_with_alias,
            f"Expected INFO log containing '{_ALIAS}'; records: "
            f"{[(r.levelno, r.getMessage()) for r in log_ctx.records]}",
        )
        bad = [
            r
            for r in log_ctx.records
            if r.levelno >= logging.WARNING and "prior refresh" in r.getMessage()
        ]
        self.assertEqual(
            bad,
            [],
            f"Collision must not appear at WARNING/ERROR; got: {[r.getMessage() for r in bad]}",
        )


class TestGenuineErrorLogLevel(_Base):
    """Non-DuplicateJobError genuine failures are logged at ERROR."""

    def test_genuine_refresh_errors_still_logged_at_error(self) -> None:
        """When _submit_refresh_job raises a generic RuntimeError, the scheduler
        logs it at ERROR level."""
        self._registry.update_next_refresh.side_effect = _stop_after_first_refresh(
            self._scheduler
        )
        self._scheduler._running = True

        with patch.object(self._scheduler, "cleanup_stale_write_mode_markers"):
            with patch.object(
                self._scheduler,
                "_submit_refresh_job",
                side_effect=RuntimeError("unexpected failure"),
            ):
                with self.assertLogs(
                    "code_indexer.global_repos.refresh_scheduler", level=logging.DEBUG
                ) as log_ctx:
                    self._scheduler._scheduler_loop()

        error_records = [r for r in log_ctx.records if r.levelno >= logging.ERROR]
        self.assertTrue(
            error_records,
            f"Expected ERROR log for genuine failure; records: "
            f"{[(r.levelno, r.getMessage()) for r in log_ctx.records]}",
        )
