"""
Unit tests for Story #270: Local Repo Indexing Lifecycle.

GoldenRepoManager.register_local_repo() initialises the CIDX index structure
in-process (the `cidx init --no-override-file` result) when .code-indexer/
does not exist.

Acceptance criteria tested:
- AC1: Langfuse repos get initialized at registration
- AC5: init is idempotent (no error on re-registration / existing .code-indexer/)
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.repositories.background_jobs import BackgroundJobManager


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


def _register(manager: GoldenRepoManager, folder: Path) -> bool:
    return manager.register_local_repo(
        alias="test-repo", folder_path=folder, fire_lifecycle_hooks=False
    )


class TestRegisterLocalRepoCidxInit:
    def test_register_local_repo_initializes_when_no_code_indexer_dir(
        self, golden_repo_manager, test_folder
    ):
        """AC1: registration writes the init layout, without an override file."""
        _register(golden_repo_manager, test_folder)

        config = json.loads((test_folder / ".code-indexer" / "config.json").read_text())
        assert config["codebase_dir"] == str(test_folder.resolve())
        assert (test_folder / ".code-indexer" / "language-mappings.yaml").is_file()
        assert not (test_folder / ".code-indexer-override.yaml").exists()

    def test_register_local_repo_skips_init_when_code_indexer_exists(
        self, golden_repo_manager, test_folder_with_cidx_index
    ):
        """AC5: an existing .code-indexer/ is left alone."""
        _register(golden_repo_manager, test_folder_with_cidx_index)

        assert list((test_folder_with_cidx_index / ".code-indexer").iterdir()) == []

    def test_register_local_repo_initializes_after_global_activation(
        self, golden_repo_manager, test_folder
    ):
        """Global activation runs before the index structure exists."""
        seen_at_activation = []

        def track_activation(*args, **kwargs):
            seen_at_activation.append((test_folder / ".code-indexer").exists())

        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as mock_activator_class:
            mock_activator_class.return_value.activate_golden_repo.side_effect = (
                track_activation
            )
            _register(golden_repo_manager, test_folder)

        assert seen_at_activation == [False]
        assert (test_folder / ".code-indexer" / "config.json").is_file()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
    def test_register_local_repo_init_failure_is_non_blocking(
        self, golden_repo_manager, test_folder
    ):
        """A failing init is logged; registration still succeeds."""
        test_folder.chmod(0o555)
        try:
            result = _register(golden_repo_manager, test_folder)
        finally:
            test_folder.chmod(0o755)

        assert result is True
        assert "test-repo" in golden_repo_manager.golden_repos
        assert not (test_folder / ".code-indexer").exists()

    def test_register_local_repo_duplicate_does_not_init_again(
        self, golden_repo_manager, test_folder
    ):
        """AC5: a duplicate registration returns False and does not re-init."""
        assert _register(golden_repo_manager, test_folder) is True
        config_path = test_folder / ".code-indexer" / "config.json"
        config_path.write_text("{}")

        assert _register(golden_repo_manager, test_folder) is False
        assert config_path.read_text() == "{}"
