"""
Unit tests for GitOperationsService conflict resolution methods (Story #389).

Tests use real git repos with tmp_path fixtures to avoid mocking.
Covers: parse_conflict_markers, git_conflict_status, git_mark_resolved.
"""

import subprocess
from pathlib import Path

import pytest

from code_indexer.server.services.git_operations_service import (
    GitOperationsService,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd: list, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), check=check, capture_output=True, text=True
    )


def _create_repo_with_conflict(path: Path) -> None:
    """Create git repo in merge-conflict state with a text conflict in file.py."""
    _run(["git", "init", str(path)], path.parent)
    _run(["git", "-C", str(path), "config", "user.email", "test@test.com"], path.parent)
    _run(["git", "-C", str(path), "config", "user.name", "Test"], path.parent)

    # Initial commit on default branch
    (path / "file.py").write_text("line1\nline2\nline3\n")
    _run(["git", "-C", str(path), "add", "."], path.parent)
    _run(["git", "-C", str(path), "commit", "-m", "initial"], path.parent)

    # Detect default branch name
    result = subprocess.run(
        ["git", "-C", str(path), "branch", "--show-current"],
        capture_output=True,
        text=True,
    )
    default_branch = result.stdout.strip() or "master"

    # Create feature branch with conflicting change
    _run(["git", "-C", str(path), "checkout", "-b", "feature"], path.parent)
    (path / "file.py").write_text("line1\nfeature_change\nline3\n")
    _run(["git", "-C", str(path), "add", "."], path.parent)
    _run(["git", "-C", str(path), "commit", "-m", "feature change"], path.parent)

    # Back to default branch with conflicting change
    _run(["git", "-C", str(path), "checkout", default_branch], path.parent)
    (path / "file.py").write_text("line1\nmain_change\nline3\n")
    _run(["git", "-C", str(path), "add", "."], path.parent)
    _run(["git", "-C", str(path), "commit", "-m", "main change"], path.parent)

    # Trigger merge conflict (don't check=True — it will fail with exit code 1)
    subprocess.run(
        ["git", "-C", str(path), "merge", "feature"], capture_output=True, text=True
    )
    # Repo is now in merge-conflict state


def _create_clean_repo(path: Path) -> None:
    """Create a git repo with no pending merge."""
    _run(["git", "init", str(path)], path.parent)
    _run(["git", "-C", str(path), "config", "user.email", "test@test.com"], path.parent)
    _run(["git", "-C", str(path), "config", "user.name", "Test"], path.parent)
    (path / "file.py").write_text("clean content\n")
    _run(["git", "-C", str(path), "add", "."], path.parent)
    _run(["git", "-C", str(path), "commit", "-m", "initial"], path.parent)


def _get_service() -> GitOperationsService:
    return GitOperationsService()


# ---------------------------------------------------------------------------
# Tests for parse_conflict_markers
# ---------------------------------------------------------------------------


