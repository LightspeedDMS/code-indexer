"""Tests for _resolve_git_repo_path() input validation (Bug #432)."""

from unittest.mock import patch

from code_indexer.server.mcp.handlers import _resolve_git_repo_path


def test_list_repository_alias_returns_error() -> None:
    """Bug #432: list input must return a clear error, not AttributeError."""
    path, error = _resolve_git_repo_path(["repo1", "repo2"], "testuser")  # type: ignore[arg-type]
    assert path is None
    assert error is not None
    assert "repository_alias must be a string" in error


def test_none_repository_alias_returns_error() -> None:
    """Bug #432: None input must return a clear error, not AttributeError."""
    path, error = _resolve_git_repo_path(None, "testuser")  # type: ignore[arg-type]
    assert path is None
    assert error is not None
    assert "repository_alias must be a string" in error


def test_int_repository_alias_returns_error() -> None:
    """Bug #432: int input must return a clear error, not AttributeError."""
    path, error = _resolve_git_repo_path(123, "testuser")  # type: ignore[arg-type]
    assert path is None
    assert error is not None
    assert "repository_alias must be a string" in error


def test_string_repository_alias_proceeds() -> None:
    """Bug #432: valid string input must pass type validation (may fail later for other reasons)."""
    mock_repo_entry = None
    with (
        patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value="/nonexistent/path",
        ),
        patch(
            "code_indexer.server.mcp.handlers._get_global_repo",
            return_value=mock_repo_entry,
        ),
        patch(
            "code_indexer.server.mcp.handlers._resolve_repo_path",
            return_value=None,
        ),
    ):
        path, error = _resolve_git_repo_path("my-repo-global", "testuser")

    # Must NOT be the type-validation error
    if error is not None:
        assert "repository_alias must be a string" not in error
