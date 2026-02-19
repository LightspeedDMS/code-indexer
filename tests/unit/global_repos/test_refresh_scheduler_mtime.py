"""
Unit tests for Story #224: RefreshScheduler mtime detection and git guards (C4-C5).

C4: _has_local_changes() mtime-based change detection.
C5: _create_new_index() git guards (no git commands when no .git dir).
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_golden_repos_dir():
    """Create temporary golden repos directory."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def mock_registry():
    """Create a mock registry."""
    registry = MagicMock()
    registry.list_global_repos.return_value = []
    registry.get_global_repo.return_value = None
    return registry


@pytest.fixture
def scheduler(temp_golden_repos_dir, mock_registry):
    """Create a RefreshScheduler with injected mock registry."""
    config_source = MagicMock()
    config_source.get_global_refresh_interval.return_value = 3600
    return RefreshScheduler(
        golden_repos_dir=temp_golden_repos_dir,
        config_source=config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=mock_registry,
    )


# ---------------------------------------------------------------------------
# C4: _has_local_changes()
# ---------------------------------------------------------------------------


class TestHasLocalChanges:
    """C4: _has_local_changes() mtime-based change detection algorithm."""

    def test_no_versioned_dir_returns_true(
        self, scheduler, temp_golden_repos_dir
    ):
        """
        No .versioned/{repo}/ directory → return True (first version needed).
        """
        source_path = Path(temp_golden_repos_dir) / "cidx-meta"
        source_path.mkdir(parents=True, exist_ok=True)
        (source_path / "some_repo.md").write_text("# content")

        result = scheduler._has_local_changes(
            str(source_path), "cidx-meta-global"
        )
        assert result is True, (
            "No versioned dir means first indexing is needed — must return True"
        )

    def test_files_newer_than_version_timestamp_returns_true(
        self, scheduler, temp_golden_repos_dir
    ):
        """
        File mtime > latest version timestamp → return True (changes detected).
        """
        source_path = Path(temp_golden_repos_dir) / "cidx-meta"
        source_path.mkdir(parents=True, exist_ok=True)

        # Latest version timestamp: 1000
        versioned_dir = (
            Path(temp_golden_repos_dir)
            / ".versioned"
            / "cidx-meta"
            / "v_1000"
        )
        versioned_dir.mkdir(parents=True, exist_ok=True)

        # File with mtime 2000 (newer than v_1000)
        test_file = source_path / "new_repo.md"
        test_file.write_text("# New content")
        os.utime(test_file, (2000, 2000))

        result = scheduler._has_local_changes(
            str(source_path), "cidx-meta-global"
        )
        assert result is True, (
            "File mtime 2000 > version timestamp 1000 → must return True"
        )

    def test_files_older_than_version_timestamp_returns_false(
        self, scheduler, temp_golden_repos_dir
    ):
        """
        All file mtimes <= latest version timestamp → return False (no changes).
        """
        source_path = Path(temp_golden_repos_dir) / "cidx-meta"
        source_path.mkdir(parents=True, exist_ok=True)

        # Latest version timestamp: 9999999999 (far future)
        versioned_dir = (
            Path(temp_golden_repos_dir)
            / ".versioned"
            / "cidx-meta"
            / "v_9999999999"
        )
        versioned_dir.mkdir(parents=True, exist_ok=True)

        # File with mtime 1000 (older than v_9999999999)
        test_file = source_path / "old_repo.md"
        test_file.write_text("# Old content")
        os.utime(test_file, (1000, 1000))

        result = scheduler._has_local_changes(
            str(source_path), "cidx-meta-global"
        )
        assert result is False, (
            "File mtime 1000 < version timestamp 9999999999 → must return False"
        )

    def test_empty_dir_returns_false(
        self, scheduler, temp_golden_repos_dir
    ):
        """
        Source directory with no non-hidden files → return False.
        """
        source_path = Path(temp_golden_repos_dir) / "cidx-meta"
        source_path.mkdir(parents=True, exist_ok=True)

        versioned_dir = (
            Path(temp_golden_repos_dir)
            / ".versioned"
            / "cidx-meta"
            / "v_1000"
        )
        versioned_dir.mkdir(parents=True, exist_ok=True)

        # No non-hidden files in source_path
        result = scheduler._has_local_changes(
            str(source_path), "cidx-meta-global"
        )
        assert result is False, "Empty source dir → must return False"

    def test_hidden_dirs_excluded_from_mtime_scan(
        self, scheduler, temp_golden_repos_dir
    ):
        """
        Files inside hidden directories (e.g. .code-indexer/) must be excluded.

        .code-indexer/ contains index data and must not trigger refreshes.
        """
        source_path = Path(temp_golden_repos_dir) / "cidx-meta"
        source_path.mkdir(parents=True, exist_ok=True)

        # Latest version timestamp: 9999999999
        versioned_dir = (
            Path(temp_golden_repos_dir)
            / ".versioned"
            / "cidx-meta"
            / "v_9999999999"
        )
        versioned_dir.mkdir(parents=True, exist_ok=True)

        # Only file is inside .code-indexer/ (hidden dir) with very new mtime
        hidden_dir = source_path / ".code-indexer"
        hidden_dir.mkdir(parents=True, exist_ok=True)
        hidden_file = hidden_dir / "metadata.json"
        hidden_file.write_text('{"indexed": true}')
        # Set mtime FAR in the future (newer than version timestamp)
        os.utime(hidden_file, (99999999999, 99999999999))

        result = scheduler._has_local_changes(
            str(source_path), "cidx-meta-global"
        )
        # Hidden file excluded → no visible files → False
        assert result is False, (
            "Files inside .code-indexer/ must be excluded from mtime scan. "
            "Only hidden dir contents found → must return False."
        )

    def test_hidden_files_at_root_excluded(
        self, scheduler, temp_golden_repos_dir
    ):
        """
        Hidden files (starting with '.') at root level must also be excluded.
        """
        source_path = Path(temp_golden_repos_dir) / "cidx-meta"
        source_path.mkdir(parents=True, exist_ok=True)

        versioned_dir = (
            Path(temp_golden_repos_dir)
            / ".versioned"
            / "cidx-meta"
            / "v_9999999999"
        )
        versioned_dir.mkdir(parents=True, exist_ok=True)

        # Only a hidden file at root level with very new mtime
        hidden_file = source_path / ".hidden_file"
        hidden_file.write_text("hidden")
        os.utime(hidden_file, (99999999999, 99999999999))

        result = scheduler._has_local_changes(
            str(source_path), "cidx-meta-global"
        )
        assert result is False, "Hidden files at root level must be excluded from scan"

    def test_uses_latest_version_dir_when_multiple_exist(
        self, scheduler, temp_golden_repos_dir
    ):
        """
        When multiple versioned dirs exist, use the LATEST timestamp.

        File mtime between two versions should use the latest (highest) version.
        """
        source_path = Path(temp_golden_repos_dir) / "cidx-meta"
        source_path.mkdir(parents=True, exist_ok=True)

        versioned_base = Path(temp_golden_repos_dir) / ".versioned" / "cidx-meta"
        (versioned_base / "v_1000").mkdir(parents=True, exist_ok=True)
        (versioned_base / "v_5000").mkdir(parents=True, exist_ok=True)

        # File mtime 3000: newer than v_1000 but OLDER than v_5000
        test_file = source_path / "repo.md"
        test_file.write_text("# content")
        os.utime(test_file, (3000, 3000))

        result = scheduler._has_local_changes(
            str(source_path), "cidx-meta-global"
        )
        # Latest version is v_5000: file mtime 3000 < 5000 → False
        assert result is False, (
            "Must compare against latest version (v_5000). "
            "File mtime 3000 < 5000 → must return False."
        )

    def test_uses_latest_version_dir_file_newer_than_latest(
        self, scheduler, temp_golden_repos_dir
    ):
        """
        File mtime > latest version → True, even when older version also exists.
        """
        source_path = Path(temp_golden_repos_dir) / "cidx-meta"
        source_path.mkdir(parents=True, exist_ok=True)

        versioned_base = Path(temp_golden_repos_dir) / ".versioned" / "cidx-meta"
        (versioned_base / "v_1000").mkdir(parents=True, exist_ok=True)
        (versioned_base / "v_5000").mkdir(parents=True, exist_ok=True)

        # File mtime 7000: newer than BOTH versions
        test_file = source_path / "repo.md"
        test_file.write_text("# content")
        os.utime(test_file, (7000, 7000))

        result = scheduler._has_local_changes(
            str(source_path), "cidx-meta-global"
        )
        # File mtime 7000 > latest v_5000 → True
        assert result is True, (
            "File mtime 7000 > latest version 5000 → must return True."
        )


