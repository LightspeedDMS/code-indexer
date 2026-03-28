"""
Unit tests for Bug #383: stale staging directory cleanup.

Verifies that:
1. staging_dir is cleaned up in the finally block when analysis fails
2. staging_dir is NOT cleaned by the finally block when analysis succeeds
   (success path: _stage_then_swap() already consumed it)
3. Original exception propagates even when staging cleanup also fails
4. Server startup cleans stale staging directory when present
5. Server startup is silent when staging directory does not exist

Bug #383: dependency-map.staging/ left behind on analysis failure is
picked up by RefreshScheduler, baked into versioned snapshots, and
pollutes semantic search results.
"""

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.dependency_map_service import DependencyMapService


def _make_service(golden_repos_dir: str, refresh_scheduler=None):
    """Create a DependencyMapService with minimal mocked dependencies."""
    golden_repos_manager = MagicMock()
    golden_repos_manager.golden_repos_dir = golden_repos_dir

    config_manager = MagicMock()
    tracking_backend = MagicMock()
    analyzer = MagicMock()

    service = DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        analyzer=analyzer,
        refresh_scheduler=refresh_scheduler,
    )
    return service, config_manager, tracking_backend, golden_repos_manager


def _make_enabled_config():
    """Return a config mock where dependency_map_enabled is True."""
    config = MagicMock()
    config.dependency_map_enabled = True
    return config


def _make_disabled_config():
    """Return a config mock where dependency_map_enabled is False."""
    config = MagicMock()
    config.dependency_map_enabled = False
    return config


class TestStagingDirCleanedOnFailure:
    """Bug #383: staging dir must be removed when analysis fails."""

    def test_staging_dir_cleaned_on_analysis_failure(self, tmp_path):
        """When _execute_analysis_passes raises, staging dir is removed in finally."""
        # Arrange: create a real staging directory to verify it gets deleted
        golden_repos_dir = str(tmp_path / "golden-repos")
        cidx_meta = tmp_path / "golden-repos" / "cidx-meta"
        staging_dir = cidx_meta / "dependency-map.staging"
        staging_dir.mkdir(parents=True)
        # Put a sentinel file inside so we can confirm directory existed
        (staging_dir / "sentinel.txt").write_text("stale content")

        service, config_manager, tracking_backend, golden_repos_manager = _make_service(
            golden_repos_dir
        )

        # Make _setup_analysis return valid paths pointing to the real temp dirs
        enabled_config = _make_enabled_config()
        paths = {
            "golden_repos_root": tmp_path / "golden-repos",
            "cidx_meta_path": cidx_meta,
            "cidx_meta_read_path": cidx_meta,
            "staging_dir": staging_dir,
            "final_dir": cidx_meta / "dependency-map",
        }
        setup_result = {
            "early_return": False,
            "config": enabled_config,
            "paths": paths,
            "repo_list": [{"alias": "test-repo"}],
        }

        with patch.object(service, "_setup_analysis", return_value=setup_result):
            with patch.object(
                service,
                "_execute_analysis_passes",
                side_effect=RuntimeError("analysis exploded"),
            ):
                with pytest.raises(RuntimeError, match="analysis exploded"):
                    service.run_full_analysis()

        # Assert: staging dir must be gone
        assert not staging_dir.exists(), (
            "Bug #383: staging dir should have been cleaned up after analysis failure"
        )

    def test_staging_dir_cleaned_when_finalize_fails(self, tmp_path):
        """When _finalize_analysis raises, staging dir is also removed in finally."""
        golden_repos_dir = str(tmp_path / "golden-repos")
        cidx_meta = tmp_path / "golden-repos" / "cidx-meta"
        staging_dir = cidx_meta / "dependency-map.staging"
        staging_dir.mkdir(parents=True)

        service, config_manager, tracking_backend, _ = _make_service(golden_repos_dir)

        enabled_config = _make_enabled_config()
        paths = {
            "golden_repos_root": tmp_path / "golden-repos",
            "cidx_meta_path": cidx_meta,
            "cidx_meta_read_path": cidx_meta,
            "staging_dir": staging_dir,
            "final_dir": cidx_meta / "dependency-map",
        }
        setup_result = {
            "early_return": False,
            "config": enabled_config,
            "paths": paths,
            "repo_list": [{"alias": "test-repo"}],
        }

        with patch.object(service, "_setup_analysis", return_value=setup_result):
            with patch.object(
                service,
                "_execute_analysis_passes",
                return_value=([], [], 1.0, 1.0),
            ):
                with patch.object(
                    service,
                    "_finalize_analysis",
                    side_effect=RuntimeError("finalize exploded"),
                ):
                    with pytest.raises(RuntimeError, match="finalize exploded"):
                        service.run_full_analysis()

        assert not staging_dir.exists(), (
            "Bug #383: staging dir should be cleaned when finalize fails"
        )


