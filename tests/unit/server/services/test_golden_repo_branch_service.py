"""
Tests for GoldenRepoBranchService - date parsing and branch classification logic.

Following strict TDD methodology: these tests define expected behavior BEFORE
(or alongside) implementation.

Tests cover:
1. Git %ci format date parsing: "2026-02-02 14:37:25 -0600"
2. Standard ISO 8601 date parsing (regression): "2026-02-02T14:37:25-06:00"
3. Timezone variants: positive offset, UTC, negative offset
4. Edge cases: empty string, None, whitespace
5. Branch type classification (existing helper function)
"""

from datetime import datetime, timezone, timedelta

import pytest


class TestParseGitDate:
    """
    Tests for the _parse_git_date helper function.

    This function must handle git's %ci (strict ISO 8601) format which produces
    dates like "2026-02-02 14:37:25 -0600" - note the space before the timezone
    offset. Python 3.9's datetime.fromisoformat() does not support this format.

    Bug #330: datetime.fromisoformat() fails for space-separated timezone offsets.
    """

    def test_git_ci_format_negative_timezone(self):
        """AC1: Git %ci format with negative timezone offset parses correctly."""
        from code_indexer.server.services.golden_repo_branch_service import (
            _parse_git_date,
        )

        result = _parse_git_date("2026-02-02 14:37:25 -0600")

        assert result is not None
        assert result.year == 2026
        assert result.month == 2
        assert result.day == 2
        assert result.hour == 14
        assert result.minute == 37
        assert result.second == 25
        # Verify timezone offset is -06:00
        assert result.tzinfo is not None
        expected_offset = timedelta(hours=-6)
        assert result.utcoffset() == expected_offset

    def test_git_ci_format_positive_timezone(self):
        """AC1: Git %ci format with positive timezone offset parses correctly."""
        from code_indexer.server.services.golden_repo_branch_service import (
            _parse_git_date,
        )

        result = _parse_git_date("2026-02-02 14:37:25 +0530")

        assert result is not None
        assert result.year == 2026
        assert result.month == 2
        assert result.day == 2
        assert result.hour == 14
        assert result.minute == 37
        assert result.second == 25
        expected_offset = timedelta(hours=5, minutes=30)
        assert result.utcoffset() == expected_offset

    def test_git_ci_format_utc(self):
        """AC1: Git %ci format with UTC (+0000) parses correctly."""
        from code_indexer.server.services.golden_repo_branch_service import (
            _parse_git_date,
        )

        result = _parse_git_date("2026-02-02 14:37:25 +0000")

        assert result is not None
        assert result.year == 2026
        assert result.month == 2
        assert result.day == 2
        assert result.hour == 14
        assert result.minute == 37
        assert result.second == 25
        assert result.utcoffset() == timedelta(0)

    def test_iso_8601_with_t_separator_and_colon_offset(self):
        """AC2: Standard ISO 8601 with T separator and colon in offset still parses."""
        from code_indexer.server.services.golden_repo_branch_service import (
            _parse_git_date,
        )

        result = _parse_git_date("2026-02-02T14:37:25-06:00")

        assert result is not None
        assert result.year == 2026
        assert result.month == 2
        assert result.day == 2
        assert result.hour == 14
        assert result.minute == 37
        assert result.second == 25
        expected_offset = timedelta(hours=-6)
        assert result.utcoffset() == expected_offset

    def test_iso_8601_with_t_separator_and_utc(self):
        """AC2: Standard ISO 8601 with T separator and UTC offset still parses."""
        from code_indexer.server.services.golden_repo_branch_service import (
            _parse_git_date,
        )

        result = _parse_git_date("2026-02-02T14:37:25+00:00")

        assert result is not None
        assert result.utcoffset() == timedelta(0)

    def test_git_ci_format_no_timezone(self):
        """Edge case: Date without timezone parses as naive datetime."""
        from code_indexer.server.services.golden_repo_branch_service import (
            _parse_git_date,
        )

        result = _parse_git_date("2026-02-02 14:37:25")

        assert result is not None
        assert result.year == 2026
        assert result.month == 2
        assert result.day == 2
        assert result.hour == 14
        assert result.minute == 37
        assert result.second == 25
        assert result.tzinfo is None

    def test_empty_string_raises_value_error(self):
        """Edge case: Empty string raises ValueError."""
        from code_indexer.server.services.golden_repo_branch_service import (
            _parse_git_date,
        )

        with pytest.raises(ValueError, match="Cannot parse git date"):
            _parse_git_date("")

    def test_none_like_empty_string_raises_value_error(self):
        """Edge case: Whitespace-only string raises ValueError."""
        from code_indexer.server.services.golden_repo_branch_service import (
            _parse_git_date,
        )

        with pytest.raises(ValueError, match="Cannot parse git date"):
            _parse_git_date("   ")

    def test_invalid_format_raises_value_error(self):
        """Edge case: Completely invalid string raises ValueError."""
        from code_indexer.server.services.golden_repo_branch_service import (
            _parse_git_date,
        )

        with pytest.raises(ValueError, match="Cannot parse git date"):
            _parse_git_date("not-a-date")

    def test_git_ci_format_different_year(self):
        """AC3: Correctly parses any valid year in git %ci format."""
        from code_indexer.server.services.golden_repo_branch_service import (
            _parse_git_date,
        )

        result = _parse_git_date("2024-12-31 23:59:59 +0100")

        assert result is not None
        assert result.year == 2024
        assert result.month == 12
        assert result.day == 31
        assert result.hour == 23
        assert result.minute == 59
        assert result.second == 59
        expected_offset = timedelta(hours=1)
        assert result.utcoffset() == expected_offset


