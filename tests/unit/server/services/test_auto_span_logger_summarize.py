"""Unit tests for AutoSpanLogger._summarize_output lines key handling.

Bug #1008: git_blame responses use output["lines"] (list) which passes through
without summarization, creating oversized Langfuse traces.

Fix 4: Add "lines" key handling analogous to the existing "results" key handling.
"""

from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.auto_span_logger import AutoSpanLogger


def _make_logger() -> AutoSpanLogger:
    """Create a minimal AutoSpanLogger for testing _summarize_output directly."""
    return AutoSpanLogger(
        trace_manager=MagicMock(),
        langfuse_client=MagicMock(),
    )


class TestSummarizeOutputLinesKey:
    """Tests for _summarize_output handling of the 'lines' key."""

    def test_summarize_output_handles_lines_key(self):
        """Dict with 'lines' list must be summarized: line_count, summary, no 'lines'.

        git_blame responses carry a 'lines' list that can be very large.
        The method must replace it with a compact summary to reduce trace size.
        """
        logger = _make_logger()
        lines_data = [
            {"line": 1, "commit": "abc123", "content": "def foo():"},
            {"line": 2, "commit": "def456", "content": "    return 42"},
            {"line": 3, "commit": "abc123", "content": ""},
        ]
        output = {"lines": lines_data, "repo": "my-repo"}

        result = logger._summarize_output(output)

        assert "lines" not in result
        assert result["line_count"] == 3
        assert result["summary"] == "3 blame lines returned"
        assert result["repo"] == "my-repo"

    def test_summarize_output_lines_non_list_passthrough(self):
        """Dict with non-list 'lines' value must pass through unchanged.

        Only list values trigger summarization; other types are left alone.
        """
        logger = _make_logger()
        output = {"lines": "not a list", "repo": "my-repo"}

        result = logger._summarize_output(output)

        assert result == {"lines": "not a list", "repo": "my-repo"}

    def test_summarize_output_results_still_works(self):
        """Existing 'results' list summarization must still work after the change.

        Regression test: adding 'lines' handling must not break the pre-existing
        'results' summarization path.
        """
        logger = _make_logger()
        output = {
            "results": [{"score": 0.9, "text": "foo"}, {"score": 0.8, "text": "bar"}],
            "query": "foo",
        }

        result = logger._summarize_output(output)

        assert "results" not in result
        assert result["result_count"] == 2
        assert result["summary"] == "2 results returned"
        assert result["query"] == "foo"
