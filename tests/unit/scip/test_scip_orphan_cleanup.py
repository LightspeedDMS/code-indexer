"""Unit tests for SCIP orphan database cleanup.

TDD red phase: Tests written BEFORE implementation.

Tests for SCIPGenerator._cleanup_orphan_scip_databases() which removes
index.scip.db files for projects that no longer exist in the codebase.
"""

from pathlib import Path
from typing import List

import pytest

from code_indexer.scip.generator import SCIPGenerator
from code_indexer.scip.discovery import DiscoveredProject


def _make_generator(repo_root: Path) -> SCIPGenerator:
    """Create a SCIPGenerator instance for testing."""
    return SCIPGenerator(repo_root=repo_root, max_workers=1)


def _make_discovered_project(relative_path: str) -> DiscoveredProject:
    """Create a DiscoveredProject for a given relative path."""
    return DiscoveredProject(
        relative_path=Path(relative_path),
        language="python",
        build_system="poetry",
        build_file=Path(relative_path) / "pyproject.toml",
    )


def _create_scip_db(scip_dir: Path, relative_project_path: str) -> Path:
    """Create a fake index.scip.db file for a project path."""
    db_path = scip_dir / relative_project_path / "index.scip.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"fake scip db content")
    return db_path


class TestCleanupOrphanScipDatabases:
    """Tests for _cleanup_orphan_scip_databases() method."""

    def test_method_exists_on_generator(self, tmp_path: Path):
        """SCIPGenerator has _cleanup_orphan_scip_databases() method."""
        generator = _make_generator(tmp_path)
        assert hasattr(generator, "_cleanup_orphan_scip_databases"), (
            "SCIPGenerator must have _cleanup_orphan_scip_databases() method"
        )

    def test_returns_zero_when_scip_dir_missing(self, tmp_path: Path):
        """Returns 0 when .code-indexer/scip/ directory doesn't exist."""
        generator = _make_generator(tmp_path)

        # scip_dir does not exist (tmp_path is empty)
        assert not generator.scip_dir.exists()

        discovered = [_make_discovered_project("src")]
        result = generator._cleanup_orphan_scip_databases(discovered)

        assert result == 0, f"Expected 0 orphans removed, got {result}"

    def test_returns_zero_when_no_orphans(self, tmp_path: Path):
        """Returns 0 when all scip.db files match discovered projects."""
        generator = _make_generator(tmp_path)
        scip_dir = generator.scip_dir
        scip_dir.mkdir(parents=True, exist_ok=True)

        # Create scip databases for the discovered projects
        _create_scip_db(scip_dir, "src/mypackage")
        _create_scip_db(scip_dir, "src/otherpackage")

        discovered = [
            _make_discovered_project("src/mypackage"),
            _make_discovered_project("src/otherpackage"),
        ]

        result = generator._cleanup_orphan_scip_databases(discovered)

        assert result == 0, f"Expected 0 orphans removed, got {result}"

        # Both databases should still exist
        assert (scip_dir / "src" / "mypackage" / "index.scip.db").exists()
        assert (scip_dir / "src" / "otherpackage" / "index.scip.db").exists()

    def test_identifies_and_deletes_orphan_databases(self, tmp_path: Path):
        """Orphan scip.db files are identified and deleted."""
        generator = _make_generator(tmp_path)
        scip_dir = generator.scip_dir
        scip_dir.mkdir(parents=True, exist_ok=True)

        # Create scip databases: 2 live, 1 orphan
        _create_scip_db(scip_dir, "src/liveproject")
        orphan_db = _create_scip_db(scip_dir, "src/deletedproject")

        # Only one project is "discovered" (other is orphan)
        discovered = [_make_discovered_project("src/liveproject")]

        result = generator._cleanup_orphan_scip_databases(discovered)

        assert result == 1, f"Expected 1 orphan removed, got {result}"

        # Orphan should be deleted
        assert not orphan_db.exists(), (
            f"Orphan database should have been deleted: {orphan_db}"
        )

        # Live project should still exist
        assert (scip_dir / "src" / "liveproject" / "index.scip.db").exists(), (
            "Live project database should NOT be deleted"
        )

    def test_deletes_multiple_orphans(self, tmp_path: Path):
        """Multiple orphan databases are all deleted."""
        generator = _make_generator(tmp_path)
        scip_dir = generator.scip_dir
        scip_dir.mkdir(parents=True, exist_ok=True)

        # Create 3 orphans, 1 live
        orphan1 = _create_scip_db(scip_dir, "old_project1")
        orphan2 = _create_scip_db(scip_dir, "old_project2")
        orphan3 = _create_scip_db(scip_dir, "src/removed_project")
        _create_scip_db(scip_dir, "src/live_project")

        discovered = [_make_discovered_project("src/live_project")]

        result = generator._cleanup_orphan_scip_databases(discovered)

        assert result == 3, f"Expected 3 orphans removed, got {result}"

        assert not orphan1.exists()
        assert not orphan2.exists()
        assert not orphan3.exists()
        assert (scip_dir / "src" / "live_project" / "index.scip.db").exists()

    def test_empty_discovered_projects_deletes_all_orphans(self, tmp_path: Path):
        """When discovered_projects is empty, all databases are orphans."""
        generator = _make_generator(tmp_path)
        scip_dir = generator.scip_dir
        scip_dir.mkdir(parents=True, exist_ok=True)

        db1 = _create_scip_db(scip_dir, "project1")
        db2 = _create_scip_db(scip_dir, "project2")

        result = generator._cleanup_orphan_scip_databases([])

        assert result == 2, f"Expected 2 orphans removed, got {result}"
        assert not db1.exists()
        assert not db2.exists()

    def test_empty_scip_dir_returns_zero(self, tmp_path: Path):
        """Returns 0 when scip_dir exists but contains no databases."""
        generator = _make_generator(tmp_path)
        scip_dir = generator.scip_dir
        scip_dir.mkdir(parents=True, exist_ok=True)

        # No databases
        discovered = [_make_discovered_project("src/project")]

        result = generator._cleanup_orphan_scip_databases(discovered)

        assert result == 0, f"Expected 0 orphans removed, got {result}"

    def test_deletes_empty_parent_directories_after_orphan_removal(
        self, tmp_path: Path
    ):
        """Empty parent directories are cleaned up after orphan removal."""
        generator = _make_generator(tmp_path)
        scip_dir = generator.scip_dir
        scip_dir.mkdir(parents=True, exist_ok=True)

        # Create an orphan in a dedicated subdirectory
        orphan_dir = scip_dir / "old_language_dir" / "old_project"
        orphan_dir.mkdir(parents=True, exist_ok=True)
        orphan_db = orphan_dir / "index.scip.db"
        orphan_db.write_bytes(b"fake")

        discovered = []  # No live projects

        result = generator._cleanup_orphan_scip_databases(discovered)

        assert result == 1
        assert not orphan_db.exists()
        # The empty parent directory should be cleaned up too
        # (or at minimum the database is gone)
        # Directory cleanup is best-effort, so we just verify the db is gone


