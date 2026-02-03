"""Pytest fixtures for multimodal E2E tests - Story #66 AC1."""

import shutil

import pytest
from pathlib import Path


def _clean_submodule_artifacts(repo_path: Path) -> None:
    """Remove leftover .code-indexer directories from the submodule.

    This ensures test idempotency by cleaning artifacts from previous runs.

    Args:
        repo_path: Path to the multimodal-mock-repo submodule
    """
    # Remove any .code-indexer directories that might exist
    for code_indexer_dir in repo_path.rglob(".code-indexer"):
        if code_indexer_dir.is_dir():
            shutil.rmtree(code_indexer_dir)

    # Also remove .code-indexer-override.yaml if present
    override_file = repo_path / ".code-indexer-override.yaml"
    if override_file.exists():
        override_file.unlink()


@pytest.fixture(scope="session", autouse=True)
def clean_submodule_before_tests():
    """Session-scoped fixture to clean submodule artifacts before any tests run.

    This runs automatically once at the start of the test session to ensure
    a clean state for all E2E multimodal tests.
    """
    repo_path = (
        Path(__file__).parent.parent.parent.parent
        / "test-fixtures"
        / "multimodal-mock-repo"
    )
    if repo_path.exists():
        _clean_submodule_artifacts(repo_path)
    yield
    # Optional: Clean up after tests complete
    if repo_path.exists():
        _clean_submodule_artifacts(repo_path)


@pytest.fixture
def multimodal_repo_path():
    """Return path to test-fixtures/multimodal-mock-repo.

    Returns:
        Path: Absolute path to the multimodal test fixture repository

    Raises:
        AssertionError: If the test fixture submodule is not initialized
    """
    # Navigate from tests/e2e/multimodal back to project root
    repo_path = (
        Path(__file__).parent.parent.parent.parent
        / "test-fixtures"
        / "multimodal-mock-repo"
    )
    assert repo_path.exists(), (
        "Test fixture submodule not initialized. "
        "Run: git submodule update --init --recursive"
    )
    return repo_path


@pytest.fixture
def temp_index_dir(tmp_path):
    """Create temporary .code-indexer directory for test isolation.

    Args:
        tmp_path: pytest built-in fixture providing temporary directory

    Returns:
        Path: Temporary .code-indexer directory for isolated testing
    """
    index_dir = tmp_path / ".code-indexer"
    index_dir.mkdir(parents=True, exist_ok=True)
    return index_dir
