"""
Tests for rollback-safe _stage_then_swap in DependencyMapService (Bug #250).

Verifies that when staging_dir.rename(final_dir) fails, the service:
1. Rolls back old_dir to final_dir (if old_dir exists)
2. Re-raises the original exception
3. Logs the rollback attempt and its outcome

Uses real filesystem operations (tmp_path) and monkeypatching to inject
rename failures at precise points.
"""

import logging
from pathlib import Path
from unittest.mock import Mock

import pytest


def make_service():
    """Construct a minimal DependencyMapService for testing _stage_then_swap."""
    from code_indexer.server.services.dependency_map_service import DependencyMapService

    return DependencyMapService(
        golden_repos_manager=Mock(),
        config_manager=Mock(),
        tracking_backend=Mock(),
        analyzer=Mock(),
    )


class TestStageThenSwapHappyPath:
    """Normal operation: staging replaces final and old backup is cleaned up."""

    def test_happy_path_staging_becomes_final(self, tmp_path):
        """
        After a successful swap, final_dir contains the staging content
        and old backup is removed.
        """
        service = make_service()

        # Create staging directory with a sentinel file
        staging_dir = tmp_path / "dependency-map.staging"
        staging_dir.mkdir()
        sentinel = staging_dir / "result.md"
        sentinel.write_text("new content")

        # Create existing final directory
        final_dir = tmp_path / "dependency-map"
        final_dir.mkdir()
        (final_dir / "old.md").write_text("old content")

        service._stage_then_swap(staging_dir, final_dir)

        # final_dir must exist and contain new content
        assert final_dir.exists()
        assert (final_dir / "result.md").read_text() == "new content"

        # old.md must be gone (it was in the old final_dir, now swapped out)
        assert not (final_dir / "old.md").exists()

        # staging_dir must be gone
        assert not staging_dir.exists()

        # backup must be cleaned up
        old_dir = tmp_path / "dependency-map.old"
        assert not old_dir.exists()

    def test_happy_path_no_prior_final_dir(self, tmp_path):
        """
        When final_dir does not exist yet (first run), staging simply becomes final.
        No old backup is created.
        """
        service = make_service()

        staging_dir = tmp_path / "dependency-map.staging"
        staging_dir.mkdir()
        (staging_dir / "result.md").write_text("first run")

        final_dir = tmp_path / "dependency-map"
        # final_dir intentionally not created

        service._stage_then_swap(staging_dir, final_dir)

        assert final_dir.exists()
        assert (final_dir / "result.md").read_text() == "first run"
        assert not staging_dir.exists()

        old_dir = tmp_path / "dependency-map.old"
        assert not old_dir.exists()