class TestParseConflictMarkers:
    """Tests for parse_conflict_markers using real files."""

    def test_single_conflict_region(self, tmp_path: Path) -> None:
        """Single conflict block produces one region."""
        _create_clean_repo(tmp_path)
        service = _get_service()

        conflict_content = (
            "line1\n"
            "<<<<<<< HEAD\n"
            "ours_content\n"
            "=======\n"
            "theirs_content\n"
            ">>>>>>> feature\n"
            "line3\n"
        )
        (tmp_path / "conflict.py").write_text(conflict_content)

        regions = service.parse_conflict_markers(tmp_path, "conflict.py")

        assert len(regions) == 1

    def test_multiple_conflict_regions(self, tmp_path: Path) -> None:
        """Two conflict blocks produce two regions."""
        _create_clean_repo(tmp_path)
        service = _get_service()

        conflict_content = (
            "<<<<<<< HEAD\n"
            "ours1\n"
            "=======\n"
            "theirs1\n"
            ">>>>>>> feature\n"
            "middle\n"
            "<<<<<<< HEAD\n"
            "ours2\n"
            "=======\n"
            "theirs2\n"
            ">>>>>>> feature\n"
        )
        (tmp_path / "conflict.py").write_text(conflict_content)

        regions = service.parse_conflict_markers(tmp_path, "conflict.py")

        assert len(regions) == 2

    def test_conflict_region_line_numbers(self, tmp_path: Path) -> None:
        """start_line and end_line are 1-based and correct."""
        _create_clean_repo(tmp_path)
        service = _get_service()

        # Line 1: line1, Line 2: <<<<<<< HEAD, ..., Line 6: >>>>>>> feature
        conflict_content = (
            "line1\n"
            "<<<<<<< HEAD\n"
            "ours_content\n"
            "=======\n"
            "theirs_content\n"
            ">>>>>>> feature\n"
        )
        (tmp_path / "conflict.py").write_text(conflict_content)

        regions = service.parse_conflict_markers(tmp_path, "conflict.py")

        assert len(regions) == 1
        assert regions[0]["start_line"] == 2  # <<<<<<< is line 2
        assert regions[0]["end_line"] == 6  # >>>>>>> is line 6

    def test_conflict_region_labels(self, tmp_path: Path) -> None:
        """ours_label and theirs_label are parsed correctly."""
        _create_clean_repo(tmp_path)
        service = _get_service()

        conflict_content = (
            "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> my-feature-branch\n"
        )
        (tmp_path / "conflict.py").write_text(conflict_content)

        regions = service.parse_conflict_markers(tmp_path, "conflict.py")

        assert regions[0]["ours_label"] == "HEAD"
        assert regions[0]["theirs_label"] == "my-feature-branch"

    def test_conflict_region_content(self, tmp_path: Path) -> None:
        """ours_content and theirs_content are captured correctly."""
        _create_clean_repo(tmp_path)
        service = _get_service()

        conflict_content = (
            "<<<<<<< HEAD\nalpha\nbeta\n=======\ngamma\ndelta\n>>>>>>> feature\n"
        )
        (tmp_path / "conflict.py").write_text(conflict_content)

        regions = service.parse_conflict_markers(tmp_path, "conflict.py")

        assert "alpha" in regions[0]["ours_content"]
        assert "beta" in regions[0]["ours_content"]
        assert "gamma" in regions[0]["theirs_content"]
        assert "delta" in regions[0]["theirs_content"]

    def test_no_markers_returns_empty(self, tmp_path: Path) -> None:
        """File without conflict markers returns empty list."""
        _create_clean_repo(tmp_path)
        service = _get_service()

        (tmp_path / "clean.py").write_text("just normal\npython code\n")

        regions = service.parse_conflict_markers(tmp_path, "clean.py")

        assert regions == []

    def test_binary_file_returns_empty(self, tmp_path: Path) -> None:
        """Binary file (non-UTF-8) returns empty list."""
        _create_clean_repo(tmp_path)
        service = _get_service()

        # Write actual binary content that cannot be decoded as UTF-8
        (tmp_path / "binary.bin").write_bytes(b"\x80\x81\x82\x83\xff\xfe")

        regions = service.parse_conflict_markers(tmp_path, "binary.bin")

        assert regions == []


# ---------------------------------------------------------------------------
# Tests for git_conflict_status
# ---------------------------------------------------------------------------


class TestGitConflictStatus:
    """Tests for git_conflict_status using real git repos."""

    def test_no_merge_in_progress(self, tmp_path: Path) -> None:
        """Clean repo returns in_merge=False and empty conflicts."""
        _create_clean_repo(tmp_path)
        service = _get_service()

        result = service.git_conflict_status(tmp_path)

        assert result["in_merge"] is False
        assert result["conflicted_files"] == []
        assert result["total_conflicts"] == 0

    def test_merge_with_conflicts(self, tmp_path: Path) -> None:
        """Repo in merge-conflict state returns in_merge=True with conflicted files."""
        _create_repo_with_conflict(tmp_path)
        service = _get_service()

        result = service.git_conflict_status(tmp_path)

        assert result["in_merge"] is True
        assert result["total_conflicts"] >= 1
        assert len(result["conflicted_files"]) >= 1

        # The conflicted file should have regions parsed
        conflict_file = result["conflicted_files"][0]
        assert "file" in conflict_file
        assert "status" in conflict_file
        assert "regions" in conflict_file
        assert "is_binary" in conflict_file

    def test_conflict_file_has_regions(self, tmp_path: Path) -> None:
        """Conflicted text file has non-empty regions."""
        _create_repo_with_conflict(tmp_path)
        service = _get_service()

        result = service.git_conflict_status(tmp_path)

        assert result["total_conflicts"] >= 1
        conflict_file = result["conflicted_files"][0]
        assert len(conflict_file["regions"]) >= 1
        assert conflict_file["is_binary"] is False

    def test_binary_conflict_is_binary_true(self, tmp_path: Path) -> None:
        """Binary conflicted file has is_binary=True and empty regions."""
        # Create repo with a binary file conflict
        _run(["git", "init", str(tmp_path)], tmp_path.parent)
        _run(
            ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
            tmp_path.parent,
        )
        _run(
            ["git", "-C", str(tmp_path), "config", "user.name", "Test"], tmp_path.parent
        )

        # Initial binary file
        (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02\x03")
        _run(["git", "-C", str(tmp_path), "add", "."], tmp_path.parent)
        _run(["git", "-C", str(tmp_path), "commit", "-m", "initial"], tmp_path.parent)

        result = subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--show-current"],
            capture_output=True,
            text=True,
        )
        default_branch = result.stdout.strip() or "master"

        # Feature branch with different binary content
        _run(["git", "-C", str(tmp_path), "checkout", "-b", "feature"], tmp_path.parent)
        (tmp_path / "data.bin").write_bytes(b"\xaa\xbb\xcc\xdd")
        _run(["git", "-C", str(tmp_path), "add", "."], tmp_path.parent)
        _run(
            ["git", "-C", str(tmp_path), "commit", "-m", "feature binary"],
            tmp_path.parent,
        )

        # Main branch with different binary content
        _run(["git", "-C", str(tmp_path), "checkout", default_branch], tmp_path.parent)
        (tmp_path / "data.bin").write_bytes(b"\x11\x22\x33\x44")
        _run(["git", "-C", str(tmp_path), "add", "."], tmp_path.parent)
        _run(
            ["git", "-C", str(tmp_path), "commit", "-m", "main binary"], tmp_path.parent
        )

        # Trigger merge conflict
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature"],
            capture_output=True,
            text=True,
        )

        service = _get_service()
        result = service.git_conflict_status(tmp_path)

        # Find binary file in conflicts
        binary_conflict = next(
            (f for f in result["conflicted_files"] if f["file"].endswith(".bin")),
            None,
        )
        if binary_conflict is not None:
            assert binary_conflict["is_binary"] is True
            assert binary_conflict["regions"] == []


