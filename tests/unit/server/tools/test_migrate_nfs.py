"""Tests for SqliteToPostgresMigrator.migrate_nfs() and _migrate_dir_to_nfs().

Verifies:
  AC1: migrate_nfs with non-existent mount point -> error in report
  AC2: migrate_nfs with relative path -> error in report
  AC3: migrate_nfs fresh install (no existing dirs) -> creates symlinks
  AC4: migrate_nfs with existing directories -> migrates data, creates symlinks
  AC5: migrate_nfs already symlinked -> skips (idempotent)
  AC6: migrate_nfs with old .claude-projects/ -> consolidates data
  AC7: _migrate_dir_to_nfs with nested symlink -> symlink absent from NFS, real files migrated
  AC7b: _migrate_dir_to_nfs already-correct symlink -> already_symlinked status (underpins AC5)
  AC7c: _migrate_dir_to_nfs creates NFS target dir when missing (underpins AC3)

Real filesystem operations are used throughout.
The migrate_nfs() method accepts an explicit home_path parameter to avoid monkey patching.
"""

import pytest

from code_indexer.server.tools.migrate_to_postgres import SqliteToPostgresMigrator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def migrator(tmp_path):
    """SqliteToPostgresMigrator constructed with tmp_path-derived inert paths."""
    return SqliteToPostgresMigrator(
        sqlite_db_path=str(tmp_path / "cidx_server.db"),
        groups_db_path=str(tmp_path / "groups.db"),
        pg_connection_string=str(tmp_path / "cidx_test.dsn"),
    )


@pytest.fixture()
def nfs_env(tmp_path):
    """Provide a (home, nfs) pair of real tmp directories for NFS migration tests."""
    home = tmp_path / "home"
    home.mkdir()
    nfs = tmp_path / "nfs"
    nfs.mkdir()
    return home, nfs


# ---------------------------------------------------------------------------
# AC1: non-existent mount point -> error in report
# ---------------------------------------------------------------------------


class TestMigrateNfsNonExistentMount:
    def test_returns_error_in_report(self, migrator, tmp_path):
        """mount point that does not exist -> 'error' key in report."""
        report = migrator.migrate_nfs(
            str(tmp_path / "does-not-exist"), home_path=tmp_path / "home"
        )
        assert "error" in report
        assert "does not exist" in report["error"]


# ---------------------------------------------------------------------------
# AC2: relative path -> error in report
# ---------------------------------------------------------------------------


class TestMigrateNfsRelativePath:
    def test_relative_path_returns_error(self, migrator, tmp_path):
        """Relative mount path -> 'error' key in report mentioning 'absolute'."""
        report = migrator.migrate_nfs("relative/path", home_path=tmp_path / "home")
        assert "error" in report
        assert "absolute" in report["error"]


# ---------------------------------------------------------------------------
# AC3: fresh install (no existing dirs) -> creates symlinks
# ---------------------------------------------------------------------------


class TestMigrateNfsFreshInstall:
    def test_creates_symlinks_pointing_into_nfs(self, migrator, nfs_env):
        """No existing dirs -> both symlinks created pointing into NFS mount."""
        home, nfs = nfs_env
        report = migrator.migrate_nfs(str(nfs), home_path=home)
        assert "error" not in report
        assert (home / ".claude").is_symlink()
        assert (home / ".cidx-server" / "research").is_symlink()
        assert (home / ".claude").readlink() == nfs / ".claude"
        assert (home / ".cidx-server" / "research").readlink() == nfs / ".cidx-research"

    def test_creates_nfs_subdirs(self, migrator, nfs_env):
        """NFS subdirs .claude and .cidx-research are created."""
        home, nfs = nfs_env
        migrator.migrate_nfs(str(nfs), home_path=home)
        assert (nfs / ".claude").is_dir()
        assert (nfs / ".cidx-research").is_dir()


# ---------------------------------------------------------------------------
# AC4: existing directories -> migrates data, creates symlinks
# ---------------------------------------------------------------------------


class TestMigrateNfsExistingDirs:
    def test_migrates_claude_files_and_creates_symlink(self, migrator, nfs_env):
        """Local ~/.claude/ files moved to NFS; local dir replaced by symlink."""
        home, nfs = nfs_env
        claude_dir = home / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text('{"s": 1}')

        report = migrator.migrate_nfs(str(nfs), home_path=home)

        assert "error" not in report
        assert (nfs / ".claude" / "settings.json").exists()
        assert (home / ".claude").is_symlink()
        assert report["claude_home"]["items_moved"] == 1

    def test_migrates_research_files_and_creates_symlink(self, migrator, nfs_env):
        """Local ~/.cidx-server/research/ files moved to NFS; symlink created."""
        home, nfs = nfs_env
        (home / ".cidx-server").mkdir()
        research_dir = home / ".cidx-server" / "research"
        research_dir.mkdir()
        (research_dir / "session.json").write_text('{"r": 1}')

        report = migrator.migrate_nfs(str(nfs), home_path=home)

        assert "error" not in report
        assert (nfs / ".cidx-research" / "session.json").exists()
        assert (home / ".cidx-server" / "research").is_symlink()
        assert report["research"]["items_moved"] == 1