# ---------------------------------------------------------------------------
# C5: Git guards in _create_new_index()
# ---------------------------------------------------------------------------


class TestCreateNewIndexGitGuards:
    """C5: _create_new_index() must skip git commands when .git dir is absent."""

    def test_no_git_commands_for_non_git_repo(
        self, scheduler, temp_golden_repos_dir
    ):
        """
        When the CoW clone does not contain .git/, no git commands must run.

        This validates that the existing git_dir.exists() guard in
        _create_new_index() correctly skips git update-index and git restore
        for non-git directories like cidx-meta.
        """
        source_path = Path(temp_golden_repos_dir) / "cidx-meta"
        source_path.mkdir(parents=True, exist_ok=True)
        (source_path / "repo.md").write_text("# content")

        git_calls = []

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "git":
                git_calls.append(cmd)
                raise AssertionError(
                    f"Git command must not run for non-git repos: {cmd}"
                )
            # All other commands (cp, cidx) succeed without side effects
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        alias_name = "cidx-meta-global"
        scheduler.registry.get_global_repo = MagicMock(
            return_value={
                "enable_temporal": False,
                "enable_scip": False,
            }
        )

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            # Index validation will fail (no real cidx), which raises RuntimeError.
            # That is expected — we only care that NO git commands were called.
            with pytest.raises(RuntimeError):
                scheduler._create_new_index(alias_name, str(source_path))

        assert git_calls == [], (
            f"No git commands must run for non-git repos. Got: {git_calls}"
        )

    def test_git_commands_run_for_git_repo(
        self, scheduler, temp_golden_repos_dir
    ):
        """
        When the CoW clone DOES contain .git/, git commands must run.

        Regression guard: C5 must not break git repo behavior.
        """
        source_path = Path(temp_golden_repos_dir) / "some-repo"
        source_path.mkdir(parents=True, exist_ok=True)
        (source_path / "main.py").write_text("# code")

        git_calls = []

        def mock_subprocess_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""

            if isinstance(cmd, list):
                if cmd[0] == "git":
                    git_calls.append(cmd)
                elif cmd[0] == "cp":
                    # After CoW clone, create a fake .git dir in versioned path
                    # to trigger the git guard
                    cwd_arg = kwargs.get("cwd")
                    # The destination is the last arg in cp command
                    dest = Path(cmd[-1])
                    dest.mkdir(parents=True, exist_ok=True)
                    (dest / ".git").mkdir(parents=True, exist_ok=True)

            return result

        alias_name = "some-repo-global"
        scheduler.registry.get_global_repo = MagicMock(
            return_value={
                "enable_temporal": False,
                "enable_scip": False,
            }
        )

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            with pytest.raises(RuntimeError):
                # Will fail at index validation, but git commands should have run
                scheduler._create_new_index(alias_name, str(source_path))

        # Should have called git update-index and git restore
        git_cmd_names = [cmd[1] for cmd in git_calls]
        assert "update-index" in git_cmd_names, (
            "git update-index must be called when .git dir exists"
        )
        assert "restore" in git_cmd_names, (
            "git restore must be called when .git dir exists"
        )
