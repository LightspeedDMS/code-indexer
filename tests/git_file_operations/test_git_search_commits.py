"""
Git Search Commits Test Suite - Story #12, AC1.

Tests for git_search_commits: Search commit messages with query, author filter, date filter.

Uses REAL git operations - NO Python mocks for git commands.
All tests use GitOperationsService from global_repos/git_operations.py.
"""

import subprocess
from pathlib import Path
from typing import List


from code_indexer.global_repos.git_operations import GitOperationsService


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GIT_FULL_HASH_LENGTH = 40
GIT_SHORT_HASH_MIN_LENGTH = 7


# ---------------------------------------------------------------------------
# Test Helper Functions
# ---------------------------------------------------------------------------


def create_searchable_commits(
    repo_path: Path, prefix: str, count: int = 3
) -> List[str]:
    """
    Create commits with searchable messages containing the prefix.

    Args:
        repo_path: Path to the git repository
        prefix: Prefix to use in commit messages (for searching)
        count: Number of commits to create

    Returns:
        List of commit hashes created (most recent first)
    """
    commit_hashes = []
    for i in range(count):
        test_file = repo_path / f"search_commit_{prefix}_{i}.txt"
        test_file.write_text(f"Content for {prefix} commit {i}\n")

        subprocess.run(
            ["git", "add", "."],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"{prefix} searchable commit number {i}"],
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
# AC1: git_search_commits Operation Tests
# ---------------------------------------------------------------------------