# ---------------------------------------------------------------------------
# AC5: already symlinked -> skips (idempotent)
# ---------------------------------------------------------------------------


class TestMigrateNfsIdempotent:
    def test_correct_symlinks_get_already_symlinked_status(self, migrator, nfs_env):
        """Correct symlinks already in place -> 'already_symlinked' status, zero moves."""
        home, nfs = nfs_env
        nfs_claude = nfs / ".claude"
        nfs_claude.mkdir()
        (home / ".claude").symlink_to(nfs_claude)

        nfs_research = nfs / ".cidx-research"
        nfs_research.mkdir()
        (home / ".cidx-server").mkdir()
        (home / ".cidx-server" / "research").symlink_to(nfs_research)

        report = migrator.migrate_nfs(str(nfs), home_path=home)

        assert "error" not in report
        assert report["claude_home"]["status"] == "already_symlinked"
        assert report["research"]["status"] == "already_symlinked"
        assert report["claude_home"]["items_moved"] == 0
        assert report["research"]["items_moved"] == 0


# ---------------------------------------------------------------------------
# AC6: old .claude-projects/ present -> consolidates data
# ---------------------------------------------------------------------------


class TestMigrateNfsOldLayout:
    def test_old_projects_data_consolidated_into_new_location(self, migrator, nfs_env):
        """Old {nfs}/.claude-projects/ data moved to {nfs}/.claude/projects/."""
        home, nfs = nfs_env
        old_projects = nfs / ".claude-projects"
        old_projects.mkdir()
        (old_projects / "session-abc.json").write_text('{"session": "abc"}')

        report = migrator.migrate_nfs(str(nfs), home_path=home)

        assert "error" not in report
        assert "claude_projects_consolidation" in report
        new_projects = nfs / ".claude" / "projects"
        assert (new_projects / "session-abc.json").read_text() == '{"session": "abc"}'
        assert not old_projects.exists()

    def test_consolidation_status_reported_as_ok(self, migrator, nfs_env):
        """Consolidation report shows status=ok and correct item count."""
        home, nfs = nfs_env
        old_projects = nfs / ".claude-projects"
        old_projects.mkdir()
        (old_projects / "item.json").write_text("{}")

        report = migrator.migrate_nfs(str(nfs), home_path=home)

        consolidation = report["claude_projects_consolidation"]
        assert consolidation["status"] == "ok"
        assert consolidation["items_moved"] == 1


# ---------------------------------------------------------------------------
# AC7: _migrate_dir_to_nfs nested symlink -> absent from NFS, real files migrated
# AC7b: already-correct symlink -> already_symlinked status (underpins AC5)
# AC7c: creates NFS target dir when missing (underpins AC3)
# ---------------------------------------------------------------------------


class TestMigrateDirToNfs:
    def test_nested_symlink_absent_from_nfs_real_files_moved(self, tmp_path):
        """Nested symlinks inside the source dir are removed and absent from NFS target."""
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        nfs_target = tmp_path / "nfs_target"

        some_dir = tmp_path / "some-dir"
        some_dir.mkdir()
        (local_dir / "stale").symlink_to(some_dir)
        (local_dir / "real-file.txt").write_text("real")

        SqliteToPostgresMigrator._migrate_dir_to_nfs(local_dir, nfs_target)

        assert not (nfs_target / "stale").exists(), (
            "Nested symlink must be absent from NFS target"
        )
        assert (nfs_target / "real-file.txt").exists(), (
            "Real files must be migrated to NFS target"
        )
        assert local_dir.is_symlink(), "Source path must become a symlink to NFS target"

    def test_already_correct_symlink_returns_already_symlinked(self, tmp_path):
        """Already-correct symlink -> already_symlinked status, zero items moved."""
        nfs_target = tmp_path / "nfs_target"
        nfs_target.mkdir()
        local_link = tmp_path / "local"
        local_link.symlink_to(nfs_target)

        result = SqliteToPostgresMigrator._migrate_dir_to_nfs(local_link, nfs_target)

        assert result["status"] == "already_symlinked"
        assert result["items_moved"] == 0

    def test_creates_nfs_target_dir_if_missing(self, tmp_path):
        """NFS target directory is created if it does not yet exist."""
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        nfs_target = tmp_path / "nfs" / "subdir"

        SqliteToPostgresMigrator._migrate_dir_to_nfs(local_dir, nfs_target)

        assert nfs_target.is_dir()
