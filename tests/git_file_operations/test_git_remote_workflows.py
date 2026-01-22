"""
Integration tests for Git Remote Operations Workflows.

Story #9 - Git Remote Operations Test Suite (Workflow Integration)

Tests complete workflows combining push, pull, and fetch operations
to verify they work correctly together in realistic scenarios.

Uses REAL git operations - NO Python mocks for git commands.
Uses local bare remote set up by local_test_repo fixture - NO network access required.
"""

import shutil
import subprocess
from pathlib import Path

from code_indexer.server.services.git_operations_service import GitOperationsService


class TestRemoteOperationsWorkflow:
    """Integration tests for complete remote operation workflows."""

    def test_full_push_fetch_pull_workflow(
        self,
        local_test_repo: Path,
        synced_remote_state,
        unique_filename: str,
    ):
        """Complete workflow: commit -> push -> (remote changes) -> fetch -> pull."""
        service = GitOperationsService()
        temp_dir = local_test_repo.parent
        remote_path = temp_dir / "test-remote.git"

        # Step 1: Create and push local commit
        local_file = local_test_repo / unique_filename
        local_file.write_text("initial content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Local commit"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        push_result = service.git_push(local_test_repo, remote="origin", branch="main")
        assert push_result["success"] is True

        # Step 2: Simulate remote changes via second clone
        second_clone = temp_dir / "workflow-second-clone"
        if second_clone.exists():
            shutil.rmtree(second_clone)

        subprocess.run(
            ["git", "clone", "--branch", "main", str(remote_path), str(second_clone)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "other@example.com"],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Other User"],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )

        remote_file = second_clone / "remote_workflow_file.txt"
        remote_file.write_text("remote workflow content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Remote workflow commit"],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )

        # Step 3: Fetch to see remote changes
        fetch_result = service.git_fetch(local_test_repo, remote="origin")
        assert fetch_result["success"] is True

        # Verify local file doesn't exist yet (fetch doesn't merge)
        remote_file_local = local_test_repo / "remote_workflow_file.txt"
        assert not remote_file_local.exists()

        # Step 4: Pull to merge remote changes
        pull_result = service.git_pull(local_test_repo, remote="origin", branch="main")
        assert pull_result["success"] is True

        # Verify remote file now exists locally
        assert remote_file_local.exists()
        assert remote_file_local.read_text() == "remote workflow content\n"

    def test_push_after_pull_workflow(
        self,
        local_test_repo: Path,
        synced_remote_state,
        unique_filename: str,
    ):
        """Workflow: pull remote changes -> commit local -> push."""
        service = GitOperationsService()
        temp_dir = local_test_repo.parent
        remote_path = temp_dir / "test-remote.git"

        # Create remote changes via second clone
        second_clone = temp_dir / "push-after-pull-clone"
        if second_clone.exists():
            shutil.rmtree(second_clone)

        subprocess.run(
            ["git", "clone", "--branch", "main", str(remote_path), str(second_clone)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "other@example.com"],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Other User"],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )

        remote_file = second_clone / "remote_file_first.txt"
        remote_file.write_text("remote first\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Remote commit first"],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )

        # Pull remote changes first
        pull_result = service.git_pull(local_test_repo, remote="origin", branch="main")
        assert pull_result["success"] is True

        # Now create local commit
        local_file = local_test_repo / unique_filename
        local_file.write_text("local after pull\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Local commit after pull"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        # Push should succeed because we pulled first
        push_result = service.git_push(local_test_repo, remote="origin", branch="main")
        assert push_result["success"] is True

    def test_fetch_then_compare_before_pull(
        self,
        local_test_repo: Path,
        synced_remote_state,
    ):
        """Workflow: fetch -> compare refs -> decide to pull."""
        service = GitOperationsService()
        temp_dir = local_test_repo.parent
        remote_path = temp_dir / "test-remote.git"

        # Create remote changes
        second_clone = temp_dir / "fetch-compare-clone"
        if second_clone.exists():
            shutil.rmtree(second_clone)

        subprocess.run(
            ["git", "clone", "--branch", "main", str(remote_path), str(second_clone)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "other@example.com"],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Other User"],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )

        new_file = second_clone / "new_remote_file.txt"
        new_file.write_text("new content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "New remote commit"],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )

        # Get local HEAD before fetch
        local_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Fetch to update remote tracking refs
        fetch_result = service.git_fetch(local_test_repo, remote="origin")
        assert fetch_result["success"] is True

        # Compare local vs origin/main
        origin_main = subprocess.run(
            ["git", "rev-parse", "origin/main"],
            cwd=local_test_repo,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # They should be different (remote has new commits)
        assert local_head != origin_main

        # Now pull to sync
        pull_result = service.git_pull(local_test_repo, remote="origin", branch="main")
        assert pull_result["success"] is True

        # Now they should match
        new_local_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert new_local_head == origin_main
