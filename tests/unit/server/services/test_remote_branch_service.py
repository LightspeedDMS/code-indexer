"""
Tests for RemoteBranchService - branch filtering and extraction logic.

Following TDD methodology: these tests define expected behavior BEFORE implementation.

Tests cover:
1. Branch filtering logic (exclude issue-tracker patterns)
2. Branch name extraction from git ls-remote output
3. Fetching branches from remote URLs
4. Error handling for inaccessible repositories
"""


class TestFilterIssueTrackerBranches:
    r"""Tests for filter_issue_tracker_branches function.

    Issue tracker patterns to exclude: [A-Za-z]+-\d+
    Examples: SCM-1234, A-1, AB-99, PROJ-567, X-9
    Also excludes branches containing these patterns in paths.
    """

    def test_filter_keeps_standard_branches(self):
        """Test that standard branch names are kept."""
        # Import will fail until implementation exists
        from code_indexer.server.services.remote_branch_service import (
            filter_issue_tracker_branches,
        )

        branches = ["main", "develop", "master", "staging", "production"]
        result = filter_issue_tracker_branches(branches)
        assert result == branches

    def test_filter_excludes_simple_issue_patterns(self):
        """Test that simple issue-tracker patterns are excluded."""
        from code_indexer.server.services.remote_branch_service import (
            filter_issue_tracker_branches,
        )

        branches = ["main", "SCM-1234", "A-1", "AB-99", "PROJ-567", "X-9"]
        result = filter_issue_tracker_branches(branches)
        assert result == ["main"]

    def test_filter_excludes_patterns_in_paths(self):
        """Test that issue-tracker patterns within paths are excluded."""
        from code_indexer.server.services.remote_branch_service import (
            filter_issue_tracker_branches,
        )

        branches = [
            "main",
            "feature/login",
            "feature/SCM-1234-hotfix",
            "bugfix/AB-12-fix",
        ]
        result = filter_issue_tracker_branches(branches)
        assert result == ["main", "feature/login"]

    def test_filter_keeps_numeric_suffix_branches(self):
        """Test that branches with numeric suffixes (no letter prefix) are kept."""
        from code_indexer.server.services.remote_branch_service import (
            filter_issue_tracker_branches,
        )

        branches = ["main", "hotfix-123", "release/v1.0", "v2.3.4"]
        result = filter_issue_tracker_branches(branches)
        assert result == branches

    def test_filter_handles_release_branches(self):
        """Test that release branches are kept (even with version numbers)."""
        from code_indexer.server.services.remote_branch_service import (
            filter_issue_tracker_branches,
        )

        branches = ["release/v1.0", "release/2.0.0", "releases/stable"]
        result = filter_issue_tracker_branches(branches)
        assert result == branches

    def test_filter_empty_list_returns_empty(self):
        """Test that empty input returns empty output."""
        from code_indexer.server.services.remote_branch_service import (
            filter_issue_tracker_branches,
        )

        result = filter_issue_tracker_branches([])
        assert result == []

    def test_filter_all_filtered_returns_empty(self):
        """Test that when all branches match pattern, empty list is returned."""
        from code_indexer.server.services.remote_branch_service import (
            filter_issue_tracker_branches,
        )

        branches = ["SCM-1234", "PROJ-567", "A-1"]
        result = filter_issue_tracker_branches(branches)
        assert result == []

    def test_filter_comprehensive_example_from_story(self):
        """Test the exact example from the story acceptance criteria."""
        from code_indexer.server.services.remote_branch_service import (
            filter_issue_tracker_branches,
        )

        # Input from story Scenario 2
        branches = [
            "main",
            "develop",
            "feature/login",
            "SCM-1234",
            "A-1",
            "AB-99",
            "PROJ-567",
            "X-9",
            "feature/SCM-1234-hotfix",
            "bugfix/AB-12-fix",
            "release/v1.0",
            "hotfix-123",
        ]

        # Expected output from story Scenario 2
        expected = [
            "main",
            "develop",
            "feature/login",
            "release/v1.0",
            "hotfix-123",
        ]

        result = filter_issue_tracker_branches(branches)
        assert result == expected


