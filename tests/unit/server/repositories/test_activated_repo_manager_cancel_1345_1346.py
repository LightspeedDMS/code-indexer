"""
Unit tests for Bug #1345 and Bug #1346 (follow-ups to the #1342 activation-
cancel fix shipped in v11.36.0).

Bug #1345: a cancel raised during the CoW CLONE phase (before the
branch-delta reindex starts) must not leave an orphaned, unregistered
partial activated-repo clone directory on disk. The existing #1203
"Removing orphaned clone" cleanup only ran on the reindex-phase failure
path inside `_do_activate_repository`; the clone-phase path relied solely
on `_clone_with_copy_on_write`'s own internal `os.path.exists(dest_path)`
check, which can miss a partial directory that becomes visible a beat
later (e.g. an NFS-backed CoW Storage Daemon clone). `_do_activate_repository`
now performs a SECOND, defense-in-depth cleanup attempt for the clone
phase, mirroring the existing reindex-phase cleanup exactly.

Bug #1346: a user-initiated cancel (`SubprocessCancelledError`) must log at
INFO/WARNING, never ERROR, in both `_run_branch_delta_index` and
`_do_activate_repository`'s exception handlers. Genuine (non-cancel)
failures must continue to log ERROR.

Mocking policy (anti-mock): only the clone_backend and index_manager seams
are stubbed (same seam `test_activated_repo_manager_cancel_1342.py` uses).
The method under test, `_do_activate_repository` / `_run_branch_delta_index`,
is never mocked -- it runs for real, including its real exception handling
and real filesystem operations against a real temp directory.
"""

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
    ActivatedRepoManager,
)
from src.code_indexer.server.repositories.golden_repo_manager import GoldenRepo

