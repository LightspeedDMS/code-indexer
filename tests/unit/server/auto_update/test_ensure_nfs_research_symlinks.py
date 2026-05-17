"""Tests for DeploymentExecutor._ensure_nfs_research_symlinks().

Verifies:
  AC1: storage_mode != 'postgres' -> skips, returns True
  AC2: storage_mode == 'postgres', ontap is None -> skips, returns True
  AC3: ontap present but mount_point == '' -> skips, returns True
  AC4: symlinks already pointing to correct NFS paths -> idempotent, returns True
  AC5: target directories do not exist -> creates NFS dirs + symlinks
  AC6: regular dir with contents -> moves data to NFS dir, replaces with symlink
  AC7: NFS dir already has data (merge scenario) -> preserves both NFS and local data
  AC8: OSError during setup -> logs WARNING mentioning error, returns False (non-fatal)
  AC9: old .claude-projects/ NFS dir present -> data consolidated into .claude/projects/,
       old dir removed when emptied, collisions leave old dir in place for manual cleanup
  AC10: symlinks inside ~/.claude/ (e.g., old projects/ symlink from previous Step 14,
        or any other nested symlink) are unlinked during migration, not moved to NFS;
        real files are still moved

Only true external dependencies are mocked:
  - ServerConfigManager (config source)
  - Path.home (home directory resolution)
  - _cidx_data_dir (data directory path)
  - shutil.move (only in AC8 to trigger OSError)
"""

import logging
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def executor():
    """Minimal DeploymentExecutor for unit testing."""
    return DeploymentExecutor(
        repo_path=Path("/test/repo"),
        service_name="cidx-server",
    )


# ---------------------------------------------------------------------------
# Shared helpers - single source of truth for patch scaffolding
# ---------------------------------------------------------------------------


def _make_config(
    storage_mode: str = "sqlite", mount_point: Optional[str] = None
) -> MagicMock:
    """Return a mock server config with given storage_mode and optional ontap."""
    config = MagicMock()
    config.storage_mode = storage_mode
    if mount_point is None:
        config.ontap = None
    else:
        config.ontap = MagicMock()
        config.ontap.mount_point = mount_point
    return config


def _run_symlinks(
    executor,
    home: Path,
    nfs: Optional[Path],
    *,
    storage_mode: str = "postgres",
    mount_point_override: Optional[str] = None,
    move_side_effect: Any = None,
) -> bool:
    """Run _ensure_nfs_research_symlinks with controllable config and patches.

    Args:
        executor: DeploymentExecutor under test.
        home: Directory used as Path.home() return value.
        nfs: NFS mount directory; derived mount_point = str(nfs).
        storage_mode: Value to put in config.storage_mode.
        mount_point_override: When not None, overrides str(nfs) as the mount point
            (use '' to test empty-mount-point skip path).
        move_side_effect: When not None, patched as shutil.move side effect
            (e.g., OSError to trigger AC8).
    """
    if mount_point_override is not None:
        mp = mount_point_override
    elif nfs is not None:
        mp = str(nfs)
    else:
        mp = None

    config = _make_config(storage_mode=storage_mode, mount_point=mp)
    data_dir = home / ".cidx-server"

    with patch(
        "code_indexer.server.utils.config_manager.ServerConfigManager"
    ) as MockCM:
        MockCM.return_value.load_config.return_value = config
        with patch(
            "code_indexer.server.auto_update.deployment_executor._cidx_data_dir",
            data_dir,
        ):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.Path.home",
                return_value=home,
            ):
                if move_side_effect is not None:
                    with patch("shutil.move", side_effect=move_side_effect):
                        return bool(executor._ensure_nfs_research_symlinks())
                return bool(executor._ensure_nfs_research_symlinks())


# ---------------------------------------------------------------------------
# AC1: not cluster mode -> skip
# ---------------------------------------------------------------------------


class TestSkipsWhenNotClusterMode:
    def test_sqlite_returns_true_creates_no_symlinks(self, executor, tmp_path):
        """storage_mode='sqlite' -> True, no symlinks created anywhere."""
        home = tmp_path / "home"
        home.mkdir()
        result = _run_symlinks(executor, home, nfs=None, storage_mode="sqlite")
        assert result is True
        assert [p for p in home.rglob("*") if p.is_symlink()] == []


# ---------------------------------------------------------------------------
# AC2: cluster mode, no ontap -> skip
# ---------------------------------------------------------------------------


