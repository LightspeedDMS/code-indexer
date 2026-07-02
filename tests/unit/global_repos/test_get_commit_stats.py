"""Unit tests for GitOperationsService._get_commit_stats (Bug #698).

Bug #698: git_show_commit stats always return insertions=0/deletions=0 because
combining --numstat and --name-status in a single git show command silently
suppresses numstat output.

Fix: Run two separate git show commands (one for --numstat, one for --name-status)
and merge their results, matching the pattern already used in get_diff().

Tests use REAL git repos in tmp_path — no mocks.
"""

import subprocess
from pathlib import Path

import pytest


def _init_git_repo(repo_path: Path) -> None:
    """Initialize a git repo with test user identity."""
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )


def _git_commit(repo_path: Path, message: str) -> str:
    """Create a commit and return its full hash as a str."""
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal git repo with one initial commit."""
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    # Need an initial commit so subsequent commits have a parent
    (repo_path / "README.md").write_text("init\n")
    subprocess.run(
        ["git", "add", "README.md"], cwd=repo_path, capture_output=True, check=True
    )
    _git_commit(repo_path, "initial")
    return repo_path


class TestGetCommitStatsInsertionsAndDeletions:
    """Bug #698: verify insertions and deletions are non-zero for real changes."""

    def test_commit_stats_returns_correct_insertions_deletions(self, git_repo):
        """A commit that adds text files must report correct insertion counts."""
        from code_indexer.global_repos.git_operations import GitOperationsService

        # Add two new files with known line counts
        (git_repo / "file1.py").write_text("line1\nline2\nline3\n")
        (git_repo / "file2.py").write_text("a\nb\nc\nd\ne\n")
        subprocess.run(
            ["git", "add", "file1.py", "file2.py"],
            cwd=git_repo,
            capture_output=True,
            check=True,
        )
        commit_hash = _git_commit(git_repo, "add files")

        svc = GitOperationsService(git_repo)
        stats = svc._get_commit_stats(commit_hash)

        paths = {s.path: s for s in stats}
        assert "file1.py" in paths, f"file1.py missing from {list(paths.keys())}"
        assert "file2.py" in paths, f"file2.py missing from {list(paths.keys())}"

        # file1.py: 3 insertions (new file), 0 deletions
        assert paths["file1.py"].insertions == 3, (
            f"expected 3 insertions for file1.py but got {paths['file1.py'].insertions}"
        )
        assert paths["file1.py"].deletions == 0

        # file2.py: 5 insertions (new file), 0 deletions
        assert paths["file2.py"].insertions == 5, (
            f"expected 5 insertions for file2.py but got {paths['file2.py'].insertions}"
        )
        assert paths["file2.py"].deletions == 0

    def test_commit_stats_modification_insertions_deletions(self, git_repo):
        """A commit replacing lines must report correct insertion and deletion counts."""
        from code_indexer.global_repos.git_operations import GitOperationsService

        # Create file with 4 lines in first commit
        (git_repo / "modify.py").write_text("a\nb\nc\nd\n")
        subprocess.run(
            ["git", "add", "modify.py"], cwd=git_repo, capture_output=True, check=True
        )
        _git_commit(git_repo, "create modify.py")

        # Replace all 4 lines with 2 new lines: deletions=4, insertions=2
        (git_repo / "modify.py").write_text("x\ny\n")
        subprocess.run(
            ["git", "add", "modify.py"], cwd=git_repo, capture_output=True, check=True
        )
        commit_hash = _git_commit(git_repo, "modify file")

        svc = GitOperationsService(git_repo)
        stats = svc._get_commit_stats(commit_hash)

        assert len(stats) == 1
        stat = stats[0]
        assert stat.path == "modify.py"
        assert stat.insertions == 2, f"expected 2 insertions but got {stat.insertions}"
        assert stat.deletions == 4, f"expected 4 deletions but got {stat.deletions}"