# NOTE: imported WITHOUT the "src." prefix, matching the absolute import
# `from code_indexer.server.utils.cancellable_subprocess import
# SubprocessCancelledError` used inside activated_repo_manager.py itself.
# Under PYTHONPATH=./src, "src.code_indexer...." and "code_indexer...." are
# two distinct entries in sys.modules with two distinct class objects of
# the same name -- importing via "src." here would make isinstance() checks
# in the production code (which always resolves the plain, non-"src."
# absolute import) silently fail to recognize this test's exception
# instances as cancellations.
from code_indexer.server.utils.cancellable_subprocess import (
    SubprocessCancelledError,
)
from src.code_indexer.server.utils.config_manager import ServerResourceConfig


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def golden_repo_manager_mock():
    mock = MagicMock()
    golden_repo = GoldenRepo(
        alias="test-repo",
        repo_url="https://github.com/example/test-repo.git",
        default_branch="main",
        clone_path="/path/to/golden/test-repo",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    golden_repos_dict = {"test-repo": golden_repo}
    mock.golden_repos = golden_repos_dict
    mock.get_golden_repo.side_effect = lambda alias: golden_repos_dict.get(alias)
    mock.get_actual_repo_path.return_value = "/path/to/golden/test-repo"
    mock.resource_config = ServerResourceConfig()
    return mock


@pytest.fixture
def background_job_manager_mock():
    mock = MagicMock()
    mock.submit_job.return_value = "job-123"
    return mock


@pytest.fixture
def mock_clone_backend():
    backend = MagicMock()
    backend.create_clone_at_path.return_value = "/dest/path"
    return backend


@pytest.fixture
def mock_index_manager():
    return MagicMock()


@pytest.fixture
def activated_repo_manager(
    temp_data_dir,
    golden_repo_manager_mock,
    background_job_manager_mock,
    mock_clone_backend,
    mock_index_manager,
):
    return ActivatedRepoManager(
        data_dir=temp_data_dir,
        golden_repo_manager=golden_repo_manager_mock,
        background_job_manager=background_job_manager_mock,
        clone_backend=mock_clone_backend,
        index_manager=mock_index_manager,
    )


def _patch_committer():
    return patch(
        "src.code_indexer.server.repositories.activated_repo_manager"
        ".CommitterResolutionService"
    )


# ---------------------------------------------------------------------------
# Bug #1345 — clone-phase orphan-clone cleanup
# ---------------------------------------------------------------------------


class TestClonePhaseOrphanCleanup1345:
    def test_clone_phase_cancel_cleans_up_orphan_missed_by_inner_check(
        self, activated_repo_manager, mock_clone_backend
    ):
        """A clone-phase cancellation whose partial directory is NOT yet
        visible to _clone_with_copy_on_write's own os.path.exists() check
        (simulating an NFS-visibility race) must still be cleaned up by a
        second, defense-in-depth check in _do_activate_repository."""
        activated_repo_path = os.path.join(
            activated_repo_manager.activated_repos_dir,
            "testuser",
            "my-activated-repo",
        )

        def _fake_clone(source_path, dest_path, **kwargs):
            # Simulate the clone backend materializing partial content on
            # disk right as it raises the cancellation error.
            os.makedirs(dest_path, exist_ok=True)
            (Path(dest_path) / "partial-file.txt").write_text("partial")
            raise SubprocessCancelledError("clone cancelled")

        mock_clone_backend.create_clone_at_path.side_effect = _fake_clone

        real_exists = os.path.exists
        call_count = {"n": 0}

        def _flaky_exists(path):
            if str(path) == activated_repo_path:
                call_count["n"] += 1
                if call_count["n"] == 1:
                    # First check (inner _clone_with_copy_on_write cleanup)
                    # misses the not-yet-visible directory.
                    return False
            return real_exists(path)

        with (
            patch(
                "src.code_indexer.server.repositories.activated_repo_manager"
                ".os.path.exists",
                side_effect=_flaky_exists,
            ),
            _patch_committer(),
        ):
            with pytest.raises(ActivatedRepoError):
                activated_repo_manager._do_activate_repository(
                    username="testuser",
                    golden_repo_alias="test-repo",
                    branch_name="main",  # default branch: isolates clone phase
                    user_alias="my-activated-repo",
                    cancel_check=lambda: True,
                )

        assert call_count["n"] >= 2, (
            "expected a second (outer, _do_activate_repository-level) "
            "existence check after the inner one missed the directory"
        )
        assert not real_exists(activated_repo_path), (
            "orphaned clone directory must be removed even when the FIRST "
            "(inner) existence check misses it"
        )


class TestReindexPhaseOrphanCleanupRegression1345:
    def test_reindex_phase_cancel_still_cleans_up_orphan_clone(
        self, activated_repo_manager, mock_clone_backend, mock_index_manager
    ):
        """Regression guard: a reindex-phase cancellation must still clean
        up the orphaned clone directory (pre-existing #1203 behavior)."""
        activated_repo_path = os.path.join(
            activated_repo_manager.activated_repos_dir,
            "testuser",
            "my-activated-repo",
        )

        def _fake_clone(source_path, dest_path, **kwargs):
            os.makedirs(dest_path, exist_ok=True)
            return dest_path

        mock_clone_backend.create_clone_at_path.side_effect = _fake_clone
        mock_index_manager.run_branch_delta_index.side_effect = (
            SubprocessCancelledError("reindex cancelled")
        )
        successful_git = MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(
                "src.code_indexer.server.repositories.activated_repo_manager"
                ".subprocess.run",
                return_value=successful_git,
            ),
            _patch_committer() as mock_committer_cls,
        ):
            mock_committer_cls.return_value.resolve_committer_email.return_value = (
                "",
                None,
            )
            with pytest.raises(ActivatedRepoError):
                activated_repo_manager._do_activate_repository(
                    username="testuser",
                    golden_repo_alias="test-repo",
                    branch_name="feature-branch",  # non-default -> triggers reindex
                    user_alias="my-activated-repo",
                    cancel_check=lambda: True,
                )

        assert not os.path.exists(activated_repo_path), (
            "reindex-phase cancel must still remove the orphaned clone dir"
        )


# ---------------------------------------------------------------------------
# Bug #1346 — cancellation must log INFO/WARNING, never ERROR
# ---------------------------------------------------------------------------


