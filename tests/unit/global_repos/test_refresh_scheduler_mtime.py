"""
Unit tests for Story #224: RefreshScheduler mtime detection and git guards (C4-C5).

C4: _has_local_changes() mtime-based change detection.
C5: _create_new_index() git guards (no git commands when no .git dir).
"""

import os
import shutil
import tempfile
import time
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
def mock_snapshot_manager(temp_golden_repos_dir):
    """Mock snapshot_manager that replicates cp --reflink=auto via shutil.copytree."""
    mgr = MagicMock()

    def _create_snapshot(repo_name, source_path):
        versioned_path = (
            Path(temp_golden_repos_dir)
            / ".versioned"
            / repo_name
            / f"v_{int(time.time())}"
        )
        versioned_path.mkdir(parents=True, exist_ok=True)
        for item in Path(source_path).iterdir():
            dest = versioned_path / item.name
            if item.is_dir():
                shutil.copytree(str(item), str(dest))
            else:
                shutil.copy2(str(item), str(dest))
        return str(versioned_path)

    mgr.create_snapshot.side_effect = _create_snapshot

    # Bug #1084 Phase A7: _has_local_changes now uses the discovery API. Mirror a
    # real local-backed VersionedSnapshotManager by globbing the temp .versioned
    # dirs these tests create, returning [(path, ts), ...] sorted ascending.
    def _list_snapshots(alias):
        repo_name = alias.removesuffix("-global")
        ns_dir = Path(temp_golden_repos_dir) / ".versioned" / repo_name
        if not ns_dir.exists():
            return []
        out = []
        for d in ns_dir.iterdir():
            if d.is_dir() and d.name.startswith("v_"):
                try:
                    out.append((str(d), int(d.name[2:])))
                except (ValueError, IndexError):
                    continue
        out.sort(key=lambda x: x[1])
        return out

    mgr.list_snapshots.side_effect = _list_snapshots
    return mgr


@pytest.fixture
def scheduler(temp_golden_repos_dir, mock_registry, mock_snapshot_manager):
    """Create a RefreshScheduler with injected mock registry."""
    config_source = MagicMock()
    config_source.get_global_refresh_interval.return_value = 3600
    return RefreshScheduler(
        golden_repos_dir=temp_golden_repos_dir,
        config_source=config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=mock_registry,
        snapshot_manager=mock_snapshot_manager,
    )


# ---------------------------------------------------------------------------
# C4: _has_local_changes()
# ---------------------------------------------------------------------------


class TestHasLocalChanges:
    """C4: _has_local_changes() mtime-based change detection algorithm."""

    def test_no_versioned_dir_returns_true(self, scheduler, temp_golden_repos_dir):
        """
        No .versioned/{repo}/ directory → return True (first version needed).
        """
        source_path = Path(temp_golden_repos_dir) / "cidx-meta"
        source_path.mkdir(parents=True, exist_ok=True)
        (source_path / "some_repo.md").write_text("# content")

        result = scheduler._has_local_changes(str(source_path), "cidx-meta-global")
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
            Path(temp_golden_repos_dir) / ".versioned" / "cidx-meta" / "v_1000"
        )
        versioned_dir.mkdir(parents=True, exist_ok=True)

        # File with mtime 2000 (newer than v_1000)
        test_file = source_path / "new_repo.md"
        test_file.write_text("# New content")
        os.utime(test_file, (2000, 2000))

        result = scheduler._has_local_changes(str(source_path), "cidx-meta-global")
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
            Path(temp_golden_repos_dir) / ".versioned" / "cidx-meta" / "v_9999999999"
        )
        versioned_dir.mkdir(parents=True, exist_ok=True)

        # File with mtime 1000 (older than v_9999999999)
        test_file = source_path / "old_repo.md"
        test_file.write_text("# Old content")
        os.utime(test_file, (1000, 1000))

        result = scheduler._has_local_changes(str(source_path), "cidx-meta-global")
        assert result is False, (
            "File mtime 1000 < version timestamp 9999999999 → must return False"
        )

    def test_empty_dir_returns_false(self, scheduler, temp_golden_repos_dir):
        """
        Source directory with no non-hidden files → return False.
        """
        source_path = Path(temp_golden_repos_dir) / "cidx-meta"
        source_path.mkdir(parents=True, exist_ok=True)

        versioned_dir = (
            Path(temp_golden_repos_dir) / ".versioned" / "cidx-meta" / "v_1000"
        )
        versioned_dir.mkdir(parents=True, exist_ok=True)

        # No non-hidden files in source_path
        result = scheduler._has_local_changes(str(source_path), "cidx-meta-global")
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
            Path(temp_golden_repos_dir) / ".versioned" / "cidx-meta" / "v_9999999999"
        )
        versioned_dir.mkdir(parents=True, exist_ok=True)

        # Only file is inside .code-indexer/ (hidden dir) with very new mtime
        hidden_dir = source_path / ".code-indexer"
        hidden_dir.mkdir(parents=True, exist_ok=True)
        hidden_file = hidden_dir / "metadata.json"
        hidden_file.write_text('{"indexed": true}')
        # Set mtime FAR in the future (newer than version timestamp)
        os.utime(hidden_file, (99999999999, 99999999999))

        result = scheduler._has_local_changes(str(source_path), "cidx-meta-global")
        # Hidden file excluded → no visible files → False
        assert result is False, (
            "Files inside .code-indexer/ must be excluded from mtime scan. "
            "Only hidden dir contents found → must return False."
        )

    def test_hidden_files_at_root_excluded(self, scheduler, temp_golden_repos_dir):
        """
        Hidden files (starting with '.') at root level must also be excluded.
        """
        source_path = Path(temp_golden_repos_dir) / "cidx-meta"
        source_path.mkdir(parents=True, exist_ok=True)

        versioned_dir = (
            Path(temp_golden_repos_dir) / ".versioned" / "cidx-meta" / "v_9999999999"
        )
        versioned_dir.mkdir(parents=True, exist_ok=True)

        # Only a hidden file at root level with very new mtime
        hidden_file = source_path / ".hidden_file"
        hidden_file.write_text("hidden")
        os.utime(hidden_file, (99999999999, 99999999999))

        result = scheduler._has_local_changes(str(source_path), "cidx-meta-global")
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

        result = scheduler._has_local_changes(str(source_path), "cidx-meta-global")
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

        result = scheduler._has_local_changes(str(source_path), "cidx-meta-global")
        # File mtime 7000 > latest v_5000 → True
        assert result is True, (
            "File mtime 7000 > latest version 5000 → must return True."
        )