class TestSkipsWhenNoOntap:
    def test_postgres_no_ontap_returns_true_creates_no_symlinks(
        self, executor, tmp_path
    ):
        """storage_mode='postgres', ontap=None -> True, no symlinks created."""
        home = tmp_path / "home"
        home.mkdir()
        result = _run_symlinks(executor, home, nfs=None, storage_mode="postgres")
        assert result is True
        assert [p for p in home.rglob("*") if p.is_symlink()] == []


# ---------------------------------------------------------------------------
# AC3: cluster mode, empty mount_point -> skip
# ---------------------------------------------------------------------------


class TestSkipsWhenEmptyMountPoint:
    def test_empty_mount_returns_true_creates_no_symlinks(self, executor, tmp_path):
        """mount_point='' -> True, no symlinks created."""
        home = tmp_path / "home"
        home.mkdir()
        result = _run_symlinks(executor, home, nfs=None, mount_point_override="")
        assert result is True
        assert [p for p in home.rglob("*") if p.is_symlink()] == []


# ---------------------------------------------------------------------------
# AC4: already correct symlinks -> idempotent
# ---------------------------------------------------------------------------


class TestIdempotentWhenAlreadySymlinked:
    def test_correct_symlinks_returns_true_unchanged(self, executor, tmp_path):
        """Correct symlinks -> True and symlink targets remain unchanged.

        New layout: ~/.claude/ -> {nfs}/.claude/
                    ~/.cidx-server/research/ -> {nfs}/.cidx-research/
        """
        home = tmp_path / "home"
        home.mkdir()
        nfs = tmp_path / "nfs"
        nfs.mkdir()

        nfs_claude = nfs / ".claude"
        nfs_research = nfs / ".cidx-research"
        nfs_claude.mkdir()
        nfs_research.mkdir()

        # New layout: ~/.claude/ is a symlink to {nfs}/.claude/
        claude_link = home / ".claude"
        claude_link.symlink_to(nfs_claude)
        (home / ".cidx-server").mkdir()
        research_link = home / ".cidx-server" / "research"
        research_link.symlink_to(nfs_research)

        result = _run_symlinks(executor, home, nfs)

        assert result is True
        assert claude_link.readlink() == nfs_claude
        assert research_link.readlink() == nfs_research


# ---------------------------------------------------------------------------
# AC5: target directories do not exist -> create NFS dirs + symlinks
# ---------------------------------------------------------------------------


class TestCreatesSymlinksWhenMissing:
    def test_creates_symlinks_pointing_into_nfs(self, executor, tmp_path):
        """No existing dirs -> both symlinks created pointing into NFS mount.

        ~/.claude/ -> {nfs}/.claude/
        ~/.cidx-server/research/ -> {nfs}/.cidx-research/
        """
        home = tmp_path / "home"
        home.mkdir()
        nfs = tmp_path / "nfs"
        nfs.mkdir()

        result = _run_symlinks(executor, home, nfs)

        assert result is True
        claude_link = home / ".claude"
        research_link = home / ".cidx-server" / "research"
        assert claude_link.is_symlink()
        assert research_link.is_symlink()
        assert str(claude_link.readlink()).startswith(str(nfs))
        assert str(research_link.readlink()).startswith(str(nfs))

    def test_creates_nfs_subdirs(self, executor, tmp_path):
        """NFS subdirs .claude and .cidx-research must be created."""
        home = tmp_path / "home"
        home.mkdir()
        nfs = tmp_path / "nfs"
        nfs.mkdir()

        _run_symlinks(executor, home, nfs)

        assert (nfs / ".claude").is_dir()
        assert (nfs / ".cidx-research").is_dir()


# ---------------------------------------------------------------------------
# AC6: regular dir with contents -> move data to NFS, replace with symlink
# ---------------------------------------------------------------------------


class TestMovesContentsToNfs:
    def test_local_file_moved_and_dir_replaced_with_symlink(self, executor, tmp_path):
        """Local ~/.claude dir with file -> file moved to NFS, dir replaced by symlink."""
        home = tmp_path / "home"
        home.mkdir()
        nfs = tmp_path / "nfs"
        nfs.mkdir()

        claude_dir = home / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text('{"s": 1}')

        result = _run_symlinks(executor, home, nfs)

        assert result is True
        assert (nfs / ".claude" / "settings.json").exists(), (
            "File must be moved into NFS .claude dir"
        )
        assert (home / ".claude").is_symlink(), (
            "Local dir must be replaced with a symlink"
        )


# ---------------------------------------------------------------------------
# AC7: NFS already has data (merge scenario) -> preserves all files on both sides
# ---------------------------------------------------------------------------


