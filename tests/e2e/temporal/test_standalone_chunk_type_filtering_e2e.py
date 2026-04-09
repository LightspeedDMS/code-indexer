"""E2E test for Bug #3: chunk_type filtering in standalone mode (daemon disabled).

This test verifies that --chunk-type filter works correctly when daemon is disabled.

Bug Report:
    When daemon is disabled, `cidx query "X" --chunk-type commit_diff` returns
    [Commit Message] chunks instead of only file diff chunks.

Root Cause Investigation:
    - cli.py passes chunk_type to query_temporal (line 5239) ✓
    - query_temporal adds chunk_type filter to filter_conditions (line 359-364) ✓
    - FilesystemVectorStore evaluate_condition handles "value" match (line 1382-1385) ✓
    - Post-filter in _filter_by_time_range also applies chunk_type (line 666-670) ✓

This test will help identify where the filtering is failing.
"""

import tempfile
import shutil
import subprocess
from pathlib import Path


class TestStandaloneChunkTypeFilteringE2E:
    """E2E tests for chunk_type filtering in standalone mode (Bug #3)."""

    @classmethod
    def setup_class(cls):
        """Set up test repository with temporal index (daemon disabled)."""
        cls.test_dir = tempfile.mkdtemp(prefix="test_chunk_type_standalone_")  # type: ignore[attr-defined]
        cls.repo_path = Path(cls.test_dir) / "test_repo"  # type: ignore[attr-defined]
        cls.repo_path.mkdir()  # type: ignore[attr-defined]

        # Initialize git repo
        subprocess.run(["git", "init"], cwd=cls.repo_path, check=True)  # type: ignore[attr-defined]
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=cls.repo_path,  # type: ignore[attr-defined]
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=cls.repo_path,  # type: ignore[attr-defined]
            check=True,
        )

        # Commit 1: Add auth.py with clear commit message
        (cls.repo_path / "auth.py").write_text(  # type: ignore[attr-defined]
            """def authenticate(username, password):
    # Basic authentication implementation
    if not username or not password:
        return False
    return True
"""
        )
        subprocess.run(["git", "add", "."], cwd=cls.repo_path, check=True)  # type: ignore[attr-defined]
        subprocess.run(
            ["git", "commit", "-m", "Add authentication module"],
            cwd=cls.repo_path,  # type: ignore[attr-defined]
            check=True,
        )

        # Commit 2: Modify auth.py with distinctive commit message
        (cls.repo_path / "auth.py").write_text(  # type: ignore[attr-defined]
            """def authenticate(username, password):
    # Enhanced authentication with logging
    if not username or not password:
        logger.warning("Missing credentials")
        return False

    logger.info(f"Authenticating user: {username}")
    return validate_credentials(username, password)
"""
        )
        subprocess.run(["git", "add", "."], cwd=cls.repo_path, check=True)  # type: ignore[attr-defined]
        subprocess.run(
            ["git", "commit", "-m", "Improve authentication logging and validation"],
            cwd=cls.repo_path,  # type: ignore[attr-defined]
            check=True,
        )

        # Initialize cidx (CLI mode, daemon will be disabled)
        subprocess.run(["cidx", "init"], cwd=cls.repo_path, check=True)  # type: ignore[attr-defined]

        # Verify daemon is disabled
        result = subprocess.run(
            ["cidx", "status"],
            cwd=cls.repo_path,  # type: ignore[attr-defined]
            capture_output=True,
            text=True,
            check=True,
        )
        assert (
            "Daemon Mode: ❌ Disabled" in result.stdout
            or "disabled" in result.stdout.lower()
        )

        # Build temporal index
        subprocess.run(
            ["cidx", "index", "--index-commits", "--clear"],
            cwd=cls.repo_path,  # type: ignore[attr-defined]
            check=True,
            timeout=60,
        )

    @classmethod
    def teardown_class(cls):
        """Clean up test repository."""
        shutil.rmtree(cls.test_dir, ignore_errors=True)  # type: ignore[attr-defined]

    def test_chunk_type_commit_diff_filter_standalone(self):
        """Test that --chunk-type commit_diff returns ONLY file diffs in standalone mode.

        Bug #3: This test demonstrates the bug - commit_diff filter returns commit messages.
        Expected: Query returns file diff chunks (auth.py)
        Actual (if bug): Query returns commit message chunks ([Commit Message])
        """
        # Query with chunk_type=commit_diff filter
        result = subprocess.run(
            [
                "cidx",
                "query",
                "authentication",
                "--time-range-all",
                "--chunk-type",
                "commit_diff",
                "--limit",
                "10",
                "--quiet",
            ],
            cwd=self.repo_path,  # type: ignore[attr-defined]
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, f"Query failed: {result.stderr}"
        output = result.stdout

        # Debug output
        print(f"\n=== Query Output (commit_diff filter) ===\n{output}\n===")

        # Assertion: Should ONLY show file paths (auth.py), NOT [Commit Message]
        lines = [line.strip() for line in output.strip().split("\n") if line.strip()]

        # BUG REPRODUCTION: If bug exists, this will fail because output contains [Commit Message]
        for line in lines:
            # Each line should contain file path, not [Commit Message]
            # Example expected: "0.850 auth.py"
            # Example WRONG: "0.850 [Commit Message]"
            if not line.startswith("🕒") and not line.startswith("📊"):
                assert "[Commit Message]" not in line, (
                    f"BUG REPRODUCED: commit_diff filter returned commit message: {line}"
                )
                assert (
                    "auth.py" in line or ".py" in line or any(c.isalnum() for c in line)
                ), f"Expected file path in result, got: {line}"
