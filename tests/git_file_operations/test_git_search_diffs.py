"""
Git Search Diffs (Pickaxe) Test Suite - Story #12, AC2.

Tests for git_search_diffs: Find commits where code was added/removed, support regex patterns.

Uses REAL git operations - NO Python mocks for git commands.
All tests use GitOperationsService from global_repos/git_operations.py.

Note: Pickaxe tests are marked with @pytest.mark.slow as they scan git history.
"""

import subprocess
from pathlib import Path

import pytest

from code_indexer.global_repos.git_operations import GitOperationsService


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GIT_FULL_HASH_LENGTH = 40
GIT_SHORT_HASH_MIN_LENGTH = 7


# ---------------------------------------------------------------------------
# AC2: git_search_diffs (Pickaxe) Operation Tests
# ---------------------------------------------------------------------------


class TestGitSearchDiffs:
    """Tests for git_search_diffs (pickaxe) operation (AC2)."""

    @pytest.mark.slow
    def test_search_diffs_literal_string(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Find commit that added specific string (pickaxe -S).
        """
        # Create a file with unique content and commit
        unique_content = "UNIQUE_PICKAXE_TEST_STRING_12345"
        test_file = local_test_repo / "pickaxe_test.txt"
        test_file.write_text(f"Contains {unique_content} in content\n")

        subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add pickaxe test file"],
            cwd=local_test_repo, check=True, capture_output=True
        )

        service = GitOperationsService(local_test_repo)
        result = service.search_diffs(search_string=unique_content)

        assert len(result.matches) >= 1
        # The commit that added this string should be found
        assert any("pickaxe" in m.subject.lower() for m in result.matches)

    @pytest.mark.slow
    def test_search_diffs_returns_result_object(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: search_diffs returns DiffSearchResult with proper fields.
        """
        unique_content = "RESULT_OBJECT_TEST_STRING"
        test_file = local_test_repo / "result_object_test.txt"
        test_file.write_text(f"Contains {unique_content}\n")

        subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add result object test file"],
            cwd=local_test_repo, check=True, capture_output=True
        )

        service = GitOperationsService(local_test_repo)
        result = service.search_diffs(search_string=unique_content)

        # Verify result structure
        assert result.search_term == unique_content
        assert isinstance(result.is_regex, bool)
        assert isinstance(result.matches, list)
        assert isinstance(result.total_matches, int)
        assert isinstance(result.truncated, bool)
        assert isinstance(result.search_time_ms, float)

    @pytest.mark.slow
    def test_search_diffs_no_match(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Pickaxe returns empty when string never existed.
        """
        service = GitOperationsService(local_test_repo)
        result = service.search_diffs(search_string="NONEXISTENT_STRING_NEVER_USED_xyz987abc")

        assert len(result.matches) == 0
        assert result.total_matches == 0

    @pytest.mark.slow
    def test_search_diffs_match_fields(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Each match includes required fields.
        """
        unique_content = "MATCH_FIELDS_CHECK_STRING"
        test_file = local_test_repo / "match_fields_test.txt"
        test_file.write_text(f"Contains {unique_content}\n")

        subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add match fields test file"],
            cwd=local_test_repo, check=True, capture_output=True
        )

        service = GitOperationsService(local_test_repo)
        result = service.search_diffs(search_string=unique_content)

        assert len(result.matches) >= 1
        match = result.matches[0]

        assert match.hash is not None
        assert len(match.hash) == GIT_FULL_HASH_LENGTH
        assert match.short_hash is not None
        assert len(match.short_hash) >= GIT_SHORT_HASH_MIN_LENGTH
        assert match.author_name is not None
        assert match.author_date is not None
        assert match.subject is not None
        assert isinstance(match.files_changed, list)

    @pytest.mark.slow
    def test_search_diffs_with_regex_pattern(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Search using regex pattern (-G flag).
        """
        # Create content with a pattern
        test_file = local_test_repo / "regex_test.txt"
        test_file.write_text("function calculateTotal(items) { return sum; }\n")

        subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add regex test function"],
            cwd=local_test_repo, check=True, capture_output=True
        )

        service = GitOperationsService(local_test_repo)
        result = service.search_diffs(search_pattern="calculate.*Total", is_regex=True)

        assert result.is_regex is True
        assert len(result.matches) >= 1

    @pytest.mark.slow
    def test_search_diffs_regex_vs_literal(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Regex search (-G) differs from literal search (-S).
        """
        # Create content with regex metacharacters
        test_file = local_test_repo / "regex_meta_test.txt"
        test_file.write_text("test.value = getValue()\n")

        subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add regex meta test"],
            cwd=local_test_repo, check=True, capture_output=True
        )

        service = GitOperationsService(local_test_repo)

        # Literal search (dot is literal)
        result_literal = service.search_diffs(search_string="test.value")
        assert len(result_literal.matches) >= 1

        # Regex search (dot matches any character)
        result_regex = service.search_diffs(search_pattern="test.value", is_regex=True)
        assert len(result_regex.matches) >= 1

    @pytest.mark.slow
    def test_search_diffs_with_path_filter(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Filter pickaxe search to specific path.
        """
        # Create files in different directories
        subdir = local_test_repo / "subdir"
        subdir.mkdir(exist_ok=True)

        (subdir / "specific.txt").write_text("SPECIFIC_PATH_CONTENT_12345\n")
        (local_test_repo / "other.txt").write_text("OTHER_PATH_CONTENT_67890\n")

        subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add files in different paths"],
            cwd=local_test_repo, check=True, capture_output=True
        )

        service = GitOperationsService(local_test_repo)

        # Search with path filter
        result = service.search_diffs(
            search_string="SPECIFIC_PATH_CONTENT",
            path="subdir/"
        )

        assert len(result.matches) >= 1
        # Verify the commit touches the subdir
        for match in result.matches:
            assert any("subdir" in f for f in match.files_changed)

    def test_search_diffs_mutual_exclusion_error(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Error when both search_string and search_pattern provided.
        """
        service = GitOperationsService(local_test_repo)

        with pytest.raises(ValueError, match="mutually exclusive"):
            service.search_diffs(
                search_string="literal",
                search_pattern="regex.*"
            )

    def test_search_diffs_missing_param_error(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Error when neither search_string nor search_pattern provided.
        """
        service = GitOperationsService(local_test_repo)

        with pytest.raises(ValueError, match="Must provide"):
            service.search_diffs()

    @pytest.mark.slow
    def test_search_diffs_with_limit(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Limit number of pickaxe results.
        """
        # Create multiple commits with similar content
        for i in range(5):
            test_file = local_test_repo / f"limit_test_{i}.txt"
            test_file.write_text(f"LIMIT_TEST_CONTENT_{i}_UNIQUE\n")
            subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", f"Limit test commit {i}"],
                cwd=local_test_repo, check=True, capture_output=True
            )

        service = GitOperationsService(local_test_repo)
        result = service.search_diffs(
            search_string="LIMIT_TEST_CONTENT",
            limit=2
        )

        assert len(result.matches) <= 2

    @pytest.mark.slow
    def test_search_diffs_truncated_flag(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Truncated flag indicates when more commits exist.
        """
        # Create multiple commits with searchable content
        for i in range(5):
            test_file = local_test_repo / f"truncate_diff_{i}.txt"
            test_file.write_text(f"TRUNCATE_DIFF_CONTENT_{i}\n")
            subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", f"Truncate diff test {i}"],
                cwd=local_test_repo, check=True, capture_output=True
            )

        service = GitOperationsService(local_test_repo)

        # With low limit - should be truncated
        result_limited = service.search_diffs(
            search_string="TRUNCATE_DIFF_CONTENT",
            limit=2
        )
        assert result_limited.truncated is True

        # With high limit - should not be truncated
        result_all = service.search_diffs(
            search_string="TRUNCATE_DIFF_CONTENT",
            limit=100
        )
        assert result_all.truncated is False

    @pytest.mark.slow
    def test_search_diffs_detects_removed_code(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Pickaxe detects when code was removed.
        """
        # First add a file
        test_file = local_test_repo / "to_remove.txt"
        test_file.write_text("CONTENT_TO_BE_REMOVED_XYZ\n")

        subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add file to remove"],
            cwd=local_test_repo, check=True, capture_output=True
        )

        # Now remove the file
        test_file.unlink()
        subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Remove the file"],
            cwd=local_test_repo, check=True, capture_output=True
        )

        service = GitOperationsService(local_test_repo)
        result = service.search_diffs(search_string="CONTENT_TO_BE_REMOVED_XYZ")

        # Should find both the add and remove commits
        assert len(result.matches) >= 2
        subjects = [m.subject.lower() for m in result.matches]
        assert any("add" in s for s in subjects)
        assert any("remove" in s for s in subjects)

    @pytest.mark.slow
    def test_search_diffs_detects_modified_code(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Pickaxe detects when code was modified.
        """
        # Create file with original content
        test_file = local_test_repo / "modify_test.txt"
        test_file.write_text("ORIGINAL_MODIFY_CONTENT_123\n")

        subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add original content"],
            cwd=local_test_repo, check=True, capture_output=True
        )

        # Modify the file (replace content)
        test_file.write_text("MODIFIED_CONTENT_456\n")
        subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Modify the content"],
            cwd=local_test_repo, check=True, capture_output=True
        )

        service = GitOperationsService(local_test_repo)

        # Search for original content (should find add and remove/modify)
        result = service.search_diffs(search_string="ORIGINAL_MODIFY_CONTENT_123")
        assert len(result.matches) >= 2

    @pytest.mark.slow
    def test_search_diffs_with_since_filter(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Filter pickaxe search by date (since).
        """
        unique_content = "SINCE_FILTER_TEST_CONTENT"
        test_file = local_test_repo / "since_test.txt"
        test_file.write_text(f"{unique_content}\n")

        subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add since test file"],
            cwd=local_test_repo, check=True, capture_output=True
        )

        service = GitOperationsService(local_test_repo)

        # Future date - should find nothing
        result = service.search_diffs(
            search_string=unique_content,
            since="2099-01-01"
        )
        assert len(result.matches) == 0

    @pytest.mark.slow
    def test_search_diffs_with_until_filter(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Filter pickaxe search by date (until).
        """
        unique_content = "UNTIL_FILTER_TEST_CONTENT"
        test_file = local_test_repo / "until_test.txt"
        test_file.write_text(f"{unique_content}\n")

        subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add until test file"],
            cwd=local_test_repo, check=True, capture_output=True
        )

        service = GitOperationsService(local_test_repo)

        # Past date - should find nothing
        result = service.search_diffs(
            search_string=unique_content,
            until="1990-01-01"
        )
        assert len(result.matches) == 0

    @pytest.mark.slow
    def test_search_diffs_files_changed_populated(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Pickaxe results include list of changed files.
        """
        unique_content = "FILES_CHANGED_TEST_CONTENT"
        test_file = local_test_repo / "files_changed_test.txt"
        test_file.write_text(f"{unique_content}\n")

        subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add files changed test"],
            cwd=local_test_repo, check=True, capture_output=True
        )

        service = GitOperationsService(local_test_repo)
        result = service.search_diffs(search_string=unique_content)

        assert len(result.matches) >= 1
        match = result.matches[0]
        assert len(match.files_changed) >= 1
        assert "files_changed_test.txt" in match.files_changed

    @pytest.mark.slow
    def test_search_diffs_timing_tracked(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Search time is tracked in milliseconds.
        """
        unique_content = "TIMING_TEST_CONTENT"
        test_file = local_test_repo / "timing_test.txt"
        test_file.write_text(f"{unique_content}\n")

        subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add timing test"],
            cwd=local_test_repo, check=True, capture_output=True
        )

        service = GitOperationsService(local_test_repo)
        result = service.search_diffs(search_string=unique_content)

        assert result.search_time_ms >= 0
        assert isinstance(result.search_time_ms, float)

    @pytest.mark.slow
    def test_search_diffs_multifile_commit(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Pickaxe correctly handles commits with multiple files.
        """
        unique_content = "MULTIFILE_TEST_CONTENT"

        # Create multiple files with same content
        (local_test_repo / "multi_a.txt").write_text(f"{unique_content}_A\n")
        (local_test_repo / "multi_b.txt").write_text(f"{unique_content}_B\n")
        (local_test_repo / "multi_c.txt").write_text("Different content\n")

        subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add multiple files"],
            cwd=local_test_repo, check=True, capture_output=True
        )

        service = GitOperationsService(local_test_repo)
        result = service.search_diffs(search_string="MULTIFILE_TEST_CONTENT")

        assert len(result.matches) >= 1
        match = result.matches[0]
        # Should list multiple files changed in that commit
        assert len(match.files_changed) >= 2