class TestGetCommitStatsStatus:
    """Verify status field for added, deleted, and renamed files."""

    def test_commit_stats_added_file(self, git_repo):
        """A commit adding a new file must have status='added' and insertions > 0."""
        from code_indexer.global_repos.git_operations import GitOperationsService

        (git_repo / "new_file.py").write_text("hello\nworld\n")
        subprocess.run(
            ["git", "add", "new_file.py"],
            cwd=git_repo,
            capture_output=True,
            check=True,
        )
        commit_hash = _git_commit(git_repo, "add new_file.py")

        svc = GitOperationsService(git_repo)
        stats = svc._get_commit_stats(commit_hash)

        stat = next((s for s in stats if s.path == "new_file.py"), None)
        assert stat is not None, "new_file.py not found in stats"
        assert stat.status == "added", (
            f"expected status='added' but got '{stat.status}'"
        )
        assert stat.insertions > 0, "added file must have insertions > 0"
        assert stat.deletions == 0

    def test_commit_stats_deleted_file(self, git_repo):
        """A commit deleting a file must have status='deleted' and deletions > 0."""
        from code_indexer.global_repos.git_operations import GitOperationsService

        # Create file first
        (git_repo / "to_delete.py").write_text("remove me\nand me\n")
        subprocess.run(
            ["git", "add", "to_delete.py"],
            cwd=git_repo,
            capture_output=True,
            check=True,
        )
        _git_commit(git_repo, "create to_delete.py")

        # Delete the file
        subprocess.run(
            ["git", "rm", "to_delete.py"], cwd=git_repo, capture_output=True, check=True
        )
        commit_hash = _git_commit(git_repo, "delete to_delete.py")

        svc = GitOperationsService(git_repo)
        stats = svc._get_commit_stats(commit_hash)

        stat = next((s for s in stats if s.path == "to_delete.py"), None)
        assert stat is not None, "to_delete.py not found in stats"
        assert stat.status == "deleted", (
            f"expected status='deleted' but got '{stat.status}'"
        )
        assert stat.deletions > 0, "deleted file must have deletions > 0"

    def test_commit_stats_renamed_file(self, git_repo):
        """A commit renaming a file must have status='renamed' and sensible counts."""
        from code_indexer.global_repos.git_operations import GitOperationsService

        # Create file to rename
        (git_repo / "old_name.py").write_text("content\n")
        subprocess.run(
            ["git", "add", "old_name.py"],
            cwd=git_repo,
            capture_output=True,
            check=True,
        )
        _git_commit(git_repo, "create old_name.py")

        # Rename via git mv
        subprocess.run(
            ["git", "mv", "old_name.py", "new_name.py"],
            cwd=git_repo,
            capture_output=True,
            check=True,
        )
        commit_hash = _git_commit(git_repo, "rename to new_name.py")

        svc = GitOperationsService(git_repo)
        stats = svc._get_commit_stats(commit_hash)

        # The renamed file should appear under the new name
        stat = next(
            (s for s in stats if "new_name.py" in s.path or "old_name.py" in s.path),
            None,
        )
        assert stat is not None, f"renamed file not found in stats: {stats}"
        assert stat.status == "renamed", (
            f"expected status='renamed' but got '{stat.status}'"
        )
        # Pure rename with no content change: 0 insertions, 0 deletions
        assert stat.insertions == 0
        assert stat.deletions == 0


class TestGetCommitStatsBinaryFile:
    """Verify binary files don't crash and report 0/0."""

    def test_commit_stats_binary_file(self, git_repo):
        """A commit adding a binary file must not crash; insertions/deletions should be 0."""
        from code_indexer.global_repos.git_operations import GitOperationsService

        # Write actual binary bytes (PNG magic bytes + garbage)
        binary_data = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
        (git_repo / "image.png").write_bytes(binary_data)
        subprocess.run(
            ["git", "add", "image.png"], cwd=git_repo, capture_output=True, check=True
        )
        commit_hash = _git_commit(git_repo, "add binary image.png")

        svc = GitOperationsService(git_repo)
        # Must not raise
        stats = svc._get_commit_stats(commit_hash)

        stat = next((s for s in stats if s.path == "image.png"), None)
        assert stat is not None, "image.png not found in stats"
        # Binary files report '-\t-\tpath' in numstat → 0/0
        assert stat.insertions == 0
        assert stat.deletions == 0


class TestGetCommitStatsEdgeCases:
    """Edge cases: multiple files with mixed status, empty commit."""

    def test_commit_stats_multiple_files_with_mixed_status(self, git_repo):
        """A commit adding one file and modifying another must return both with correct status."""
        from code_indexer.global_repos.git_operations import GitOperationsService

        # Create existing file
        (git_repo / "existing.py").write_text("original\n")
        subprocess.run(
            ["git", "add", "existing.py"], cwd=git_repo, capture_output=True, check=True
        )
        _git_commit(git_repo, "create existing.py")

        # Modify existing and add a new file in the same commit
        (git_repo / "existing.py").write_text("modified\n")
        (git_repo / "brand_new.py").write_text("new content\n")
        subprocess.run(
            ["git", "add", "existing.py", "brand_new.py"],
            cwd=git_repo,
            capture_output=True,
            check=True,
        )
        commit_hash = _git_commit(git_repo, "mixed commit")

        svc = GitOperationsService(git_repo)
        stats = svc._get_commit_stats(commit_hash)

        paths = {s.path: s for s in stats}
        assert "existing.py" in paths, f"existing.py missing from {list(paths.keys())}"
        assert "brand_new.py" in paths, (
            f"brand_new.py missing from {list(paths.keys())}"
        )

        assert paths["existing.py"].status == "modified"
        assert paths["brand_new.py"].status == "added"

        # existing.py: 1 insertion ("modified"), 1 deletion ("original")
        assert paths["existing.py"].insertions == 1
        assert paths["existing.py"].deletions == 1

        # brand_new.py: 1 insertion, 0 deletions
        assert paths["brand_new.py"].insertions == 1
        assert paths["brand_new.py"].deletions == 0

    def test_commit_stats_empty_commit_returns_empty_list(self, git_repo):
        """An empty commit (--allow-empty) must return an empty stats list."""
        from code_indexer.global_repos.git_operations import GitOperationsService

        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "empty commit"],
            cwd=git_repo,
            capture_output=True,
            check=True,
        )
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_repo,
            capture_output=True,
            check=True,
            text=True,
        )
        commit_hash = result.stdout.strip()

        svc = GitOperationsService(git_repo)
        stats = svc._get_commit_stats(commit_hash)

        assert stats == [], f"empty commit should return empty list but got {stats}"
