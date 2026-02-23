"""
Unit tests for Story #270: Local Repo Indexing Lifecycle.

Tests the new cidx init call added to GoldenRepoManager.register_local_repo()
when .code-indexer/ directory does not exist.

Acceptance criteria tested:
- AC1: Langfuse repos get initialized at registration
  (register_local_repo creates .code-indexer/ via cidx init)
- AC5: cidx init is idempotent (no error on re-registration / existing .code-indexer/)
"""

import tempfile
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from src.code_indexer.server.repositories.background_jobs import BackgroundJobManager


@pytest.fixture
def temp_data_dir():
    """Create temporary data directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def golden_repo_manager(temp_data_dir):
    """Create GoldenRepoManager instance with temp directory."""
    manager = GoldenRepoManager(data_dir=temp_data_dir)
    mock_bg_manager = MagicMock(spec=BackgroundJobManager)
    mock_bg_manager.submit_job.return_value = "test-job-id-12345"
    manager.background_job_manager = mock_bg_manager
    return manager


@pytest.fixture
def test_folder(temp_data_dir):
    """Create a test folder to register (without .code-indexer/)."""
    folder_path = Path(temp_data_dir) / "test-local-repo"
    folder_path.mkdir(parents=True, exist_ok=True)
    return folder_path


@pytest.fixture
def test_folder_with_cidx_index(test_folder):
    """Create a test folder with existing .code-indexer/ directory."""
    cidx_dir = test_folder / ".code-indexer"
    cidx_dir.mkdir(parents=True, exist_ok=True)
    return test_folder


class TestRegisterLocalRepoCidxInit:
    """
    Tests for the cidx init behavior added to register_local_repo() in Story #270.

    AC1: Langfuse repos get initialized at registration — register_local_repo
    calls cidx init when .code-indexer/ does not exist.
    AC5: cidx init is idempotent — no error on re-registration with existing .code-indexer/.
    """

    def test_register_local_repo_calls_cidx_init_when_no_code_indexer_dir(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that register_local_repo runs 'cidx init' when .code-indexer/ does not exist.

        AC1: Langfuse repos get initialized at registration.
        """
        assert not (test_folder / ".code-indexer").exists(), (
            "Pre-condition: .code-indexer/ must not exist"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            golden_repo_manager.register_local_repo(
                alias="test-repo",
                folder_path=test_folder,
                fire_lifecycle_hooks=False,
            )

        # Verify cidx init was called with correct cwd
        cidx_init_calls = [
            c for c in mock_run.call_args_list
            if c[0][0] == ["cidx", "init"]
        ]
        assert len(cidx_init_calls) == 1, (
            f"Expected exactly one 'cidx init' call, got {len(cidx_init_calls)}. "
            f"All subprocess.run calls: {mock_run.call_args_list}"
        )

        init_call = cidx_init_calls[0]
        assert init_call[1]["cwd"] == str(test_folder), (
            f"Expected cwd={str(test_folder)!r}, got {init_call[1]['cwd']!r}"
        )
        assert init_call[1]["check"] is True
        assert init_call[1]["capture_output"] is True

    def test_register_local_repo_skips_cidx_init_when_code_indexer_exists(
        self, golden_repo_manager, test_folder_with_cidx_index
    ):
        """
        Test that register_local_repo does NOT run 'cidx init' when .code-indexer/ exists.

        AC5: cidx init is idempotent — skip when already initialized.
        """
        assert (test_folder_with_cidx_index / ".code-indexer").exists(), (
            "Pre-condition: .code-indexer/ must already exist"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            golden_repo_manager.register_local_repo(
                alias="test-repo",
                folder_path=test_folder_with_cidx_index,
                fire_lifecycle_hooks=False,
            )

        # Verify cidx init was NOT called
        cidx_init_calls = [
            c for c in mock_run.call_args_list
            if c[0][0] == ["cidx", "init"]
        ]
        assert len(cidx_init_calls) == 0, (
            f"Expected no 'cidx init' call when .code-indexer/ exists, "
            f"got {len(cidx_init_calls)} calls."
        )

    def test_register_local_repo_cidx_init_called_after_global_activation(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that 'cidx init' is called after global activation.

        Per the algorithm, cidx init happens after global activation.
        """
        call_order = []

        def track_subprocess(cmd, *args, **kwargs):
            call_order.append(("subprocess", cmd))
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=track_subprocess):
            with patch(
                "code_indexer.global_repos.global_activation.GlobalActivator"
            ) as mock_activator_class:
                mock_activator = MagicMock()

                def track_activation(*args, **kwargs):
                    call_order.append(("activation",))

                mock_activator.activate_golden_repo.side_effect = track_activation
                mock_activator_class.return_value = mock_activator

                golden_repo_manager.register_local_repo(
                    alias="test-repo",
                    folder_path=test_folder,
                    fire_lifecycle_hooks=False,
                )

        # Find activation and cidx init calls in order
        activation_idx = next(
            (i for i, item in enumerate(call_order) if item[0] == "activation"), None
        )
        cidx_init_idx = next(
            (
                i
                for i, item in enumerate(call_order)
                if item[0] == "subprocess" and item[1] == ["cidx", "init"]
            ),
            None,
        )

        assert cidx_init_idx is not None, "cidx init was not called"
        assert activation_idx is not None, "Global activation was not called"
        assert cidx_init_idx > activation_idx, (
            "cidx init must be called AFTER global activation"
        )

    def test_register_local_repo_cidx_init_failure_is_non_blocking(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that register_local_repo continues even if cidx init fails.

        Per the algorithm: "Continue with registration even if init fails"
        Non-blocking: log error but don't raise.
        """
        assert not (test_folder / ".code-indexer").exists()

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, ["cidx", "init"], stderr="cidx not found"
            )

            # Should NOT raise despite cidx init failure
            result = golden_repo_manager.register_local_repo(
                alias="test-repo",
                folder_path=test_folder,
                fire_lifecycle_hooks=False,
            )

        # Registration should still succeed
        assert result is True
        assert "test-repo" in golden_repo_manager.golden_repos

    def test_register_local_repo_cidx_init_unexpected_exception_is_non_blocking(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that register_local_repo continues if subprocess.run raises an unexpected error.

        Covers the broad 'except Exception' clause in the algorithm.
        """
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = OSError("cidx binary not found")

            # Should NOT raise
            result = golden_repo_manager.register_local_repo(
                alias="test-repo",
                folder_path=test_folder,
                fire_lifecycle_hooks=False,
            )

        assert result is True
        assert "test-repo" in golden_repo_manager.golden_repos

    def test_register_local_repo_cidx_init_uses_check_true(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that cidx init subprocess call uses check=True.

        check=True ensures CalledProcessError is raised on non-zero exit,
        which we then catch and log.
        """
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            golden_repo_manager.register_local_repo(
                alias="test-repo",
                folder_path=test_folder,
                fire_lifecycle_hooks=False,
            )

        cidx_init_calls = [
            c for c in mock_run.call_args_list
            if c[0][0] == ["cidx", "init"]
        ]
        assert len(cidx_init_calls) == 1
        assert cidx_init_calls[0][1].get("check") is True, (
            "cidx init must use check=True to detect failures"
        )

    def test_register_local_repo_duplicate_does_not_call_cidx_init_again(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that duplicate register_local_repo call (idempotent False return)
        does not call cidx init again.

        AC5: Re-registration is idempotent.
        """
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            # First registration
            result1 = golden_repo_manager.register_local_repo(
                alias="test-repo",
                folder_path=test_folder,
                fire_lifecycle_hooks=False,
            )
            assert result1 is True

            first_call_count = len([
                c for c in mock_run.call_args_list
                if c[0][0] == ["cidx", "init"]
            ])

            # Second registration (duplicate - returns False early)
            result2 = golden_repo_manager.register_local_repo(
                alias="test-repo",
                folder_path=test_folder,
                fire_lifecycle_hooks=False,
            )
            assert result2 is False

            second_call_count = len([
                c for c in mock_run.call_args_list
                if c[0][0] == ["cidx", "init"]
            ])

        # cidx init should NOT have been called again on duplicate
        assert second_call_count == first_call_count, (
            "cidx init must not be called again for duplicate registration"
        )