class TestStageThenSwapRollback:
    """Rollback behaviour when staging rename fails."""

    def test_rollback_restores_final_dir_when_staging_rename_fails(self, tmp_path, monkeypatch):
        """
        When staging_dir.rename(final_dir) raises OSError, old_dir is renamed
        back to final_dir so callers still have the previous state available.
        """
        service = make_service()

        staging_dir = tmp_path / "dependency-map.staging"
        staging_dir.mkdir()
        (staging_dir / "new.md").write_text("new content")

        final_dir = tmp_path / "dependency-map"
        final_dir.mkdir()
        (final_dir / "old.md").write_text("old content")

        old_dir = tmp_path / "dependency-map.old"

        original_rename = Path.rename

        def patched_rename(self, target):
            target = Path(target)
            # Allow final_dir → old_dir rename to succeed
            if self == staging_dir and target == final_dir:
                raise OSError(28, "No space left on device")
            return original_rename(self, target)

        monkeypatch.setattr(Path, "rename", patched_rename)

        with pytest.raises(OSError):
            service._stage_then_swap(staging_dir, final_dir)

        # Rollback: old_dir must have been moved back to final_dir
        assert final_dir.exists(), "final_dir must be restored after rollback"
        assert (final_dir / "old.md").read_text() == "old content"

        # old_dir must no longer exist (it was rolled back)
        assert not old_dir.exists()

    def test_rollback_reraises_original_exception(self, tmp_path, monkeypatch):
        """
        The original OSError (not a rollback exception) must propagate to the caller.
        """
        service = make_service()

        staging_dir = tmp_path / "dependency-map.staging"
        staging_dir.mkdir()

        final_dir = tmp_path / "dependency-map"
        final_dir.mkdir()

        original_rename = Path.rename

        original_error = OSError(28, "No space left on device")

        def patched_rename(self, target):
            target = Path(target)
            if self == staging_dir and target == final_dir:
                raise original_error
            return original_rename(self, target)

        monkeypatch.setattr(Path, "rename", patched_rename)

        with pytest.raises(OSError) as exc_info:
            service._stage_then_swap(staging_dir, final_dir)

        # Must be the exact original error, not a wrapped or different one
        assert exc_info.value is original_error

    def test_no_rollback_when_no_old_dir(self, tmp_path, monkeypatch):
        """
        When final_dir did not exist (first run), there is no old_dir to roll back.
        The method must still re-raise the original exception without attempting rollback.
        """
        service = make_service()

        staging_dir = tmp_path / "dependency-map.staging"
        staging_dir.mkdir()

        final_dir = tmp_path / "dependency-map"
        # final_dir NOT created — simulates first-run scenario

        old_dir = tmp_path / "dependency-map.old"

        original_rename = Path.rename

        def patched_rename(self, target):
            target = Path(target)
            if self == staging_dir and target == final_dir:
                raise OSError(13, "Permission denied")
            return original_rename(self, target)

        monkeypatch.setattr(Path, "rename", patched_rename)

        with pytest.raises(OSError) as exc_info:
            service._stage_then_swap(staging_dir, final_dir)

        assert exc_info.value.errno == 13
        # No old_dir should have been created
        assert not old_dir.exists()

    def test_rollback_failure_logs_error_and_raises_original(self, tmp_path, monkeypatch, caplog):
        """
        When both the staging rename AND the rollback rename fail, the method:
        - Logs an error about the rollback failure
        - Re-raises the original staging exception (not the rollback exception)
        """
        service = make_service()

        staging_dir = tmp_path / "dependency-map.staging"
        staging_dir.mkdir()

        final_dir = tmp_path / "dependency-map"
        final_dir.mkdir()
        (final_dir / "old.md").write_text("important data")

        old_dir = tmp_path / "dependency-map.old"

        original_rename = Path.rename
        original_error = OSError(28, "No space left on device")
        rollback_error = OSError(1, "Operation not permitted")

        def patched_rename(self, target):
            target = Path(target)
            # First rename: final_dir → old_dir succeeds
            if self == final_dir and target == old_dir:
                return original_rename(self, target)
            # Second rename: staging_dir → final_dir fails
            if self == staging_dir and target == final_dir:
                raise original_error
            # Third rename: old_dir → final_dir (rollback) also fails
            if self == old_dir and target == final_dir:
                raise rollback_error
            return original_rename(self, target)

        monkeypatch.setattr(Path, "rename", patched_rename)

        with caplog.at_level(logging.ERROR):
            with pytest.raises(OSError) as exc_info:
                service._stage_then_swap(staging_dir, final_dir)

        # Must re-raise the ORIGINAL staging error, not the rollback error
        assert exc_info.value is original_error

        # Must log the rollback failure
        rollback_log_found = any(
            "rollback" in record.message.lower() and record.levelno >= logging.ERROR
            for record in caplog.records
        )
        assert rollback_log_found, (
            f"Expected an ERROR-level log message mentioning rollback. "
            f"Got records: {[(r.levelno, r.message) for r in caplog.records]}"
        )

    def test_rollback_logs_warning_on_attempt(self, tmp_path, monkeypatch, caplog):
        """
        When rollback is triggered (staging rename fails and old_dir exists),
        a WARNING-level (or higher) log message about the rollback attempt must appear.
        """
        service = make_service()

        staging_dir = tmp_path / "dependency-map.staging"
        staging_dir.mkdir()

        final_dir = tmp_path / "dependency-map"
        final_dir.mkdir()

        original_rename = Path.rename

        def patched_rename(self, target):
            target = Path(target)
            if self == staging_dir and target == final_dir:
                raise OSError(28, "No space left on device")
            return original_rename(self, target)

        monkeypatch.setattr(Path, "rename", patched_rename)

        with caplog.at_level(logging.WARNING):
            with pytest.raises(OSError):
                service._stage_then_swap(staging_dir, final_dir)

        rollback_log_found = any(
            "rollback" in record.message.lower()
            for record in caplog.records
        )
        assert rollback_log_found, (
            f"Expected a log message mentioning rollback attempt. "
            f"Got records: {[(r.levelno, r.message) for r in caplog.records]}"
        )
