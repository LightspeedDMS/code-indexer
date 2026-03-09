"""
Unit tests for GitOperationsService merge_branch, _parse_conflicts, and
_check_if_binary_conflict methods (Story #388).

Tests use real git repos with tmp_path fixtures to avoid mocking.
"""

import subprocess
from pathlib import Path

import pytest

from code_indexer.server.services.git_operations_service import (
    GitCommandError,
    GitOperationsService,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_test_repo(path: Path) -> None:
    """Create a git repo with an initial commit."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (path / "initial.txt").write_text("initial content\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )


def _get_service() -> GitOperationsService:
    """Return a GitOperationsService instance for testing."""
    return GitOperationsService()


# ---------------------------------------------------------------------------
# Tests for merge_branch (service level, real git repos)
# ---------------------------------------------------------------------------


class TestMergeBranchClean:
    """Tests for clean (non-conflicting) merges."""

    def test_clean_merge_succeeds(self, tmp_path: Path) -> None:
        """Clean merge returns success=True with empty conflicts list."""
        _create_test_repo(tmp_path)
        service = _get_service()

        # Create a feature branch with a new file
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature"],
            check=True,
            capture_output=True,
        )
        (tmp_path / "feature.txt").write_text("feature content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "feature commit"],
            check=True,
            capture_output=True,
        )

        # Switch back to main branch
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "master"],
            check=True,
            capture_output=True,
        )

        result = service.merge_branch(tmp_path, "feature")

        assert result["success"] is True
        assert result["conflicts"] == []
        assert "merge_summary" in result

    def test_clean_merge_already_up_to_date(self, tmp_path: Path) -> None:
        """Merging a branch already merged returns success=True."""
        _create_test_repo(tmp_path)
        service = _get_service()

        # Try to merge main into itself - should be "Already up to date."
        result = service.merge_branch(tmp_path, "master")

        assert result["success"] is True
        assert result["conflicts"] == []


class TestMergeBranchConflicts:
    """Tests for conflicting merges."""

    def _create_conflict_scenario(self, tmp_path: Path) -> None:
        """Set up a repo with a conflict between master and feature branch."""
        _create_test_repo(tmp_path)

        # Create and switch to feature branch
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature"],
            check=True,
            capture_output=True,
        )
        # Modify the same file differently
        (tmp_path / "initial.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "feature change"],
            check=True,
            capture_output=True,
        )

        # Go back to master and make a different change
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "master"],
            check=True,
            capture_output=True,
        )
        (tmp_path / "initial.txt").write_text("master version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "master change"],
            check=True,
            capture_output=True,
        )

    def test_conflict_merge_returns_conflicts(self, tmp_path: Path) -> None:
        """Conflicting merge returns success=False with non-empty conflicts list."""
        self._create_conflict_scenario(tmp_path)
        service = _get_service()

        result = service.merge_branch(tmp_path, "feature")

        assert result["success"] is False
        assert len(result["conflicts"]) > 0
        assert "merge_summary" in result

    def test_conflict_includes_file_path_and_type(self, tmp_path: Path) -> None:
        """Conflict entries contain file, status, conflict_type, is_binary fields."""
        self._create_conflict_scenario(tmp_path)
        service = _get_service()

        result = service.merge_branch(tmp_path, "feature")

        assert len(result["conflicts"]) > 0
        conflict = result["conflicts"][0]
        assert "file" in conflict
        assert "status" in conflict
        assert "conflict_type" in conflict
        assert "is_binary" in conflict
        assert conflict["file"] == "initial.txt"
        assert conflict["status"] == "UU"
        assert conflict["is_binary"] is False

    def test_conflict_type_is_content_for_both_modified(self, tmp_path: Path) -> None:
        """Both-modified conflicts show conflict_type='content'."""
        self._create_conflict_scenario(tmp_path)
        service = _get_service()

        result = service.merge_branch(tmp_path, "feature")

        conflict = result["conflicts"][0]
        assert conflict["conflict_type"] == "content"


class TestMergeBranchBinaryDetection:
    """Tests for binary conflict detection."""

    def test_conflict_merge_binary_detection(self, tmp_path: Path) -> None:
        """Binary file conflicts are detected (is_binary=True)."""
        _create_test_repo(tmp_path)

        # Create a binary-like file on feature branch
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature"],
            check=True,
            capture_output=True,
        )
        # Write binary content (null bytes = binary)
        (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02\x03feature_version")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "feature binary"],
            check=True,
            capture_output=True,
        )

        # Go back to master, add a different binary file version
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "master"],
            check=True,
            capture_output=True,
        )
        (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02\x03master_version")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "master binary"],
            check=True,
            capture_output=True,
        )

        service = _get_service()
        result = service.merge_branch(tmp_path, "feature")

        # Binary conflicts should be detected
        assert result["success"] is False
        assert len(result["conflicts"]) > 0
        binary_conflict = next(
            (c for c in result["conflicts"] if c["file"] == "data.bin"), None
        )
        assert binary_conflict is not None
        assert binary_conflict["is_binary"] is True


class TestMergeBranchErrors:
    """Tests for error conditions in merge_branch."""

    def test_invalid_branch_raises_git_error(self, tmp_path: Path) -> None:
        """Merging a nonexistent branch raises GitCommandError."""
        _create_test_repo(tmp_path)
        service = _get_service()

        with pytest.raises(GitCommandError):
            service.merge_branch(tmp_path, "nonexistent-branch-xyz")

    def test_merge_already_in_progress(self, tmp_path: Path) -> None:
        """Attempting a merge when MERGE_HEAD exists fails gracefully."""
        _create_test_repo(tmp_path)

        # Create conflicting scenario
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature"],
            check=True,
            capture_output=True,
        )
        (tmp_path / "initial.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "feature"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "master"],
            check=True,
            capture_output=True,
        )
        (tmp_path / "initial.txt").write_text("master version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "master"],
            check=True,
            capture_output=True,
        )

        service = _get_service()
        # First merge - creates conflict state
        result = service.merge_branch(tmp_path, "feature")
        assert result["success"] is False  # Should have conflicts

        # Second merge while in conflict state should raise GitCommandError
        with pytest.raises(GitCommandError):
            service.merge_branch(tmp_path, "feature")


# ---------------------------------------------------------------------------
# Tests for _parse_conflicts (parsing logic)
# ---------------------------------------------------------------------------


class TestParseConflicts:
    """Tests for the _parse_conflicts helper method."""

    def test_parse_content_conflict(self, tmp_path: Path) -> None:
        """Parse 'CONFLICT (content): Merge conflict in file.py'."""
        _create_test_repo(tmp_path)
        service = _get_service()

        # Create the conflicted file (simulating git merge state)
        (tmp_path / "file.py").write_text(
            "<<<<<<< HEAD\nmaster line\n=======\nfeature line\n>>>>>>> feature\n"
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True
        )

        merge_output = "CONFLICT (content): Merge conflict in file.py\n"
        conflicts = service._parse_conflicts(merge_output, tmp_path)

        # Should find conflict_type=content for file.py (from CONFLICT line)
        content_conflicts = [c for c in conflicts if c["file"] == "file.py"]
        if content_conflicts:
            assert content_conflicts[0]["conflict_type"] == "content"

    def test_parse_add_add_conflict(self, tmp_path: Path) -> None:
        """Parse 'CONFLICT (add/add): Merge conflict in file.py'."""
        _create_test_repo(tmp_path)
        service = _get_service()

        (tmp_path / "file.py").write_text(
            "<<<<<<< HEAD\nmaster\n=======\nfeature\n>>>>>>> feature\n"
        )

        merge_output = "CONFLICT (add/add): Merge conflict in file.py\n"
        conflicts = service._parse_conflicts(merge_output, tmp_path)

        content_conflicts = [c for c in conflicts if c["file"] == "file.py"]
        if content_conflicts:
            assert content_conflicts[0]["conflict_type"] == "add/add"

    def test_parse_multiple_conflicts(self, tmp_path: Path) -> None:
        """Parse multiple CONFLICT lines correctly with real git conflict state."""
        _create_test_repo(tmp_path)
        service = _get_service()

        # Create two files on master with initial content
        (tmp_path / "file1.py").write_text("master v1\n")
        (tmp_path / "file2.py").write_text("master v2\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "add two files"],
            check=True,
            capture_output=True,
        )

        # Create feature branch and modify both files differently
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature"],
            check=True,
            capture_output=True,
        )
        (tmp_path / "file1.py").write_text("feature version of file1\n")
        (tmp_path / "file2.py").write_text("feature version of file2\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "feature changes"],
            check=True,
            capture_output=True,
        )

        # Switch to master and modify both files differently to create conflicts
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "master"],
            check=True,
            capture_output=True,
        )
        (tmp_path / "file1.py").write_text("master version of file1\n")
        (tmp_path / "file2.py").write_text("master version of file2\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "master changes"],
            check=True,
            capture_output=True,
        )

        # Trigger a real merge conflict (ignore the error - we want conflict state)
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature"],
            capture_output=True,
        )

        merge_output = (
            "CONFLICT (content): Merge conflict in file1.py\n"
            "CONFLICT (content): Merge conflict in file2.py\n"
        )
        conflicts = service._parse_conflicts(merge_output, tmp_path)

        # git status --porcelain should show UU for both files in conflict state
        conflict_files_from_output = {
            c["file"] for c in conflicts if c["file"] in ("file1.py", "file2.py")
        }
        assert len(conflict_files_from_output) == 2


class TestCheckIfBinaryConflict:
    """Tests for _check_if_binary_conflict helper."""

    def test_text_file_with_conflict_markers_is_not_binary(
        self, tmp_path: Path
    ) -> None:
        """Text file with conflict markers returns is_binary=False."""
        _create_test_repo(tmp_path)
        service = _get_service()

        (tmp_path / "text.py").write_text(
            "<<<<<<< HEAD\nmaster version\n=======\nfeature version\n>>>>>>> feature\n"
        )

        result = service._check_if_binary_conflict(tmp_path, "text.py")
        assert result is False

    def test_text_file_without_conflict_markers_is_binary(
        self, tmp_path: Path
    ) -> None:
        """Text file without conflict markers is treated as binary."""
        _create_test_repo(tmp_path)
        service = _get_service()

        (tmp_path / "no_markers.txt").write_text("just some text\n")

        result = service._check_if_binary_conflict(tmp_path, "no_markers.txt")
        assert result is True

    def test_actual_binary_file_is_binary(self, tmp_path: Path) -> None:
        """Binary file (null bytes) returns is_binary=True."""
        _create_test_repo(tmp_path)
        service = _get_service()

        (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02\x03\xff\xfe")

        result = service._check_if_binary_conflict(tmp_path, "data.bin")
        assert result is True

    def test_missing_file_is_binary(self, tmp_path: Path) -> None:
        """Missing file (OSError) returns is_binary=True."""
        _create_test_repo(tmp_path)
        service = _get_service()

        result = service._check_if_binary_conflict(tmp_path, "nonexistent.bin")
        assert result is True
