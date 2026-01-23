"""
Integration tests for Discovery Branches functionality.

These tests use real public git repositories with ZERO mocking,
following CLAUDE.md Foundation #1 (Anti-Mock).

Tests verify:
1. Branch fetching from real GitHub/GitLab repositories
2. Branch filtering is applied correctly
3. Default branch detection works
4. Error handling for non-existent repositories
"""


class TestRemoteBranchServiceIntegration:
    """Integration tests for RemoteBranchService with real git repos."""

    def test_fetch_branches_from_github_hello_world(self):
        """Test fetching branches from GitHub's Hello-World repo."""
        from code_indexer.server.services.remote_branch_service import (
            RemoteBranchService,
        )

        service = RemoteBranchService()
        result = service.fetch_remote_branches(
            clone_url="https://github.com/octocat/Hello-World.git", platform="github"
        )

        # Should succeed
        assert result.success is True
        assert result.error is None

        # Should have branches
        assert len(result.branches) > 0

        # Should have a default branch
        assert result.default_branch is not None

        # Master is the default branch for Hello-World
        assert "master" in result.branches

    def test_fetch_branches_from_github_with_many_branches(self):
        """Test fetching branches from a repo with multiple branches."""
        from code_indexer.server.services.remote_branch_service import (
            RemoteBranchService,
        )

        service = RemoteBranchService()
        # Using git/git which has multiple branches
        result = service.fetch_remote_branches(
            clone_url="https://github.com/git/git.git", platform="github"
        )

        # Should succeed
        assert result.success is True
        assert result.error is None

        # Should have multiple branches
        assert len(result.branches) > 1

        # master or main should be present
        assert any(b in result.branches for b in ["master", "main"])

    def test_fetch_branches_nonexistent_repo_returns_error(self):
        """Test that non-existent repos return proper error."""
        from code_indexer.server.services.remote_branch_service import (
            RemoteBranchService,
        )

        service = RemoteBranchService()
        result = service.fetch_remote_branches(
            clone_url="https://github.com/this-user-does-not-exist-xyz/nonexistent-repo-abc123.git",
            platform="github",
        )

        # Should fail gracefully
        assert result.success is False
        assert result.error is not None
        assert len(result.error) > 0
        assert result.branches == []

    def test_fetch_branches_invalid_url_returns_error(self):
        """Test that invalid URLs return proper error."""
        from code_indexer.server.services.remote_branch_service import (
            RemoteBranchService,
        )

        service = RemoteBranchService()
        result = service.fetch_remote_branches(
            clone_url="not-a-valid-url", platform="github"
        )

        # Should fail gracefully
        assert result.success is False
        assert result.error is not None


class TestBranchFilteringIntegration:
    """Integration tests for branch filtering with real data."""

    def test_issue_tracker_patterns_are_filtered(self):
        """Test that issue-tracker patterns are correctly filtered."""
        from code_indexer.server.services.remote_branch_service import (
            filter_issue_tracker_branches,
        )

        # Simulate branches that might exist in a real repo
        branches = [
            "main",
            "develop",
            "feature/add-login",
            "JIRA-1234",  # Should be filtered
            "SCM-567",  # Should be filtered
            "release/v1.0.0",
            "hotfix-123",  # Should NOT be filtered (lowercase)
            "bugfix/ABC-99-fix",  # Should be filtered (contains ABC-99)
            "A-1",  # Should be filtered
        ]

        filtered = filter_issue_tracker_branches(branches)

        # Verify filtering
        assert "main" in filtered
        assert "develop" in filtered
        assert "feature/add-login" in filtered
        assert "release/v1.0.0" in filtered
        assert "hotfix-123" in filtered

        # Issue tracker patterns should be excluded
        assert "JIRA-1234" not in filtered
        assert "SCM-567" not in filtered
        assert "bugfix/ABC-99-fix" not in filtered
        assert "A-1" not in filtered

    def test_real_repo_branches_do_not_contain_issue_patterns(self):
        """Test that filtered results from real repo don't have issue patterns."""
        from code_indexer.server.services.remote_branch_service import (
            RemoteBranchService,
            ISSUE_TRACKER_PATTERN,
        )

        service = RemoteBranchService()
        result = service.fetch_remote_branches(
            clone_url="https://github.com/octocat/Hello-World.git", platform="github"
        )

        assert result.success is True

        # Verify no issue-tracker patterns in results
        for branch in result.branches:
            assert not ISSUE_TRACKER_PATTERN.search(
                branch
            ), f"Branch '{branch}' should have been filtered out"


class TestMultipleReposFetching:
    """Integration tests for fetching branches from multiple repos."""

    def test_fetch_branches_for_multiple_repos(self):
        """Test fetching branches for multiple repos in one call."""
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
                clone_url="https://github.com/this-does-not-exist-xyz/no-repo.git",
                platform="github",
            ),
        ]

        results = service.fetch_branches_for_repos(requests)

        # Should have results for both repos
        assert len(results) == 2

        # First repo should succeed
        hello_world = results["https://github.com/octocat/Hello-World.git"]
        assert hello_world.success is True
        assert len(hello_world.branches) > 0

        # Second repo should fail gracefully
        nonexistent = results["https://github.com/this-does-not-exist-xyz/no-repo.git"]
        assert nonexistent.success is False
        assert nonexistent.error is not None


class TestDefaultBranchDetection:
    """Integration tests for default branch detection."""

    def test_default_branch_is_detected(self):
        """Test that default branch is correctly detected."""
        from code_indexer.server.services.remote_branch_service import (
            RemoteBranchService,
        )

        service = RemoteBranchService()
        result = service.fetch_remote_branches(
            clone_url="https://github.com/octocat/Hello-World.git", platform="github"
        )

        assert result.success is True
        assert result.default_branch is not None

        # Default branch should be in the branches list
        assert result.default_branch in result.branches

    def test_default_branch_is_master_for_hello_world(self):
        """Test that Hello-World repo has master as default."""
        from code_indexer.server.services.remote_branch_service import (
            RemoteBranchService,
        )

        service = RemoteBranchService()
        result = service.fetch_remote_branches(
            clone_url="https://github.com/octocat/Hello-World.git", platform="github"
        )

        assert result.success is True
        # Hello-World uses master as default
        assert result.default_branch == "master"
