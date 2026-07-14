"""
Unit tests for cooperative cancellation wiring in ActivatedRepoManager
(Bug #1342).

Cancelling a running activation job used to be a no-op while the worker was
blocked inside the CoW clone or branch-delta reindex steps. These tests
prove the `cancel_check` callable is correctly threaded:

    _do_activate_repository -> _clone_with_copy_on_write -> clone_backend
    _do_activate_repository -> _run_branch_delta_index -> _index_manager

and that a cancellation-triggered exception during the clone step still
hits the EXISTING cleanup path (`shutil.rmtree(dest_path, ...)` in
_clone_with_copy_on_write's except-Exception handler), so no partial clone
directory survives a cancelled activation.

Mocking policy: clone_backend and _index_manager are injected test doubles
(the same seam ActivatedRepoManager already uses for dependency injection in
tests/unit/server/repositories/test_activated_repo_manager.py) — this is
parameter-wiring/cleanup verification, not a "process" mock of the actual
subprocess kill mechanics (those are covered with real subprocesses in
test_cancellable_subprocess_1342.py, test_clone_backend_cancel_1342.py, and
test_activated_repo_index_manager_cancel_1342.py).
"""

import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
    ActivatedRepoManager,
)
from src.code_indexer.server.repositories.golden_repo_manager import GoldenRepo
from src.code_indexer.server.utils.cancellable_subprocess import (
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


class TestCloneWithCopyOnWriteCancelWiring:
    def test_forwards_cancel_check_to_clone_backend(
        self, activated_repo_manager, mock_clone_backend, tmp_path
    ):
        sentinel_cancel_check = MagicMock(return_value=False)
        # Real, empty (non-git, non-cidx) directory: the post-clone
        # git rev-parse/fix-config checks in _clone_with_copy_on_write run
        # real subprocess calls with cwd=dest_path and all no-op cleanly
        # against a plain empty dir.
        dest_path = tmp_path / "dest"
        dest_path.mkdir()

        activated_repo_manager._clone_with_copy_on_write(
            "/src/path", str(dest_path), cancel_check=sentinel_cancel_check
        )

        _, kwargs = mock_clone_backend.create_clone_at_path.call_args
        assert kwargs.get("cancel_check") is sentinel_cancel_check

    def test_cancellation_exception_cleans_up_partial_dest_path(
        self, activated_repo_manager, mock_clone_backend, tmp_path
    ):
        """A cancellation-triggered exception from the clone backend must
        still hit the existing rmtree-on-failure cleanup path -- no
        orphaned partial clone directory survives."""
        dest_path = tmp_path / "activated-repo"
        dest_path.mkdir()
        (dest_path / "partial-file.txt").write_text("partial clone content")
        assert dest_path.exists()

        mock_clone_backend.create_clone_at_path.side_effect = SubprocessCancelledError(
            "clone cancelled"
        )

        with pytest.raises(ActivatedRepoError):
            activated_repo_manager._clone_with_copy_on_write(
                "/src/path", str(dest_path), cancel_check=lambda: True
            )

        assert not dest_path.exists(), (
            "partial clone directory must be removed after a cancelled clone"
        )


class TestRunBranchDeltaIndexCancelWiring:
    def test_forwards_cancel_check_to_index_manager(
        self, activated_repo_manager, mock_index_manager
    ):
        sentinel_cancel_check = MagicMock(return_value=False)

        activated_repo_manager._run_branch_delta_index(
            "/repo/path", "my-alias", cancel_check=sentinel_cancel_check
        )

        _, kwargs = mock_index_manager.run_branch_delta_index.call_args
        assert kwargs.get("cancel_check") is sentinel_cancel_check

    def test_none_index_manager_stays_noop_with_cancel_check_param(
        self,
        temp_data_dir,
        golden_repo_manager_mock,
        background_job_manager_mock,
        mock_clone_backend,
    ):
        """Legacy callers (index_manager=None) must remain a safe no-op even
        when a cancel_check is passed."""
        manager = ActivatedRepoManager(
            data_dir=temp_data_dir,
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
            clone_backend=mock_clone_backend,
            index_manager=None,
        )

        # Must not raise.
        manager._run_branch_delta_index(
            "/repo/path", "my-alias", cancel_check=lambda: True
        )

    def test_global_alias_stays_noop_with_cancel_check_param(
        self, activated_repo_manager, mock_index_manager
    ):
        """-global aliases skip reindex entirely regardless of cancel_check."""
        activated_repo_manager._run_branch_delta_index(
            "/repo/path", "my-alias-global", cancel_check=lambda: True
        )
        mock_index_manager.run_branch_delta_index.assert_not_called()


class TestDoActivateRepositoryCancelWiring:
    def test_forwards_cancel_check_to_clone_step_default_branch(
        self, activated_repo_manager, mock_clone_backend
    ):
        """Default-branch activation: reindex is skipped, only the clone
        step must receive cancel_check."""
        sentinel_cancel_check = MagicMock(return_value=False)

        def _fake_clone(source_path, dest_path, **kwargs):
            # Mimic a real CoW clone leaving a real (non-git, non-cidx)
            # directory on disk, since _do_activate_repository's post-clone
            # git checks run real subprocess calls with cwd=dest_path.
            import os as _os

            _os.makedirs(dest_path, exist_ok=True)
            return dest_path

        mock_clone_backend.create_clone_at_path.side_effect = _fake_clone

        with patch(
            "src.code_indexer.server.repositories.activated_repo_manager"
            ".CommitterResolutionService"
        ) as mock_committer_cls:
            mock_committer_cls.return_value.resolve_committer_email.return_value = (
                "",
                None,
            )
            activated_repo_manager._do_activate_repository(
                username="testuser",
                golden_repo_alias="test-repo",
                branch_name="main",  # matches golden_repo default_branch
                user_alias="my-activated-repo",
                cancel_check=sentinel_cancel_check,
            )

        _, kwargs = mock_clone_backend.create_clone_at_path.call_args
        assert kwargs.get("cancel_check") is sentinel_cancel_check

    def test_forwards_cancel_check_to_reindex_step_non_default_branch(
        self, activated_repo_manager, mock_index_manager
    ):
        """Non-default-branch activation: the branch-delta reindex step
        must also receive cancel_check."""
        sentinel_cancel_check = MagicMock(return_value=False)
        successful_git = MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(
                "src.code_indexer.server.repositories.activated_repo_manager"
                ".subprocess.run",
                return_value=successful_git,
            ),
            patch(
                "src.code_indexer.server.repositories.activated_repo_manager"
                ".CommitterResolutionService"
            ) as mock_committer_cls,
        ):
            mock_committer_cls.return_value.resolve_committer_email.return_value = (
                "",
                None,
            )
            activated_repo_manager._do_activate_repository(
                username="testuser",
                golden_repo_alias="test-repo",
                branch_name="feature-branch",  # differs from default_branch "main"
                user_alias="my-activated-repo",
                cancel_check=sentinel_cancel_check,
            )

        _, kwargs = mock_index_manager.run_branch_delta_index.call_args
        assert kwargs.get("cancel_check") is sentinel_cancel_check
