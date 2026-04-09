"""Unit tests for temporal routing gate in MCP handlers._utils.

Bug fix: chunk_type, diff_type, and author must trigger temporal routing
via _is_temporal_query(). Before the fix, these params were absent from
the temporal_params list, causing queries with only chunk_type/diff_type/author
to skip the temporal index entirely and return no temporal metadata.

Tests must FAIL before the fix (Gate 3 broken) and PASS after the fix.
"""

import pytest
from code_indexer.server.mcp.handlers._utils import _is_temporal_query


class TestIsTemporalQueryNewParams:
    """Test _is_temporal_query detects newly-added temporal parameters."""

    @pytest.mark.parametrize(
        "params",
        [
            {"chunk_type": "commit_diff"},
            {"diff_type": "added"},
            {"author": "Alice"},
        ],
    )
    def test_new_temporal_param_triggers_routing(self, params):
        """chunk_type / diff_type / author alone must trigger temporal routing."""
        assert _is_temporal_query(params) is True

    @pytest.mark.parametrize(
        "params",
        [
            {"chunk_type": None},
            {"diff_type": None},
            {"author": None},
        ],
    )
    def test_new_temporal_param_none_does_not_trigger_routing(self, params):
        """None values for new params must NOT trigger temporal routing."""
        assert _is_temporal_query(params) is False


class TestIsTemporalQueryExistingParams:
    """Regression: existing temporal params must still trigger routing after fix."""

    @pytest.mark.parametrize(
        "params",
        [
            {"time_range": "2024-01-01..2024-12-31"},
            {"time_range_all": True},
            {"at_commit": "abc123"},
            {"include_removed": True},
        ],
    )
    def test_existing_temporal_param_still_triggers_routing(self, params):
        """Pre-existing temporal params must still return True after fix."""
        assert _is_temporal_query(params) is True

    @pytest.mark.parametrize(
        "params",
        [
            {"query_text": "hello"},
            {},
            {"time_range_all": False},
            {"include_removed": False},
        ],
    )
    def test_non_temporal_params_do_not_trigger_routing(self, params):
        """Non-temporal or falsy params must return False."""
        assert _is_temporal_query(params) is False