# ---------------------------------------------------------------------------
# C5: Git guards in _create_new_index()
# ---------------------------------------------------------------------------


class TestCreateNewIndexGitGuards:
    """C5: _create_new_index() must skip git commands when .git dir is absent."""

    def test_no_git_commands_for_non_git_repo(self, scheduler, temp_golden_repos_dir):
        """
        When the CoW clone does not contain .git/, no git commands must run.

        This validates that the existing git_dir.exists() guard in
        _create_new_index() correctly skips git update-index and git restore
        for non-git directories like cidx-meta.

        Bug #1381: previously simulated the subprocess boundary by patching
        the global `subprocess.run` (process-wide, via `unittest.mock.patch`
        on the singleton `subprocess` module). Under full-suite concurrent
        load, any unrelated background thread invoking a real `git` subprocess
        during this test's patch window would also be intercepted by
        mock_subprocess_run and trip its "raise on any git call" guard —
        the same fragility class fixed for bug #1375 in
        test_delta_merge_frontmatter.py. Fixed by injecting the fake runner
        via RefreshScheduler's `_subprocess_runner` instance seam instead:
        `_run_subprocess()` (see refresh_scheduler.py) only consults this
        seam for calls made through THIS scheduler instance, so no other
        thread or test can ever observe or trigger it.
        """
        source_path = Path(temp_golden_repos_dir) / "cidx-meta"
        source_path.mkdir(parents=True, exist_ok=True)
        (source_path / "repo.md").write_text("# content")

        git_calls = []
        all_calls = []

        def mock_subprocess_run(cmd, **kwargs):
            all_calls.append(cmd)
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

        scheduler._subprocess_runner = mock_subprocess_run

        with patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(0, 0),
        ):
            with patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                return_value=50,
            ):
                # Index validation will fail (no real cidx), which raises RuntimeError.
                # That is expected — we only care that NO git commands were called.
                with pytest.raises(RuntimeError):
                    scheduler._create_new_index(alias_name, str(source_path))

        assert git_calls == [], (
            f"No git commands must run for non-git repos. Got: {git_calls}"
        )
        fix_config_calls = [
            cmd
            for cmd in all_calls
            if isinstance(cmd, list) and cmd[:2] == ["cidx", "fix-config"]
        ]
        assert len(fix_config_calls) == 1, (
            "Expected the injected _subprocess_runner seam to observe exactly "
            "one 'cidx fix-config' call — got "
            f"{len(fix_config_calls)} in all_calls={all_calls}. This proves "
            "production code routes subprocess calls through the per-instance "
            "seam instead of the real subprocess.run(), so no unrelated "
            "concurrent thread's real subprocess call can ever be observed here."
        )

    def test_git_commands_run_for_git_repo(
        self, scheduler, mock_snapshot_manager, temp_golden_repos_dir
    ):
        """
        When the CoW clone DOES contain .git/, git commands must run.

        Regression guard: C5 must not break git repo behavior.
        """
        source_path = Path(temp_golden_repos_dir) / "some-repo"
        source_path.mkdir(parents=True, exist_ok=True)
        (source_path / "main.py").write_text("# code")

        git_calls = []

        # Story #1034 C3: snapshot_manager now owns the CoW clone.
        # Override its side_effect to create a .git dir in the versioned path,
        # triggering the git guard in _create_snapshot() (Steps 3+4).
        def _snapshot_with_git(repo_name, source_path_arg):
            versioned_path = (
                Path(temp_golden_repos_dir)
                / ".versioned"
                / repo_name
                / f"v_{int(time.time())}"
            )
            versioned_path.mkdir(parents=True, exist_ok=True)
            (versioned_path / ".git").mkdir(parents=True, exist_ok=True)
            return str(versioned_path)

        mock_snapshot_manager.create_snapshot.side_effect = _snapshot_with_git

        def mock_subprocess_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""

            if isinstance(cmd, list) and cmd[0] == "git":
                git_calls.append(cmd)

            return result

        alias_name = "some-repo-global"
        scheduler.registry.get_global_repo = MagicMock(
            return_value={
                "enable_temporal": False,
                "enable_scip": False,
            }
        )

        with patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(0, 0),
        ):
            with patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                return_value=50,
            ):
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
