"""
Tests for app.state self-monitoring configuration (Bug #87 fix).

Validates that app.py startup stores repo_root and github_repo in app.state
for manual trigger route access, preventing re-detection issues on production servers.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch


def test_app_startup_stores_repo_root_in_state(web_client):
    """Test that app startup stores repo_root in app.state when self-monitoring enabled."""
    # Access the app instance through the test client
    app = web_client.app

    # Verify repo_root is stored in app.state
    assert hasattr(app.state, "self_monitoring_repo_root"), \
        "app.state.self_monitoring_repo_root should be set during startup"

    # If self-monitoring is enabled, repo_root should be a valid path
    if getattr(app.state, "self_monitoring_service", None) is not None:
        assert app.state.self_monitoring_repo_root is not None, \
            "repo_root should not be None when self-monitoring is enabled"
        assert isinstance(app.state.self_monitoring_repo_root, str), \
            "repo_root should be stored as string"
        repo_root = Path(app.state.self_monitoring_repo_root)
        assert repo_root.exists(), f"repo_root path should exist: {repo_root}"
        assert (repo_root / ".git").exists(), f"repo_root should be git repo: {repo_root}"
    # If disabled, should be None
    else:
        assert app.state.self_monitoring_repo_root is None, \
            "repo_root should be None when self-monitoring is disabled"


def test_app_startup_stores_github_repo_in_state(web_client):
    """Test that app startup stores github_repo in app.state when self-monitoring enabled."""
    # Access the app instance through the test client
    app = web_client.app

    # Verify github_repo is stored in app.state
    assert hasattr(app.state, "self_monitoring_github_repo"), \
        "app.state.self_monitoring_github_repo should be set during startup"

    # If self-monitoring is enabled and github_repo detected, validate format
    if getattr(app.state, "self_monitoring_service", None) is not None:
        if app.state.self_monitoring_github_repo is not None:
            assert isinstance(app.state.self_monitoring_github_repo, str), \
                "github_repo should be stored as string"
            assert "/" in app.state.self_monitoring_github_repo, \
                "github_repo should be in 'owner/repo' format"
    # If disabled, should be None
    else:
        assert app.state.self_monitoring_github_repo is None, \
            "github_repo should be None when self-monitoring is disabled"


def test_detect_repo_root_uses_env_var_first():
    """
    Test _detect_repo_root() checks CIDX_REPO_ROOT environment variable first.

    This is the most reliable detection method for production deployments where
    the systemd service explicitly sets the repo root path.

    Bug Fix: MONITOR-GENERAL-011
    """
    from code_indexer.server.app import _detect_repo_root
    import os

    # Create a temporary directory structure simulating a git repo
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "test-repo"
        repo_dir.mkdir()
        git_dir = repo_dir / ".git"
        git_dir.mkdir()

        # Test: When CIDX_REPO_ROOT is set, should use it regardless of __file__ or cwd
        with patch.dict(os.environ, {"CIDX_REPO_ROOT": str(repo_dir)}):
            result = _detect_repo_root(start_from_file=True)

            assert result is not None, \
                "Should detect repo_root from CIDX_REPO_ROOT env var"
            assert result == repo_dir, \
                f"Should detect {repo_dir} from env var, got {result}"
            assert (result / ".git").exists(), \
                "Detected repo_root should have .git directory"


def test_detect_repo_root_falls_back_to_cwd():
    """
    Test _detect_repo_root() uses cwd as fallback when __file__ detection fails.

    This simulates pip-installed packages where __file__ points to site-packages
    but the systemd service runs from the cloned repo directory (cwd has .git).

    Bug: MONITOR-GENERAL-011
    """
    from code_indexer.server.app import _detect_repo_root

    # Create a temporary directory structure simulating a git repo
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "test-repo"
        repo_dir.mkdir()
        git_dir = repo_dir / ".git"
        git_dir.mkdir()

        # Test: When cwd is the repo directory, should detect from cwd
        # (start_from_file=False simulates __file__ detection failure)
        with patch("code_indexer.server.app.Path.cwd", return_value=repo_dir):
            result = _detect_repo_root(start_from_file=False)

            assert result is not None, \
                "Should detect repo_root from cwd when __file__ detection skipped"
            assert result == repo_dir, \
                f"Should detect {repo_dir}, got {result}"
            assert (result / ".git").exists(), \
                "Detected repo_root should have .git directory"