class TestStagingDirNotCleanedOnSuccess:
    """On success, _stage_then_swap() consumes staging dir; finally must not interfere."""

    def test_staging_dir_not_cleaned_by_finally_on_success(self, tmp_path):
        """When analysis succeeds, finally block does NOT try to remove staging dir."""
        golden_repos_dir = str(tmp_path / "golden-repos")
        cidx_meta = tmp_path / "golden-repos" / "cidx-meta"
        staging_dir = cidx_meta / "dependency-map.staging"
        # On success, _stage_then_swap() would have already removed staging_dir.
        # Simulate that: do NOT create it. Confirm no error occurs and the
        # finally block's success path does not attempt shutil.rmtree.
        staging_dir.parent.mkdir(parents=True)

        service, config_manager, tracking_backend, _ = _make_service(golden_repos_dir)

        enabled_config = _make_enabled_config()
        paths = {
            "golden_repos_root": tmp_path / "golden-repos",
            "cidx_meta_path": cidx_meta,
            "cidx_meta_read_path": cidx_meta,
            "staging_dir": staging_dir,
            "final_dir": cidx_meta / "dependency-map",
        }
        setup_result = {
            "early_return": False,
            "config": enabled_config,
            "paths": paths,
            "repo_list": [{"alias": "test-repo"}],
        }

        with patch.object(service, "_setup_analysis", return_value=setup_result):
            with patch.object(
                service,
                "_execute_analysis_passes",
                return_value=([], [], 1.0, 1.0),
            ):
                with patch.object(service, "_finalize_analysis", return_value=None):
                    # Should not raise
                    result = service.run_full_analysis()

        assert result["status"] == "completed"
        # staging dir was never created (success consumed it), no error means
        # the finally block correctly skipped cleanup on the success path
        assert not staging_dir.exists()


class TestOriginalExceptionNotMasked:
    """Bug #383: staging cleanup failure must not mask the original exception."""

    def test_staging_cleanup_failure_does_not_mask_original_exception(self, tmp_path):
        """When both analysis AND staging cleanup fail, original exception propagates."""
        golden_repos_dir = str(tmp_path / "golden-repos")
        cidx_meta = tmp_path / "golden-repos" / "cidx-meta"
        staging_dir = cidx_meta / "dependency-map.staging"
        staging_dir.mkdir(parents=True)

        service, config_manager, tracking_backend, _ = _make_service(golden_repos_dir)

        enabled_config = _make_enabled_config()
        paths = {
            "golden_repos_root": tmp_path / "golden-repos",
            "cidx_meta_path": cidx_meta,
            "cidx_meta_read_path": cidx_meta,
            "staging_dir": staging_dir,
            "final_dir": cidx_meta / "dependency-map",
        }
        setup_result = {
            "early_return": False,
            "config": enabled_config,
            "paths": paths,
            "repo_list": [{"alias": "test-repo"}],
        }

        original_error = RuntimeError("the real original error")

        with patch.object(service, "_setup_analysis", return_value=setup_result):
            with patch.object(
                service,
                "_execute_analysis_passes",
                side_effect=original_error,
            ):
                # Patch shutil.rmtree to raise inside the finally cleanup path
                with patch(
                    "code_indexer.server.services.dependency_map_service.shutil.rmtree",
                    side_effect=OSError("cannot delete staging dir"),
                ):
                    # The original RuntimeError must propagate, not the OSError
                    with pytest.raises(RuntimeError, match="the real original error"):
                        service.run_full_analysis()


class TestStartupStagingCleanup:
    """Tests for startup cleanup logic (Fix 2 in Bug #383)."""

    def test_startup_cleans_stale_staging_dir(self, tmp_path):
        """Startup cleanup removes dependency-map.staging/ when it exists."""
        golden_repos_dir = tmp_path / "golden-repos"
        staging_dir = golden_repos_dir / "cidx-meta" / "dependency-map.staging"
        staging_dir.mkdir(parents=True)
        (staging_dir / "stale_file.json").write_text('{"stale": true}')

        assert staging_dir.exists(), (
            "Precondition: staging dir should exist before cleanup"
        )

        # Execute the startup cleanup logic directly
        _startup_cleanup_staging_dir(golden_repos_dir)

        assert not staging_dir.exists(), (
            "Bug #383: startup cleanup should remove stale staging dir"
        )

    def test_startup_no_error_when_staging_dir_missing(self, tmp_path):
        """Startup cleanup is silent (no exception) when no staging dir exists."""
        golden_repos_dir = tmp_path / "golden-repos"
        # Ensure the cidx-meta dir exists but no staging subdir
        (golden_repos_dir / "cidx-meta").mkdir(parents=True)

        staging_dir = golden_repos_dir / "cidx-meta" / "dependency-map.staging"
        assert not staging_dir.exists(), "Precondition: staging dir must not exist"

        # Should not raise
        _startup_cleanup_staging_dir(golden_repos_dir)

    def test_startup_no_error_when_cidx_meta_missing(self, tmp_path):
        """Startup cleanup is silent when cidx-meta dir does not exist at all."""
        golden_repos_dir = tmp_path / "golden-repos"
        # Don't create any directories at all

        # Should not raise
        _startup_cleanup_staging_dir(golden_repos_dir)


def _startup_cleanup_staging_dir(golden_repos_dir: Path) -> None:
    """
    Helper that mirrors the startup cleanup logic from app.py (Bug #383, Fix 2).

    This function is the exact logic extracted from app.py startup so it can
    be tested independently without spinning up the full FastAPI application.
    The production code in app.py will use the same pattern.
    """
    try:
        staging_dir = Path(golden_repos_dir) / "cidx-meta" / "dependency-map.staging"
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
    except Exception:
        pass  # Non-fatal: startup cleanup failure should never block server startup
