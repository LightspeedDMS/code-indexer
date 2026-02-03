"""
Integration tests for Git History Exploration Operations.

Story #11 - Git History and Exploration Operations Test Suite

Tests for:
- git_log (AC1): Return N most recent commits with filtering
- git_show_commit (AC2): Full commit details including message, author, changed files
- git_diff (AC3): Line-by-line changes between two revisions
- git_blame (AC4): Line attribution to commit hash, author, content
- git_file_at_revision (AC5): Retrieve file content at a specific commit
- git_file_history (AC6): List all commits that modified a specific file

Uses REAL git operations - NO Python mocks for git commands.
All tests use GitOperationsService from global_repos/git_operations.py.
"""

import subprocess
from pathlib import Path
from typing import List

import pytest

from code_indexer.global_repos.git_operations import GitOperationsService


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GIT_FULL_HASH_LENGTH = 40
GIT_SHORT_HASH_MIN_LENGTH = 7


# ---------------------------------------------------------------------------
# Test Helper Functions
# ---------------------------------------------------------------------------


def create_test_commits(repo_path: Path, count: int = 5) -> List[str]:
    """
    Create a series of test commits in the repository.

    Args:
        repo_path: Path to the git repository
        count: Number of commits to create

    Returns:
        List of commit hashes created (most recent first)
    """
    commit_hashes = []
    for i in range(count):
        test_file = repo_path / f"history_test_file_{i}.txt"
        test_file.write_text(f"Content for commit {i}\nLine 2 of file {i}\n")

        subprocess.run(
            ["git", "add", "."],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"Test commit {i} for history exploration"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )
        commit_hashes.insert(0, result.stdout.strip())

    return commit_hashes


# ---------------------------------------------------------------------------
# AC1: git_log Operation Tests
# ---------------------------------------------------------------------------


