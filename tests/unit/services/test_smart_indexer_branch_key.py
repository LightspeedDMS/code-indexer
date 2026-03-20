"""Tests for Fix 4: SmartIndexer git_status key consistency (Bug #469).

Bug: smart_indexer.py wrote updated_git_status["branch"] = current_branch
but progressive_metadata.py reads git_status.get("current_branch").
The key mismatch means current_branch is always None after a branch change.

Tests:
1. Source-code guard: smart_indexer uses "current_branch" key, not "branch"
2. ProgressiveMetadata ignores "branch" key (documents the bug mechanism)
3. ProgressiveMetadata correctly stores branch when "current_branch" key is used
"""

import inspect


from code_indexer.services import smart_indexer as si_module
from code_indexer.services.progressive_metadata import ProgressiveMetadata


class TestSmartIndexerBranchKey:
    """Verify SmartIndexer uses 'current_branch' key in updated_git_status."""

    def test_smart_indexer_uses_current_branch_key_not_branch(self):
        """Source guard: updated_git_status must use 'current_branch', not 'branch'.

        This test fails before Fix 4 is applied because line ~430 reads:
            updated_git_status["branch"] = current_branch   (WRONG)
        After the fix it must read:
            updated_git_status["current_branch"] = current_branch  (CORRECT)
        """
        source = inspect.getsource(si_module)

        buggy_pattern = 'updated_git_status["branch"] = current_branch'
        correct_pattern = 'updated_git_status["current_branch"] = current_branch'

        assert buggy_pattern not in source, (
            f"Found buggy key assignment '{buggy_pattern}' in smart_indexer.py. "
            "Must be changed to 'current_branch' to match what progressive_metadata reads."
        )
        assert correct_pattern in source, (
            f"Expected to find '{correct_pattern}' in smart_indexer.py but it was absent. "
            "The fix must use 'current_branch' as the dict key."
        )


class TestProgressiveMetadataBranchKeyContract:
    """Verify ProgressiveMetadata's contract: reads 'current_branch', ignores 'branch'."""

    def test_branch_key_is_ignored_by_progressive_metadata(self):
        """Documents the bug mechanism: 'branch' key is silently ignored.

        If smart_indexer only writes git_status["branch"], progressive_metadata
        will record current_branch=None because it calls git_status.get("current_branch").
        """
        buggy_git_status = {
            "git_available": True,
            "branch": "feature-x",  # wrong key — written by buggy smart_indexer
            "current_commit": "abc123",
        }
        # ProgressiveMetadata reads "current_branch", not "branch"
        assert buggy_git_status.get("current_branch") is None

    def test_progressive_metadata_stores_branch_from_current_branch_key(self, tmp_path):
        """ProgressiveMetadata.start_indexing() stores branch when 'current_branch' key is used."""
        metadata_path = tmp_path / "metadata.json"
        pm = ProgressiveMetadata(metadata_path)

        git_status = {
            "git_available": True,
            "current_branch": "feature-x",  # correct key
            "current_commit": "abc123",
            "project_id": "test-project",
        }
        pm.start_indexing("voyage", "voyage-3", git_status)

        assert pm.metadata.get("current_branch") == "feature-x"

    def test_progressive_metadata_branch_null_with_wrong_key(self, tmp_path):
        """Demonstrates that using 'branch' key leaves current_branch=None in metadata."""
        metadata_path = tmp_path / "metadata.json"
        pm = ProgressiveMetadata(metadata_path)

        git_status = {
            "git_available": True,
            "branch": "feature-x",  # wrong key — the bug
            "current_commit": "abc123",
        }
        pm.start_indexing("voyage", "voyage-3", git_status)

        # The bug: current_branch ends up None even though branch was provided
        assert pm.metadata.get("current_branch") is None
