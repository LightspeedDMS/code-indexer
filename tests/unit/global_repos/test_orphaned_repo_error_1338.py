"""
Unit tests for Bug #1338: harden orphan-skip with a typed OrphanedRepoError
instead of error-message substring matching (follow-up to #1336).

#1336 made lifecycle_backfill / global_repo_refresh skip orphaned golden
aliases (registry row present, on-disk clone absent) by matching the raised
error MESSAGE TEXT:
  - lifecycle_batch_runner.py matched the substring
    "repo_path does not exist for alias" (a duplicated copy of the message
    raised at lifecycle_claude_cli_invoker.py).
  - refresh_scheduler.py caught a bare ValueError from GitPullUpdater.__init__.

This is brittle: reword either raise message and orphan-skip silently stops
working, while the #1336 stand-in tests (hardcoding the same message) would
stay green.

Fix: a dedicated OrphanedRepoError(ValueError), defined once, raised at BOTH
orphaned-clone source sites -- and ONLY for that case, not other input
validation errors -- then caught by TYPE at both skip sites.

These tests drive the REAL LifecycleClaudeCliInvoker._validate_repo_inputs and
the REAL GitPullUpdater.__init__ (never a message-hardcoded stand-in), so a
future reword of either raise message is caught by CI via a failing test
instead of silently regressing orphan-skip.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.global_repos.orphaned_repo_error import OrphanedRepoError
from code_indexer.global_repos.lifecycle_claude_cli_invoker import (
    LifecycleClaudeCliInvoker,
)
from code_indexer.global_repos.git_pull_updater import GitPullUpdater


# ---------------------------------------------------------------------------
# OrphanedRepoError is a ValueError subclass (existing `except ValueError`
# callers must be unaffected).
# ---------------------------------------------------------------------------


class TestOrphanedRepoErrorIsValueErrorSubclass:
    def test_is_subclass_of_value_error(self) -> None:
        assert issubclass(OrphanedRepoError, ValueError)

    def test_instance_is_caught_by_bare_except_value_error(self) -> None:
        try:
            raise OrphanedRepoError("orphaned clone")
        except ValueError as exc:
            assert isinstance(exc, OrphanedRepoError)
        else:
            pytest.fail("OrphanedRepoError must be catchable as ValueError")


# ---------------------------------------------------------------------------
# Source site 1: LifecycleClaudeCliInvoker._validate_repo_inputs (REAL class)
# ---------------------------------------------------------------------------


class TestLifecycleClaudeCliInvokerRaisesTypedOrphanError:
    def test_missing_repo_path_raises_orphaned_repo_error(self, tmp_path: Path) -> None:
        """The missing-clone-for-a-registered-alias case must raise the typed
        OrphanedRepoError, not a plain ValueError."""
        invoker = LifecycleClaudeCliInvoker()
        missing_path = tmp_path / "does-not-exist"

        with pytest.raises(OrphanedRepoError) as exc_info:
            invoker._validate_repo_inputs("some-alias", missing_path)

        assert "some-alias" in str(exc_info.value)

    def test_empty_alias_is_plain_value_error_not_orphaned(
        self, tmp_path: Path
    ) -> None:
        """A non-orphan input-validation error must stay a PLAIN ValueError
        and must NOT be an OrphanedRepoError."""
        invoker = LifecycleClaudeCliInvoker()

        with pytest.raises(ValueError) as exc_info:
            invoker._validate_repo_inputs("", tmp_path)

        assert not isinstance(exc_info.value, OrphanedRepoError)

    def test_none_repo_path_is_plain_value_error_not_orphaned(self) -> None:
        invoker = LifecycleClaudeCliInvoker()

        with pytest.raises(ValueError) as exc_info:
            invoker._validate_repo_inputs("some-alias", None)  # type: ignore[arg-type]

        assert not isinstance(exc_info.value, OrphanedRepoError)

    def test_not_a_directory_is_plain_value_error_not_orphaned(
        self, tmp_path: Path
    ) -> None:
        a_file = tmp_path / "a-file.txt"
        a_file.write_text("not a directory")
        invoker = LifecycleClaudeCliInvoker()

        with pytest.raises(ValueError) as exc_info:
            invoker._validate_repo_inputs("some-alias", a_file)

        assert not isinstance(exc_info.value, OrphanedRepoError)


# ---------------------------------------------------------------------------
# Source site 2: GitPullUpdater.__init__ (REAL class)
# ---------------------------------------------------------------------------


class TestGitPullUpdaterRaisesTypedOrphanError:
    def test_missing_repo_path_raises_orphaned_repo_error(self, tmp_path: Path) -> None:
        missing_path = tmp_path / "missing-clone"

        with pytest.raises(OrphanedRepoError) as exc_info:
            GitPullUpdater(str(missing_path))

        assert str(missing_path) in str(exc_info.value)


# ---------------------------------------------------------------------------
# Integration: real LifecycleClaudeCliInvoker wired into LifecycleBatchRunner
# ---------------------------------------------------------------------------


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
    def signal_dirty(self) -> None:
        pass


class _StubScheduler:
    def acquire_write_lock(self, key: str, owner_name: str) -> bool:
        return True

    def release_write_lock(self, key: str, owner_name: str) -> None:
        pass


class TestLifecycleBatchRunnerRealInvokerIntegration:
    def test_real_invoker_orphan_is_skipped_job_succeeds(self, tmp_path: Path) -> None:
        """Wires the REAL LifecycleClaudeCliInvoker (not a message-hardcoded
        stand-in) into LifecycleBatchRunner.run() for an orphaned alias whose
        clone directory does not exist. The dispatcher is never reached
        because _validate_repo_inputs raises OrphanedRepoError before any
        CliDispatcher is built, so no live Claude CLI call occurs."""
        from code_indexer.global_repos.lifecycle_batch_runner import (
            LifecycleBatchRunner,
        )

        golden_repos_dir = tmp_path
        (golden_repos_dir / "cidx-meta").mkdir(parents=True)
        # Deliberately do NOT create golden_repos_dir / "orphan-repo".

        job_tracker = _StubJobTracker()
        runner = LifecycleBatchRunner(
            golden_repos_dir=golden_repos_dir,
            job_tracker=job_tracker,
            refresh_scheduler=_StubScheduler(),
            debouncer=_StubDebouncer(),
            claude_cli_invoker=LifecycleClaudeCliInvoker(),
            concurrency=1,
        )

        failed = runner.run(["orphan-repo"], parent_job_id="job-1338-real")

        assert failed == {}, (
            f"Real invoker's orphan must be skipped, not recorded as a "
            f"failure. Got failed={failed}"
        )
        assert job_tracker.complete_calls
        assert not job_tracker.fail_calls


# ---------------------------------------------------------------------------
# Integration: real GitPullUpdater wired into RefreshScheduler._execute_refresh
# ---------------------------------------------------------------------------


class TestRefreshSchedulerRealUpdaterIntegration:
    def test_real_updater_orphan_is_skipped_job_succeeds(self, tmp_path: Path) -> None:
        """Wires the REAL GitPullUpdater (not mocked) into
        RefreshScheduler._execute_refresh() for an orphaned golden repo whose
        clone directory does not exist on disk."""
        from unittest.mock import MagicMock, patch

        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
        from code_indexer.global_repos.query_tracker import QueryTracker
        from code_indexer.global_repos.cleanup_manager import CleanupManager

        golden_repos_dir = tmp_path / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = []
        mock_registry.get_global_repo.return_value = None

        mock_config_source = MagicMock()
        mock_config_source.get_global_refresh_interval.return_value = 3600

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=mock_config_source,
            query_tracker=MagicMock(spec=QueryTracker),
            cleanup_manager=MagicMock(spec=CleanupManager),
            registry=mock_registry,
        )

        alias_name = "orphan-lib-global"
        repo_name = "orphan-lib"
        master_path = golden_repos_dir / repo_name
        assert not master_path.exists()

        repo_info = {
            "alias_name": alias_name,
            "repo_url": "https://github.com/example-org/orphan-lib.git",
            "default_branch": "main",
            "enable_temporal": False,
            "enable_scip": False,
        }
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(master_path))

        mock_job_tracker = MagicMock()
        scheduler._job_tracker = mock_job_tracker

        with patch.object(scheduler, "_detect_existing_indexes", return_value={}):
            with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                result = scheduler._execute_refresh(alias_name)

        assert result["success"] is True
        mock_job_tracker.complete_job.assert_called_once()
        mock_job_tracker.fail_job.assert_not_called()


# ---------------------------------------------------------------------------
# Message-drift proof: skip sites must catch by TYPE, never by message text.
#
# These raise OrphanedRepoError with wording that does NOT contain the old
# string markers ("repo_path does not exist for alias" / "Repository path
# does not exist"). Under the pre-#1338 string-matching implementation these
# would FAIL (treated as a genuine failure instead of a skip). They must pass
# once the skip sites catch `except OrphanedRepoError` by type.
# ---------------------------------------------------------------------------


class TestBatchRunnerCatchesByTypeNotMessage:
    def test_reworded_orphan_message_still_skipped(self, tmp_path: Path) -> None:
        from code_indexer.global_repos.lifecycle_batch_runner import (
            LifecycleBatchRunner,
        )

        golden_repos_dir = tmp_path
        (golden_repos_dir / "cidx-meta").mkdir(parents=True)

        def _reworded_orphan_invoker(
            alias: str, repo_path: Path, **_kwargs: object
        ) -> Any:
            raise OrphanedRepoError(
                f"totally reworded orphan message for {alias} at {repo_path}"
            )

        job_tracker = _StubJobTracker()
        runner = LifecycleBatchRunner(
            golden_repos_dir=golden_repos_dir,
            job_tracker=job_tracker,
            refresh_scheduler=_StubScheduler(),
            debouncer=_StubDebouncer(),
            claude_cli_invoker=_reworded_orphan_invoker,
            concurrency=1,
        )

        failed = runner.run(["orphan-repo"], parent_job_id="job-1338-reword")

        assert failed == {}, (
            "Skip site must catch OrphanedRepoError by TYPE, not by message "
            f"substring. Got failed={failed}"
        )
        assert job_tracker.complete_calls
        assert not job_tracker.fail_calls


class TestSchedulerCatchesByTypeNotMessage:
    def test_reworded_orphan_message_still_skipped(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
        from code_indexer.global_repos.query_tracker import QueryTracker
        from code_indexer.global_repos.cleanup_manager import CleanupManager

        golden_repos_dir = tmp_path / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = []
        mock_registry.get_global_repo.return_value = None

        mock_config_source = MagicMock()
        mock_config_source.get_global_refresh_interval.return_value = 3600

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=mock_config_source,
            query_tracker=MagicMock(spec=QueryTracker),
            cleanup_manager=MagicMock(spec=CleanupManager),
            registry=mock_registry,
        )

        alias_name = "reworded-lib-global"
        repo_name = "reworded-lib"
        master_path = golden_repos_dir / repo_name

        repo_info = {
            "alias_name": alias_name,
            "repo_url": "https://github.com/example-org/reworded-lib.git",
            "default_branch": "main",
            "enable_temporal": False,
            "enable_scip": False,
        }
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(master_path))

        mock_job_tracker = MagicMock()
        scheduler._job_tracker = mock_job_tracker

        def _raise_reworded(*_args: object, **_kwargs: object) -> None:
            raise OrphanedRepoError("totally reworded orphan message")

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
            side_effect=_raise_reworded,
        ):
            with patch.object(scheduler, "_detect_existing_indexes", return_value={}):
                with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                    result = scheduler._execute_refresh(alias_name)

        assert result["success"] is True, (
            "Skip site must catch OrphanedRepoError by TYPE, not by message "
            f"substring. Got result={result}"
        )
        mock_job_tracker.complete_job.assert_called_once()
        mock_job_tracker.fail_job.assert_not_called()