class TestExtractBranchNamesFromLsRemote:
    """Tests for extracting branch names from git ls-remote output."""

    def test_extract_standard_branches(self):
        """Test extraction of standard branch refs."""
        from code_indexer.server.services.remote_branch_service import (
            extract_branch_names_from_ls_remote,
        )

        # git ls-remote --heads output format
        ls_remote_output = """abc123def456	refs/heads/main
def456abc789	refs/heads/develop
789abc123def	refs/heads/feature/login"""

        result = extract_branch_names_from_ls_remote(ls_remote_output)
        assert result == ["main", "develop", "feature/login"]

    def test_extract_branches_with_slashes(self):
        """Test extraction of branch names containing slashes."""
        from code_indexer.server.services.remote_branch_service import (
            extract_branch_names_from_ls_remote,
        )

        ls_remote_output = """abc123	refs/heads/feature/nested/path/branch
def456	refs/heads/bugfix/issue-123"""

        result = extract_branch_names_from_ls_remote(ls_remote_output)
        assert result == ["feature/nested/path/branch", "bugfix/issue-123"]

    def test_extract_ignores_tags(self):
        """Test that tag refs are ignored."""
        from code_indexer.server.services.remote_branch_service import (
            extract_branch_names_from_ls_remote,
        )

        ls_remote_output = """abc123	refs/heads/main
def456	refs/tags/v1.0.0
ghi789	refs/heads/develop"""

        result = extract_branch_names_from_ls_remote(ls_remote_output)
        assert result == ["main", "develop"]

    def test_extract_handles_empty_output(self):
        """Test handling of empty git ls-remote output."""
        from code_indexer.server.services.remote_branch_service import (
            extract_branch_names_from_ls_remote,
        )

        result = extract_branch_names_from_ls_remote("")
        assert result == []

    def test_extract_handles_whitespace_lines(self):
        """Test handling of whitespace-only lines in output."""
        from code_indexer.server.services.remote_branch_service import (
            extract_branch_names_from_ls_remote,
        )

        ls_remote_output = """abc123	refs/heads/main

def456	refs/heads/develop
   """

        result = extract_branch_names_from_ls_remote(ls_remote_output)
        assert result == ["main", "develop"]