class TestRunBranchDeltaIndexCancelLogging1346:
    def test_cancellation_logs_info_not_error(
        self, activated_repo_manager, mock_index_manager, caplog
    ):
        mock_index_manager.run_branch_delta_index.side_effect = (
            SubprocessCancelledError("reindex cancelled by user")
        )

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ActivatedRepoError):
                activated_repo_manager._run_branch_delta_index(
                    "/repo/path", "my-alias", cancel_check=lambda: True
                )

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not error_records, (
            f"expected no ERROR logs for a user cancel, got: {error_records}"
        )
        cancel_records = [
            r
            for r in caplog.records
            if r.levelno in (logging.INFO, logging.WARNING)
            and "cancel" in r.getMessage().lower()
        ]
        assert cancel_records, (
            "expected an INFO/WARNING 'cancelled' framed log for the user cancel"
        )

    def test_genuine_failure_still_logs_error(
        self, activated_repo_manager, mock_index_manager, caplog
    ):
        mock_index_manager.run_branch_delta_index.side_effect = RuntimeError(
            "cidx index failed: provider unavailable"
        )

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ActivatedRepoError):
                activated_repo_manager._run_branch_delta_index(
                    "/repo/path", "my-alias", cancel_check=lambda: False
                )

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, "expected an ERROR log for a genuine reindex failure"


class TestDoActivateRepositoryCancelLogging1346:
    def test_clone_phase_cancel_does_not_log_error(
        self, activated_repo_manager, mock_clone_backend, caplog
    ):
        mock_clone_backend.create_clone_at_path.side_effect = SubprocessCancelledError(
            "clone cancelled by user"
        )

        with _patch_committer():
            with caplog.at_level(logging.DEBUG):
                with pytest.raises(ActivatedRepoError):
                    activated_repo_manager._do_activate_repository(
                        username="testuser",
                        golden_repo_alias="test-repo",
                        branch_name="main",
                        user_alias="my-activated-repo",
                        cancel_check=lambda: True,
                    )

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not error_records, (
            f"expected no ERROR logs for a clone-phase user cancel, got: "
            f"{error_records}"
        )

    def test_reindex_phase_cancel_does_not_log_error(
        self, activated_repo_manager, mock_clone_backend, mock_index_manager, caplog
    ):
        def _fake_clone(source_path, dest_path, **kwargs):
            os.makedirs(dest_path, exist_ok=True)
            return dest_path

        mock_clone_backend.create_clone_at_path.side_effect = _fake_clone
        mock_index_manager.run_branch_delta_index.side_effect = (
            SubprocessCancelledError("reindex cancelled by user")
        )
        successful_git = MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(
                "src.code_indexer.server.repositories.activated_repo_manager"
                ".subprocess.run",
                return_value=successful_git,
            ),
            _patch_committer() as mock_committer_cls,
        ):
            mock_committer_cls.return_value.resolve_committer_email.return_value = (
                "",
                None,
            )
            with caplog.at_level(logging.DEBUG):
                with pytest.raises(ActivatedRepoError):
                    activated_repo_manager._do_activate_repository(
                        username="testuser",
                        golden_repo_alias="test-repo",
                        branch_name="feature-branch",
                        user_alias="my-activated-repo",
                        cancel_check=lambda: True,
                    )

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not error_records, (
            f"expected no ERROR logs for a reindex-phase user cancel, got: "
            f"{error_records}"
        )

    def test_genuine_clone_failure_still_logs_error(
        self, activated_repo_manager, mock_clone_backend, caplog
    ):
        mock_clone_backend.create_clone_at_path.side_effect = RuntimeError("disk full")

        with _patch_committer():
            with caplog.at_level(logging.DEBUG):
                with pytest.raises(ActivatedRepoError):
                    activated_repo_manager._do_activate_repository(
                        username="testuser",
                        golden_repo_alias="test-repo",
                        branch_name="main",
                        user_alias="my-activated-repo",
                        cancel_check=lambda: False,
                    )

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, (
            "expected an ERROR log for a genuine (non-cancel) clone failure"
        )