# ---------------------------------------------------------------------------
# Tests for git_mark_resolved
# ---------------------------------------------------------------------------


class TestGitMarkResolved:
    """Tests for git_mark_resolved using real git repos."""

    def test_mark_resolved_stages_file(self, tmp_path: Path) -> None:
        """After resolving conflict markers, mark_resolved stages the file."""
        _create_repo_with_conflict(tmp_path)
        service = _get_service()

        # Find conflicted file
        status_result = service.git_conflict_status(tmp_path)
        assert status_result["total_conflicts"] >= 1
        conflicted_file = status_result["conflicted_files"][0]["file"]

        # Remove conflict markers manually (resolve the conflict)
        (tmp_path / conflicted_file).write_text("line1\nresolved\nline3\n")

        result = service.git_mark_resolved(tmp_path, conflicted_file)

        assert result["success"] is True
        assert result["file"] == conflicted_file

    def test_mark_resolved_remaining_count_decreases(self, tmp_path: Path) -> None:
        """After resolving one conflict, remaining_conflicts count is correct."""
        _create_repo_with_conflict(tmp_path)
        service = _get_service()

        status_result = service.git_conflict_status(tmp_path)
        initial_count = status_result["total_conflicts"]
        assert initial_count >= 1

        conflicted_file = status_result["conflicted_files"][0]["file"]
        # Remove conflict markers
        (tmp_path / conflicted_file).write_text("line1\nresolved\nline3\n")

        result = service.git_mark_resolved(tmp_path, conflicted_file)

        assert result["remaining_conflicts"] == initial_count - 1

    def test_mark_resolved_all_resolved_when_last_conflict(
        self, tmp_path: Path
    ) -> None:
        """When last conflict is resolved, all_resolved=True."""
        _create_repo_with_conflict(tmp_path)
        service = _get_service()

        status_result = service.git_conflict_status(tmp_path)
        # This helper creates exactly one conflict
        assert status_result["total_conflicts"] == 1

        conflicted_file = status_result["conflicted_files"][0]["file"]
        (tmp_path / conflicted_file).write_text("line1\nresolved\nline3\n")

        result = service.git_mark_resolved(tmp_path, conflicted_file)

        assert result["all_resolved"] is True
        assert "commit" in result["message"].lower()

    def test_reject_if_markers_still_present(self, tmp_path: Path) -> None:
        """ValueError raised when conflict markers are still in the file."""
        _create_repo_with_conflict(tmp_path)
        service = _get_service()

        status_result = service.git_conflict_status(tmp_path)
        conflicted_file = status_result["conflicted_files"][0]["file"]

        # Do NOT remove conflict markers - file still has <<<<<<< in it
        with pytest.raises(ValueError, match="conflict markers"):
            service.git_mark_resolved(tmp_path, conflicted_file)

    def test_reject_if_not_conflicted(self, tmp_path: Path) -> None:
        """ValueError raised when file is not in a conflicted state."""
        _create_clean_repo(tmp_path)
        service = _get_service()

        with pytest.raises(ValueError, match="not in a conflicted state"):
            service.git_mark_resolved(tmp_path, "file.py")


# ---------------------------------------------------------------------------
# Tests for _safe_repo_file_path
# ---------------------------------------------------------------------------


class TestSafeRepoFilePath:
    """Tests for path traversal protection."""

    def test_rejects_path_traversal(self, tmp_path: Path) -> None:
        """Paths escaping the repo boundary are rejected."""
        service = _get_service()
        with pytest.raises(ValueError, match="escapes repository boundary"):
            service._safe_repo_file_path(tmp_path, "../../etc/passwd")

    def test_allows_valid_relative_path(self, tmp_path: Path) -> None:
        """Valid relative paths within the repo are accepted."""
        service = _get_service()
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "file.py").write_text("content")
        result = service._safe_repo_file_path(tmp_path, "src/file.py")
        assert result == (tmp_path / "src" / "file.py").resolve()