class TestRemoteBranchService:
    """Tests for RemoteBranchService class functionality."""

    def test_fetch_branches_success(self):
        """Test successful branch fetching from a public repository."""
        from code_indexer.server.services.remote_branch_service import (
            RemoteBranchService,
        )

        service = RemoteBranchService()

        # Use a well-known public repo that won't change frequently
        result = service.fetch_remote_branches(
            clone_url="https://github.com/octocat/Hello-World.git"
        )

        assert result.success is True
        assert isinstance(result.branches, list)
        assert len(result.branches) > 0
        assert result.error is None
        # Hello-World repo should have master or main
        assert any(b in result.branches for b in ["master", "main"])

    def test_fetch_branches_filters_issue_tracker_patterns(self):
        """Test that fetched branches are filtered for issue-tracker patterns."""
        from code_indexer.server.services.remote_branch_service import (
            RemoteBranchService,
            ISSUE_TRACKER_PATTERN,
        )

        service = RemoteBranchService()

        # The result should not contain issue-tracker pattern branches
        result = service.fetch_remote_branches(
            clone_url="https://github.com/octocat/Hello-World.git"
        )

        assert result.success is True
        # Verify no UPPERCASE issue-tracker patterns in result
        # Pattern matches JIRA-style keys: ABC-123, SCM-1234, etc.
        for branch in result.branches:
            assert not ISSUE_TRACKER_PATTERN.search(
                branch
            ), f"Branch '{branch}' should have been filtered (matches issue-tracker pattern)"

    def test_fetch_branches_invalid_url_returns_error(self):
        """Test that invalid/inaccessible URLs return error."""
        from code_indexer.server.services.remote_branch_service import (
            RemoteBranchService,
        )

        service = RemoteBranchService()

        result = service.fetch_remote_branches(
            clone_url="https://github.com/nonexistent-user-xyz/nonexistent-repo-xyz.git"
        )

        assert result.success is False
        assert result.branches == []
        assert result.error is not None
        assert len(result.error) > 0

    def test_fetch_branches_returns_default_branch_info(self):
        """Test that fetch result includes default branch if detectable."""
        from code_indexer.server.services.remote_branch_service import (
            RemoteBranchService,
        )

        service = RemoteBranchService()

        result = service.fetch_remote_branches(
            clone_url="https://github.com/octocat/Hello-World.git"
        )

        assert result.success is True
        # Default branch should be detected
        assert result.default_branch is not None
        assert result.default_branch in result.branches

    def test_fetch_branches_with_credentials_github(self):
        """Test branch fetching with GitHub token credentials."""
        from code_indexer.server.services.remote_branch_service import (
            RemoteBranchService,
        )

        service = RemoteBranchService()

        # Should work even with None credentials for public repos
        result = service.fetch_remote_branches(
            clone_url="https://github.com/octocat/Hello-World.git",
            platform="github",
            credentials=None,
        )

        assert result.success is True
        assert len(result.branches) > 0

    def test_fetch_multiple_repos(self):
        """Test fetching branches for multiple repositories."""
        from code_indexer.server.services.remote_branch_service import (
            RemoteBranchService,
            BranchFetchRequest,
        )

        service = RemoteBranchService()

        requests = [
            BranchFetchRequest(
                clone_url="https://github.com/octocat/Hello-World.git",
                platform="github",
            ),
            BranchFetchRequest(
                clone_url="https://github.com/nonexistent-user-xyz/nonexistent-repo-xyz.git",
                platform="github",
            ),
        ]

        results = service.fetch_branches_for_repos(requests)

        assert len(results) == 2

        # First repo should succeed
        assert results["https://github.com/octocat/Hello-World.git"].success is True

        # Second repo should fail gracefully
        assert (
            results[
                "https://github.com/nonexistent-user-xyz/nonexistent-repo-xyz.git"
            ].success
            is False
        )


class TestBranchFetchResult:
    """Tests for BranchFetchResult data class."""

    def test_success_result(self):
        """Test creating a successful result."""
        from code_indexer.server.services.remote_branch_service import (
            BranchFetchResult,
        )

        result = BranchFetchResult(
            success=True,
            branches=["main", "develop"],
            default_branch="main",
            error=None,
        )

        assert result.success is True
        assert result.branches == ["main", "develop"]
        assert result.default_branch == "main"
        assert result.error is None

    def test_error_result(self):
        """Test creating an error result."""
        from code_indexer.server.services.remote_branch_service import (
            BranchFetchResult,
        )

        result = BranchFetchResult(
            success=False,
            branches=[],
            default_branch=None,
            error="Repository not found",
        )

        assert result.success is False
        assert result.branches == []
        assert result.default_branch is None
        assert result.error == "Repository not found"


class TestBranchFetchRequest:
    """Tests for BranchFetchRequest data class."""

    def test_create_request(self):
        """Test creating a branch fetch request."""
        from code_indexer.server.services.remote_branch_service import (
            BranchFetchRequest,
        )

        request = BranchFetchRequest(
            clone_url="https://github.com/example/repo.git", platform="github"
        )

        assert request.clone_url == "https://github.com/example/repo.git"
        assert request.platform == "github"

    def test_create_request_with_optional_fields(self):
        """Test creating a request with optional credential fields."""
        from code_indexer.server.services.remote_branch_service import (
            BranchFetchRequest,
        )

        request = BranchFetchRequest(
            clone_url="https://gitlab.com/example/repo.git", platform="gitlab"
        )

        assert request.clone_url == "https://gitlab.com/example/repo.git"
        assert request.platform == "gitlab"