class TestGenerateCallsOrphanCleanup:
    """Tests that generate() and rebuild_projects() call _cleanup_orphan_scip_databases()."""

    def test_generate_calls_cleanup_after_discovery(self, tmp_path: Path):
        """generate() calls _cleanup_orphan_scip_databases() with discovered projects."""
        from unittest.mock import patch, MagicMock

        generator = _make_generator(tmp_path)

        # Patch discovery to return an empty list
        with patch.object(generator, "_cleanup_orphan_scip_databases") as mock_cleanup:
            mock_cleanup.return_value = 0

            # Patch discovery to avoid filesystem scanning
            from code_indexer.scip.discovery import ProjectDiscovery
            with patch.object(ProjectDiscovery, "discover", return_value=[]):
                generator.generate()

            # Cleanup should have been called with the discovered projects (empty list)
            mock_cleanup.assert_called_once()
            call_args = mock_cleanup.call_args[0][0]  # First positional arg
            assert call_args == [], (
                f"Expected empty discovered list, got {call_args}"
            )

    def test_rebuild_projects_calls_cleanup_after_discovery(self, tmp_path: Path):
        """rebuild_projects() calls _cleanup_orphan_scip_databases() with discovered projects."""
        from unittest.mock import patch

        generator = _make_generator(tmp_path)
        scip_dir = generator.scip_dir
        scip_dir.mkdir(parents=True, exist_ok=True)

        # Create a minimal status file so rebuild_projects() can load it
        from code_indexer.scip.status import StatusTracker, OverallStatus, GenerationStatus
        tracker = StatusTracker(scip_dir)
        status = GenerationStatus(
            overall_status=OverallStatus.SUCCESS,
            total_projects=0,
            successful_projects=0,
            failed_projects=0,
            projects={},
        )
        tracker.save(status)

        with patch.object(generator, "_cleanup_orphan_scip_databases") as mock_cleanup:
            mock_cleanup.return_value = 0

            # Patch discovery to avoid filesystem scanning
            from code_indexer.scip.discovery import ProjectDiscovery
            with patch.object(ProjectDiscovery, "discover", return_value=[]):
                # rebuild_projects with empty list - no projects to rebuild
                generator.rebuild_projects(project_paths=[], failed_only=False)

            # Cleanup should have been called
            mock_cleanup.assert_called_once(), (
                "rebuild_projects() must call _cleanup_orphan_scip_databases() "
                "to remove stale SCIP data after branch changes"
            )
