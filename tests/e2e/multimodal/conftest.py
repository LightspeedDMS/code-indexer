"""Pytest fixtures for multimodal E2E tests - Story #66 AC1."""

import pytest
from pathlib import Path


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
