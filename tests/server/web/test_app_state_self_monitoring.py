"""
Tests for app.state self-monitoring configuration (Bug #87 fix).

Validates that app.py startup stores repo_root and github_repo in app.state
for manual trigger route access, preventing re-detection issues on production servers.
"""

from pathlib import Path


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