class TestPreservesExistingNfsData:
    def test_local_and_nfs_data_both_survive_for_all_pairs(self, executor, tmp_path):
        """Pre-existing NFS data and migrated local data coexist for both symlink pairs.

        Scenario: another node already wrote to .claude and .cidx-research.
        This node has local .claude dir and .cidx-server/research dirs with data.
        After setup: NFS files from the other node survive, local files are moved in,
        and both local paths become symlinks.
        """
        home = tmp_path / "home"
        home.mkdir()
        nfs = tmp_path / "nfs"
        nfs.mkdir()

        # Another node's data already in both NFS dirs
        nfs_claude = nfs / ".claude"
        nfs_claude.mkdir()
        other_claude_file = nfs_claude / "other-node-settings.json"
        other_claude_file.write_text('{"node": "other"}')

        nfs_research = nfs / ".cidx-research"
        nfs_research.mkdir()
        other_research_file = nfs_research / "other-node-research.json"
        other_research_file.write_text('{"node": "other-research"}')

        # Local data to be migrated on this node
        claude_dir = home / ".claude"
        claude_dir.mkdir()
        (claude_dir / "local-settings.json").write_text('{"node": "local"}')

        (home / ".cidx-server").mkdir()
        research_dir = home / ".cidx-server" / "research"
        research_dir.mkdir()
        (research_dir / "local-research.json").write_text('{"node": "local-research"}')

        result = _run_symlinks(executor, home, nfs)

        assert result is True

        # Other node's files must still be there
        assert other_claude_file.read_text() == '{"node": "other"}'
        assert other_research_file.read_text() == '{"node": "other-research"}'

        # Local files must have been moved into NFS
        assert (nfs_claude / "local-settings.json").exists()
        assert (nfs_research / "local-research.json").exists()

        # Local paths must now be symlinks
        assert (home / ".claude").is_symlink()
        assert (home / ".cidx-server" / "research").is_symlink()


# ---------------------------------------------------------------------------
# AC8: OSError during setup -> logs WARNING mentioning the error, returns False
# ---------------------------------------------------------------------------


class TestHandlesOsError:
    def test_os_error_returns_false_with_warning_about_error(
        self, executor, tmp_path, caplog
    ):
        """OSError('permission denied') during move -> False, WARNING with error in caplog."""
        home = tmp_path / "home"
        home.mkdir()
        nfs = tmp_path / "nfs"
        nfs.mkdir()

        # Create local dir so the move code path is triggered
        claude_dir = home / ".claude"
        claude_dir.mkdir()
        (claude_dir / "file.txt").write_text("x")

        with caplog.at_level(logging.WARNING):
            result = _run_symlinks(
                executor,
                home,
                nfs,
                move_side_effect=OSError("permission denied"),
            )

        assert result is False
        assert any(r.levelno >= logging.WARNING for r in caplog.records), (
            "At least one WARNING must be logged on OSError"
        )
        assert "permission denied" in caplog.text, (
            "WARNING must mention the OSError message"
        )


# ---------------------------------------------------------------------------
# AC9: old .claude-projects/ NFS dir present -> data consolidated into .claude/projects/,
#      old dir removed when emptied, collisions leave old dir in place for manual cleanup
# ---------------------------------------------------------------------------