class TestGitSearchCommits:
    """Tests for git_search_commits operation (AC1)."""

    def test_search_commits_by_message(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Search for commits with specific message text.

        The initial commit contains 'Initial commit'.
        """
        service = GitOperationsService(local_test_repo)
        result = service.search_commits(query="Initial")

        assert len(result.matches) >= 1
        assert any("Initial" in m.subject for m in result.matches)

    def test_search_commits_returns_result_object(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: search_commits returns CommitSearchResult with proper fields.
        """
        create_searchable_commits(local_test_repo, "TESTQUERY", count=1)

        service = GitOperationsService(local_test_repo)
        result = service.search_commits(query="TESTQUERY")

        # Verify result structure
        assert result.query == "TESTQUERY"
        assert isinstance(result.is_regex, bool)
        assert isinstance(result.matches, list)
        assert isinstance(result.total_matches, int)
        assert isinstance(result.truncated, bool)
        assert isinstance(result.search_time_ms, float)

    def test_search_commits_no_match(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Search returns empty when no commits match.
        """
        service = GitOperationsService(local_test_repo)
        result = service.search_commits(query="XYZNONEXISTENT123456789")

        assert len(result.matches) == 0
        assert result.total_matches == 0

    def test_search_commits_match_fields(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Each match includes required fields (hash, author, date, subject).
        """
        create_searchable_commits(local_test_repo, "FIELDCHECK", count=1)

        service = GitOperationsService(local_test_repo)
        result = service.search_commits(query="FIELDCHECK")

        assert len(result.matches) >= 1
        match = result.matches[0]

        assert match.hash is not None
        assert len(match.hash) == GIT_FULL_HASH_LENGTH
        assert match.short_hash is not None
        assert len(match.short_hash) >= GIT_SHORT_HASH_MIN_LENGTH
        assert match.author_name is not None
        assert match.author_email is not None
        assert match.author_date is not None
        assert match.subject is not None
        assert "FIELDCHECK" in match.subject

    def test_search_commits_with_author_filter(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Filter search results by author.
        """
        create_searchable_commits(local_test_repo, "AUTHORTEST", count=2)

        service = GitOperationsService(local_test_repo)

        # Search with the test user configured in conftest
        result = service.search_commits(query="AUTHORTEST", author="Test User")

        assert len(result.matches) >= 1
        # All matches should be from the filtered author
        for match in result.matches:
            assert (
                "Test User" in match.author_name or "test" in match.author_name.lower()
            )

    def test_search_commits_author_filter_no_match(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Author filter returns empty for non-existent author.
        """
        create_searchable_commits(local_test_repo, "AUTHORFILTER", count=2)

        service = GitOperationsService(local_test_repo)

        # Search with non-existent author
        result = service.search_commits(
            query="AUTHORFILTER", author="nonexistent.author@nowhere.invalid"
        )

        assert len(result.matches) == 0

    def test_search_commits_with_regex(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Search using regex pattern.
        """
        service = GitOperationsService(local_test_repo)
        result = service.search_commits(query="Initial.*commit", is_regex=True)

        assert result.is_regex is True
        assert len(result.matches) >= 1
        # Should find the initial commit
        assert any(
            "Initial" in m.subject and "commit" in m.subject for m in result.matches
        )

    def test_search_commits_regex_alternation(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Regex with alternation pattern works.
        """
        # Create commits with different keywords
        test_file1 = local_test_repo / "regex_feature.txt"
        test_file1.write_text("feature content\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add feature for regex testing"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        test_file2 = local_test_repo / "regex_bugfix.txt"
        test_file2.write_text("bugfix content\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Fix bug for regex testing"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService(local_test_repo)
        # Search for either "feature" or "Fix bug"
        result = service.search_commits(query="(feature|Fix bug)", is_regex=True)

        assert len(result.matches) >= 2

    def test_search_commits_with_limit(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Limit number of results returned.
        """
        # Create multiple commits to search
        create_searchable_commits(local_test_repo, "LIMITCHECK", count=5)

        service = GitOperationsService(local_test_repo)
        result = service.search_commits(query="LIMITCHECK", limit=2)

        assert len(result.matches) <= 2
        assert result.truncated is True

    def test_search_commits_truncated_flag(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Truncated flag indicates when more commits exist.
        """
        create_searchable_commits(local_test_repo, "TRUNCATE", count=5)

        service = GitOperationsService(local_test_repo)

        # With low limit - should be truncated
        result_limited = service.search_commits(query="TRUNCATE", limit=2)
        assert result_limited.truncated is True

        # With high limit - should not be truncated
        result_all = service.search_commits(query="TRUNCATE", limit=100)
        assert result_all.truncated is False

    def test_search_commits_with_since_date_filter(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Filter by date range (since).
        """
        create_searchable_commits(local_test_repo, "DATETEST", count=2)

        service = GitOperationsService(local_test_repo)

        # Use a date in the future - should return nothing
        result = service.search_commits(query="DATETEST", since="2099-01-01")
        assert len(result.matches) == 0

    def test_search_commits_with_until_date_filter(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Filter by date range (until).
        """
        create_searchable_commits(local_test_repo, "UNTILTEST", count=2)

        service = GitOperationsService(local_test_repo)

        # Use a date in the past - should return nothing
        result = service.search_commits(query="UNTILTEST", until="1990-01-01")
        assert len(result.matches) == 0

    def test_search_commits_case_insensitive(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Search is case-insensitive by default.
        """
        # Create commit with specific case
        test_file = local_test_repo / "case_test.txt"
        test_file.write_text("case test content\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "UPPERCASE message for CaseTest"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService(local_test_repo)

        # Search with different case
        result = service.search_commits(query="casetest")

        assert len(result.matches) >= 1
        assert any("CaseTest" in m.subject for m in result.matches)

    def test_search_commits_match_highlights(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Matches include highlighted text from subject/body.
        """
        # Create commit with body
        test_file = local_test_repo / "highlight_test.txt"
        test_file.write_text("highlight content\n")
        subprocess.run(
            ["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "HIGHLIGHT subject line"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService(local_test_repo)
        result = service.search_commits(query="HIGHLIGHT")

        assert len(result.matches) >= 1
        match = result.matches[0]
        assert isinstance(match.match_highlights, list)
        # Should have at least the subject line as a highlight
        assert len(match.match_highlights) >= 1

    def test_search_commits_timing_tracked(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Search time is tracked in milliseconds.
        """
        service = GitOperationsService(local_test_repo)
        result = service.search_commits(query="commit")

        assert result.search_time_ms >= 0
        assert isinstance(result.search_time_ms, float)