class TestGitLog:
    """Tests for git_log operation (AC1)."""

    def test_log_returns_recent_commits(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: git_log returns most recent commits with limit parameter.
        """
        created_hashes = create_test_commits(local_test_repo, count=5)

        service = GitOperationsService(local_test_repo)
        result = service.get_log(limit=3)

        assert len(result.commits) == 3
        assert result.commits[0].hash == created_hashes[0]
        assert result.commits[1].hash == created_hashes[1]
        assert result.commits[2].hash == created_hashes[2]

    def test_log_commit_includes_required_fields(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Each commit includes hash, message, author, date.
        """
        create_test_commits(local_test_repo, count=1)

        service = GitOperationsService(local_test_repo)
        result = service.get_log(limit=1)

        assert len(result.commits) == 1
        commit = result.commits[0]

        assert commit.hash is not None and len(commit.hash) == GIT_FULL_HASH_LENGTH
        assert (
            commit.short_hash is not None
            and len(commit.short_hash) >= GIT_SHORT_HASH_MIN_LENGTH
        )
        assert commit.author_name is not None
        assert commit.author_email is not None
        assert commit.author_date is not None
        assert commit.subject is not None
        assert "Test commit 0" in commit.subject

    def test_log_filter_by_path(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Filter by path shows only commits touching that file.
        """
        file_a = local_test_repo / "file_a.txt"
        file_a.write_text("Content A\n")
        subprocess.run(
            ["git", "add", "file_a.txt"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Add file_a"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        file_b = local_test_repo / "file_b.txt"
        file_b.write_text("Content B\n")
        subprocess.run(
            ["git", "add", "file_b.txt"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Add file_b"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        file_a.write_text("Content A modified\n")
        subprocess.run(
            ["git", "add", "file_a.txt"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Update file_a"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService(local_test_repo)

        result = service.get_log(path="file_a.txt")
        assert len(result.commits) == 2
        assert "file_a" in result.commits[0].subject
        assert "file_a" in result.commits[1].subject

        result_b = service.get_log(path="file_b.txt")
        assert len(result_b.commits) == 1
        assert "file_b" in result_b.commits[0].subject

    def test_log_filter_by_author(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Filter by author shows only commits by that author.
        """
        create_test_commits(local_test_repo, count=2)

        service = GitOperationsService(local_test_repo)

        result = service.get_log(author="test@example.com")
        assert len(result.commits) >= 2

        result_none = service.get_log(author="nonexistent@example.com")
        assert len(result_none.commits) == 0

    def test_log_respects_limit(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Limit parameter correctly restricts number of commits returned.
        """
        create_test_commits(local_test_repo, count=10)

        service = GitOperationsService(local_test_repo)

        result_5 = service.get_log(limit=5)
        assert len(result_5.commits) == 5

        result_1 = service.get_log(limit=1)
        assert len(result_1.commits) == 1

    def test_log_truncated_flag(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Truncated flag indicates when more commits exist.
        """
        create_test_commits(local_test_repo, count=5)

        service = GitOperationsService(local_test_repo)

        result_truncated = service.get_log(limit=2)
        assert result_truncated.truncated is True

        result_all = service.get_log(limit=100)
        assert result_all.truncated is False


# ---------------------------------------------------------------------------
# AC2: git_show_commit Operation Tests
# ---------------------------------------------------------------------------


class TestGitShowCommit:
    """Tests for git_show_commit operation (AC2)."""

    def test_show_commit_returns_full_details(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: show_commit returns full commit message, author, date.
        """
        created_hashes = create_test_commits(local_test_repo, count=1)

        service = GitOperationsService(local_test_repo)
        result = service.show_commit(created_hashes[0])

        assert result.commit.hash == created_hashes[0]
        assert result.commit.author_name is not None
        assert result.commit.author_email is not None
        assert result.commit.author_date is not None
        assert "Test commit 0" in result.commit.subject

    def test_show_commit_includes_changed_files(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: show_commit includes list of changed files with stats.
        """
        test_file = local_test_repo / "show_commit_test.txt"
        test_file.write_text("Initial content\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add show_commit_test.txt"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
            text=True,
        )
        commit_hash = result.stdout.strip()

        service = GitOperationsService(local_test_repo)
        result = service.show_commit(commit_hash, include_stats=True)

        assert result.stats is not None
        assert len(result.stats) >= 1
        file_paths = [s.path for s in result.stats]
        assert "show_commit_test.txt" in file_paths

    def test_show_commit_with_diff(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: show_commit can include full diff when requested.
        """
        test_file = local_test_repo / "diff_test.txt"
        test_file.write_text("Line 1\nLine 2\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add diff_test.txt"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
            text=True,
        )
        commit_hash = result.stdout.strip()

        service = GitOperationsService(local_test_repo)
        result = service.show_commit(commit_hash, include_diff=True)

        assert result.diff is not None
        assert "diff_test.txt" in result.diff
        assert "+Line 1" in result.diff

    def test_show_commit_returns_parents(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: show_commit returns parent commit hashes.
        """
        created_hashes = create_test_commits(local_test_repo, count=2)

        service = GitOperationsService(local_test_repo)
        result = service.show_commit(created_hashes[0])

        assert result.parents is not None
        assert len(result.parents) >= 1
        assert len(result.parents[0]) == GIT_FULL_HASH_LENGTH

    def test_show_commit_abbreviated_hash(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: show_commit works with abbreviated commit hash.
        """
        created_hashes = create_test_commits(local_test_repo, count=1)
        short_hash = created_hashes[0][:7]

        service = GitOperationsService(local_test_repo)
        result = service.show_commit(short_hash)

        assert result.commit.hash == created_hashes[0]

    def test_show_commit_invalid_hash_raises_error(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: show_commit raises ValueError for invalid commit hash.
        """
        service = GitOperationsService(local_test_repo)

        with pytest.raises(ValueError, match="Commit not found"):
            service.show_commit("nonexistent123456789")


# ---------------------------------------------------------------------------
# AC3: git_diff Operation Tests
# ---------------------------------------------------------------------------


class TestGitDiff:
    """Tests for git_diff operation (AC3)."""

    def test_diff_between_commits(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC3: get_diff shows line-by-line changes between two revisions.
        """
        test_file = local_test_repo / "diff_file.txt"
        test_file.write_text("Original content\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "First commit"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        first_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        test_file.write_text("Modified content\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Second commit"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        second_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        service = GitOperationsService(local_test_repo)
        result = service.get_diff(first_commit, second_commit)

        assert len(result.files) >= 1
        assert result.total_insertions >= 1
        assert result.total_deletions >= 1

    def test_diff_includes_hunks(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC3: get_diff includes diff hunks with line numbers.
        """
        test_file = local_test_repo / "hunk_file.txt"
        test_file.write_text("Line 1\nLine 2\nLine 3\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add hunk_file"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        first_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        test_file.write_text("Line 1\nModified Line 2\nLine 3\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Modify hunk_file"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        second_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        service = GitOperationsService(local_test_repo)
        result = service.get_diff(first_commit, second_commit)

        assert len(result.files) >= 1
        file_diff = result.files[0]
        assert len(file_diff.hunks) >= 1
        hunk = file_diff.hunks[0]
        assert hunk.old_start >= 1
        assert hunk.new_start >= 1

    def test_diff_file_status(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC3: get_diff correctly identifies file status (added, modified, deleted).
        """
        test_file = local_test_repo / "status_file.txt"
        test_file.write_text("Content\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add status_file"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        first_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        test_file.unlink()
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Delete status_file"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        second_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        service = GitOperationsService(local_test_repo)
        result = service.get_diff(first_commit, second_commit)

        assert len(result.files) >= 1
        file_diff = result.files[0]
        assert file_diff.status == "deleted"

    def test_diff_stat_only(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC3: get_diff with stat_only returns statistics without hunks.
        """
        test_file = local_test_repo / "stat_file.txt"
        test_file.write_text("Content\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add stat_file"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        first_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        test_file.write_text("Modified content\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Modify stat_file"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        second_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        service = GitOperationsService(local_test_repo)
        result = service.get_diff(first_commit, second_commit, stat_only=True)

        assert len(result.files) >= 1
        assert result.files[0].hunks == []
        assert result.stat_summary is not None

    def test_diff_filter_by_path(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC3: get_diff can filter by specific path.
        """
        file_a = local_test_repo / "filter_a.txt"
        file_b = local_test_repo / "filter_b.txt"
        file_a.write_text("A\n")
        file_b.write_text("B\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add files"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        first_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        file_a.write_text("A modified\n")
        file_b.write_text("B modified\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Modify files"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        second_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        service = GitOperationsService(local_test_repo)
        result = service.get_diff(first_commit, second_commit, path="filter_a.txt")

        assert len(result.files) == 1
        assert result.files[0].path == "filter_a.txt"


# ---------------------------------------------------------------------------
# AC4: git_blame Operation Tests
# ---------------------------------------------------------------------------


class TestGitBlame:
    """Tests for git_blame operation (AC4)."""

    def test_blame_returns_line_attribution(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC4: get_blame attributes each line to commit hash, author, content.
        """
        test_file = local_test_repo / "blame_test.txt"
        test_file.write_text("Line 1\nLine 2\nLine 3\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add blame_test.txt"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService(local_test_repo)
        result = service.get_blame("blame_test.txt")

        assert len(result.lines) == 3
        for line in result.lines:
            assert line.commit_hash is not None
            assert len(line.commit_hash) == GIT_FULL_HASH_LENGTH
            assert line.author_name is not None
            assert line.content is not None

    def test_blame_line_numbers(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC4: get_blame returns correct line numbers.
        """
        test_file = local_test_repo / "blame_lines.txt"
        test_file.write_text("First\nSecond\nThird\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add blame_lines.txt"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService(local_test_repo)
        result = service.get_blame("blame_lines.txt")

        assert result.lines[0].line_number == 1
        assert result.lines[0].content == "First"
        assert result.lines[1].line_number == 2
        assert result.lines[1].content == "Second"
        assert result.lines[2].line_number == 3
        assert result.lines[2].content == "Third"

    def test_blame_with_line_range(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC4: get_blame can limit to specific line range.
        """
        test_file = local_test_repo / "blame_range.txt"
        test_file.write_text("Line 1\nLine 2\nLine 3\nLine 4\nLine 5\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add blame_range.txt"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService(local_test_repo)
        result = service.get_blame("blame_range.txt", start_line=2, end_line=4)

        assert len(result.lines) == 3
        assert result.lines[0].line_number == 2
        assert result.lines[2].line_number == 4

    def test_blame_unique_commits_count(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC4: get_blame returns count of unique commits.
        """
        test_file = local_test_repo / "blame_unique.txt"
        test_file.write_text("Initial line\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "First commit"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        test_file.write_text("Initial line\nSecond line\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Second commit"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService(local_test_repo)
        result = service.get_blame("blame_unique.txt")

        assert result.unique_commits == 2

    def test_blame_invalid_file_raises_error(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC4: get_blame raises ValueError for non-existent file.
        """
        service = GitOperationsService(local_test_repo)

        with pytest.raises(ValueError, match="File not found"):
            service.get_blame("nonexistent_file.txt")


# ---------------------------------------------------------------------------
# AC5: git_file_at_revision Operation Tests
# ---------------------------------------------------------------------------


class TestGitFileAtRevision:
    """Tests for git_file_at_revision operation (AC5)."""

    def test_file_at_revision_returns_content(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC5: get_file_at_revision retrieves file content at specific commit.
        """
        test_file = local_test_repo / "revision_test.txt"
        original_content = "Original content\n"
        test_file.write_text(original_content)
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "First version"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        first_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        test_file.write_text("Modified content\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Second version"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService(local_test_repo)
        result = service.get_file_at_revision("revision_test.txt", first_commit)

        assert result.content == original_content

    def test_file_at_revision_with_branch_name(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC5: get_file_at_revision works with branch names as revision.
        """
        test_file = local_test_repo / "branch_test.txt"
        test_file.write_text("Main branch content\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add branch_test"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService(local_test_repo)
        result = service.get_file_at_revision("branch_test.txt", "HEAD")

        assert result.content == "Main branch content\n"
        assert result.resolved_revision is not None
        assert len(result.resolved_revision) == GIT_FULL_HASH_LENGTH

    def test_file_at_revision_returns_size(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC5: get_file_at_revision returns file size in bytes.
        """
        test_file = local_test_repo / "size_test.txt"
        content = "Hello World\n"
        test_file.write_text(content)
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add size_test"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService(local_test_repo)
        result = service.get_file_at_revision("size_test.txt", "HEAD")

        assert result.size_bytes == len(content.encode("utf-8"))

    def test_file_at_revision_invalid_file_raises_error(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC5: get_file_at_revision raises ValueError for non-existent file.
        """
        create_test_commits(local_test_repo, count=1)

        service = GitOperationsService(local_test_repo)

        with pytest.raises(ValueError, match="File not found"):
            service.get_file_at_revision("nonexistent.txt", "HEAD")

    def test_file_at_revision_invalid_revision_raises_error(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC5: get_file_at_revision raises ValueError for invalid revision.
        """
        test_file = local_test_repo / "valid_file.txt"
        test_file.write_text("Content\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add valid_file"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService(local_test_repo)

        with pytest.raises(ValueError, match="Invalid revision"):
            service.get_file_at_revision("valid_file.txt", "nonexistent_revision")


# ---------------------------------------------------------------------------
# AC6: git_file_history Operation Tests
# ---------------------------------------------------------------------------


class TestGitFileHistory:
    """Tests for git_file_history operation (AC6)."""

    def test_file_history_lists_commits(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC6: get_file_history lists all commits that modified a specific file.
        """
        test_file = local_test_repo / "history_file.txt"
        test_file.write_text("Version 1\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "First version"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        test_file.write_text("Version 2\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Second version"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        test_file.write_text("Version 3\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Third version"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService(local_test_repo)
        result = service.get_file_history("history_file.txt")

        assert len(result.commits) == 3
        assert result.path == "history_file.txt"

    def test_file_history_includes_stats(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC6: get_file_history includes insertions and deletions for each commit.
        """
        test_file = local_test_repo / "stats_file.txt"
        test_file.write_text("Line 1\nLine 2\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add stats_file"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        test_file.write_text("Line 1\nModified Line 2\nLine 3\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Modify stats_file"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService(local_test_repo)
        result = service.get_file_history("stats_file.txt")

        assert len(result.commits) == 2
        latest_commit = result.commits[0]
        assert latest_commit.insertions >= 1 or latest_commit.deletions >= 1

    def test_file_history_truncation(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC6: get_file_history truncates when limit is reached.
        """
        test_file = local_test_repo / "truncate_file.txt"
        for i in range(5):
            test_file.write_text(f"Version {i}\n")
            subprocess.run(
                ["git", "add", "."],
                cwd=local_test_repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", f"Version {i}"],
                cwd=local_test_repo,
                check=True,
                capture_output=True,
            )

        service = GitOperationsService(local_test_repo)

        result_limited = service.get_file_history("truncate_file.txt", limit=2)
        assert len(result_limited.commits) == 2
        assert result_limited.truncated is True

        result_all = service.get_file_history("truncate_file.txt", limit=100)
        assert result_all.truncated is False

    def test_file_history_commit_details(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC6: get_file_history returns commit details including hash, author, date.
        """
        test_file = local_test_repo / "detail_file.txt"
        test_file.write_text("Content\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add detail_file"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService(local_test_repo)
        result = service.get_file_history("detail_file.txt")

        assert len(result.commits) >= 1
        commit = result.commits[0]
        assert commit.hash is not None
        assert len(commit.hash) == GIT_FULL_HASH_LENGTH
        assert commit.short_hash is not None
        assert commit.author_name is not None
        assert commit.author_date is not None
        assert commit.subject is not None

    def test_file_history_nonexistent_file(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC6: get_file_history returns empty result for non-existent file.
        """
        create_test_commits(local_test_repo, count=1)

        service = GitOperationsService(local_test_repo)
        result = service.get_file_history("nonexistent_file.txt")

        assert len(result.commits) == 0
        assert result.total_count == 0