class TestMigratesOldClaudeProjectsLayout:
    def test_old_projects_data_moved_into_new_claude_projects(self, executor, tmp_path):
        """Old {nfs}/.claude-projects/ data is consolidated into {nfs}/.claude/projects/.

        Simulates a node that was previously set up with the old Step 14 layout
        where projects data lived at {nfs}/.claude-projects/.
        After running the new Step 14, that data must appear under
        {nfs}/.claude/projects/.
        """
        home = tmp_path / "home"
        home.mkdir()
        nfs = tmp_path / "nfs"
        nfs.mkdir()

        # Old NFS layout: .claude-projects/ exists with a session file
        old_projects = nfs / ".claude-projects"
        old_projects.mkdir()
        old_session = old_projects / "session-abc.json"
        old_session.write_text('{"session": "abc"}')

        result = _run_symlinks(executor, home, nfs)

        assert result is True
        new_projects = nfs / ".claude" / "projects"
        assert new_projects.is_dir(), (
            "{nfs}/.claude/projects/ must exist after migration"
        )
        assert (new_projects / "session-abc.json").exists(), (
            "Old session file must be consolidated into new projects dir"
        )
        assert (new_projects / "session-abc.json").read_text() == '{"session": "abc"}'

    def test_old_claude_projects_dir_removed_after_migration(self, executor, tmp_path):
        """Old {nfs}/.claude-projects/ directory is removed after migration when emptied."""
        home = tmp_path / "home"
        home.mkdir()
        nfs = tmp_path / "nfs"
        nfs.mkdir()

        old_projects = nfs / ".claude-projects"
        old_projects.mkdir()
        (old_projects / "session-xyz.json").write_text('{"session": "xyz"}')

        _run_symlinks(executor, home, nfs)

        assert not old_projects.exists(), (
            "Old .claude-projects/ dir must be removed after successful migration"
        )

    def test_collision_in_old_projects_leaves_dir_in_place(self, executor, tmp_path):
        """If a filename collision prevents full migration, old dir is left for manual cleanup."""
        home = tmp_path / "home"
        home.mkdir()
        nfs = tmp_path / "nfs"
        nfs.mkdir()

        # Pre-create destination to cause a collision
        nfs_claude = nfs / ".claude"
        nfs_claude.mkdir()
        new_projects = nfs_claude / "projects"
        new_projects.mkdir()
        (new_projects / "session-abc.json").write_text('{"session": "already"}')

        old_projects = nfs / ".claude-projects"
        old_projects.mkdir()
        (old_projects / "session-abc.json").write_text('{"session": "old"}')

        _run_symlinks(executor, home, nfs)

        # Collision item remains in old dir (not moved), old dir not removed
        assert old_projects.exists(), (
            "Old dir must remain when collision prevented full migration"
        )
        # Destination must still have original content (not overwritten)
        assert (
            new_projects / "session-abc.json"
        ).read_text() == '{"session": "already"}'


# ---------------------------------------------------------------------------
# AC10: symlinks inside ~/.claude/ are unlinked during migration, not moved to NFS;
#       real files are still moved
# ---------------------------------------------------------------------------


class TestHandlesNestedSymlinkDuringMigration:
    def test_old_projects_symlink_in_claude_dir_is_unlinked_not_moved(
        self, executor, tmp_path
    ):
        """Old ~/.claude/projects/ symlink (from previous Step 14) is removed during migration.

        The old Step 14 created ~/.claude/projects/ -> {nfs}/.claude-projects/.
        When the new Step 14 processes ~/.claude/ as a regular dir to migrate,
        it must unlink that stale symlink rather than moving it to NFS.
        Real files alongside the symlink must still be moved.
        """
        home = tmp_path / "home"
        home.mkdir()
        nfs = tmp_path / "nfs"
        nfs.mkdir()

        # Old NFS projects dir
        old_nfs_projects = nfs / ".claude-projects"
        old_nfs_projects.mkdir()

        # Local ~/.claude/ is a real dir containing the old projects/ symlink
        claude_dir = home / ".claude"
        claude_dir.mkdir()
        old_projects_symlink = claude_dir / "projects"
        old_projects_symlink.symlink_to(old_nfs_projects)
        # A real file alongside the symlink
        (claude_dir / "settings.json").write_text('{"s": 1}')

        result = _run_symlinks(executor, home, nfs)

        assert result is True
        # ~/.claude/ must now be a symlink to {nfs}/.claude/
        assert (home / ".claude").is_symlink(), "~/.claude/ must become a symlink"
        # The old projects/ symlink must NOT have been copied into NFS .claude/
        nfs_claude = nfs / ".claude"
        assert not (nfs_claude / "projects").is_symlink(), (
            "Stale symlink must not be moved into NFS dir"
        )
        # The real file must have been moved
        assert (nfs_claude / "settings.json").exists(), (
            "Real files must still be moved to NFS"
        )

    def test_any_symlink_inside_claude_dir_is_unlinked_not_moved(
        self, executor, tmp_path
    ):
        """Any symlink inside ~/.claude/ (pointing anywhere) is unlinked, not moved to NFS.

        When the new Step 14 restructures the entire ~/.claude/ directory by moving
        it to NFS, nested symlinks are always discarded to avoid importing stale
        or dangling pointers into the shared NFS volume.
        """
        home = tmp_path / "home"
        home.mkdir()
        nfs = tmp_path / "nfs"
        nfs.mkdir()

        claude_dir = home / ".claude"
        claude_dir.mkdir()
        # Symlink pointing to some local dir (not NFS)
        some_local_dir = tmp_path / "some-local"
        some_local_dir.mkdir()
        stale_link = claude_dir / "stale"
        stale_link.symlink_to(some_local_dir)

        result = _run_symlinks(executor, home, nfs)

        assert result is True
        nfs_claude = nfs / ".claude"
        assert not (nfs_claude / "stale").is_symlink(), (
            "Symlinks must not be moved into NFS dir"
        )
        assert (home / ".claude").is_symlink()