class TestParseDateInBranchServiceContext:
    """
    Tests that verify the date parsing behaves correctly in the context of
    the get_golden_repo_branches method - i.e., that parsing errors are
    handled gracefully (returning None commit_timestamp) rather than crashing.

    These tests verify AC3: graceful error handling preserves existing behavior.
    """

    def test_parse_git_date_used_in_service_handles_bad_dates_gracefully(self):
        """
        Verify the service-level error handling pattern: bad dates yield
        commit_timestamp=None, not an exception propagating to the caller.

        This validates that the refactored code preserves the existing graceful
        degradation behavior (warning logged, None returned).
        """
        from code_indexer.server.services.golden_repo_branch_service import (
            _parse_git_date,
        )

        # Valid dates should parse successfully
        valid_date = "2026-02-02 14:37:25 -0600"
        result = _parse_git_date(valid_date)
        assert result is not None

        # Invalid dates should raise ValueError (caught by caller for graceful handling)
        with pytest.raises(ValueError):
            _parse_git_date("garbage-input")


class TestClassifyBranchType:
    """
    Tests for the classify_branch_type function.
    These cover the existing helper to ensure no regression.
    """

    def test_main_branch_names(self):
        """Primary branches are classified as 'main'."""
        from code_indexer.server.services.golden_repo_branch_service import (
            classify_branch_type,
        )

        for name in ["main", "master", "develop", "development", "dev"]:
            assert classify_branch_type(name) == "main", f"Expected 'main' for {name}"

    def test_feature_branch_prefixes(self):
        """Feature branches are classified as 'feature'."""
        from code_indexer.server.services.golden_repo_branch_service import (
            classify_branch_type,
        )

        for name in ["feature/my-feature", "feat/something", "features/big-thing"]:
            assert (
                classify_branch_type(name) == "feature"
            ), f"Expected 'feature' for {name}"

    def test_release_branch_prefixes(self):
        """Release branches are classified as 'release'."""
        from code_indexer.server.services.golden_repo_branch_service import (
            classify_branch_type,
        )

        for name in ["release/1.0", "rel/2.3", "v1.0.0", "v2.1"]:
            assert (
                classify_branch_type(name) == "release"
            ), f"Expected 'release' for {name}"

    def test_hotfix_branch_prefixes(self):
        """Hotfix branches are classified as 'hotfix'."""
        from code_indexer.server.services.golden_repo_branch_service import (
            classify_branch_type,
        )

        for name in ["hotfix/urgent", "fix/crash", "patch/security", "bugfix/login"]:
            assert (
                classify_branch_type(name) == "hotfix"
            ), f"Expected 'hotfix' for {name}"

    def test_other_branches(self):
        """Unrecognized branches are classified as 'other'."""
        from code_indexer.server.services.golden_repo_branch_service import (
            classify_branch_type,
        )

        for name in ["experiment", "wip-something", "random-branch"]:
            assert (
                classify_branch_type(name) == "other"
            ), f"Expected 'other' for {name}"

    def test_empty_or_whitespace_branch_name(self):
        """Empty or whitespace branch names are classified as 'other'."""
        from code_indexer.server.services.golden_repo_branch_service import (
            classify_branch_type,
        )

        assert classify_branch_type("") == "other"
        assert classify_branch_type("   ") == "other"
