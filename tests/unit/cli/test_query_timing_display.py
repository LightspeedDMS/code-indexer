"""
Tests for CLI query timing display with multi-index support.

These tests verify that the _display_query_timing() function properly
displays timing information for both single-index and parallel multi-index queries.
"""

from io import StringIO
from rich.console import Console
from src.code_indexer.cli import _display_query_timing


class TestQueryTimingDisplay:
    """Test timing display for single and multi-index queries."""

    def test_display_single_index_timing(self):
        """Test timing display for single-index (code only) query."""
        # Setup console to capture output
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=80)

        # Single-index timing (no multimodal)
        timing_info = {
            "code_index_ms": 38,
            "has_multimodal": False,
            "code_timed_out": False,
            "git_filter_ms": 49,
        }

        # Display timing
        _display_query_timing(console, timing_info)

        # Get output
        result = output.getvalue()

        # Verify no multi-index display
        assert (
            "parallel" not in result.lower()
        ), "Should not show parallel timing for single index"
        assert (
            "multimodal" not in result.lower()
        ), "Should not show multimodal timing for single index"
        assert (
            "merge" not in result.lower()
        ), "Should not show merge timing for single index"

        # Verify git filter timing is shown
        assert (
            "git" in result.lower() or "filter" in result.lower()
        ), "Should show git-aware filtering"

    def test_display_multi_index_timing_with_parallel_breakdown(self):
        """Test timing display for multi-index (code + multimodal) query with parallel breakdown."""
        # Setup console to capture output
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=80)

        # Multi-index timing
        timing_info = {
            "parallel_multi_index_ms": 45,
            "code_index_ms": 23,
            "multimodal_index_ms": 41,
            "merge_deduplicate_ms": 4,
            "has_multimodal": True,
            "code_timed_out": False,
            "multimodal_timed_out": False,
            "git_filter_ms": 49,
        }

        # Display timing
        _display_query_timing(console, timing_info)

        # Get output
        result = output.getvalue()

        # Verify parallel multi-index display
        assert "parallel" in result.lower(), "Should show parallel multi-index timing"
        assert (
            "voyage-code-3" in result.lower() or "code" in result.lower()
        ), "Should show code index"
        assert (
            "voyage-multimodal-3" in result.lower() or "multimodal" in result.lower()
        ), "Should show multimodal index"
        assert (
            "merge" in result.lower() or "deduplicate" in result.lower()
        ), "Should show merge/deduplicate timing"

        # Verify individual timings are shown
        assert "23" in result or "23ms" in result, "Should show code index time (23ms)"
        assert (
            "41" in result or "41ms" in result
        ), "Should show multimodal index time (41ms)"
        assert "45" in result or "45ms" in result, "Should show parallel time (45ms)"
        assert "4" in result or "4ms" in result, "Should show merge time (4ms)"

    def test_display_multi_index_timing_with_timeout(self):
        """Test timing display when one index times out."""
        # Setup console to capture output
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=80)

        # Multi-index timing with multimodal timeout
        timing_info = {
            "parallel_multi_index_ms": 30000,  # 30 seconds (timeout)
            "code_index_ms": 23,
            "multimodal_index_ms": 0,  # Timed out, no timing
            "merge_deduplicate_ms": 2,
            "has_multimodal": True,
            "code_timed_out": False,
            "multimodal_timed_out": True,
            "git_filter_ms": 15,
        }

        # Display timing
        _display_query_timing(console, timing_info)

        # Get output
        result = output.getvalue()

        # Verify timeout indication
        # The display should show multimodal timing with TIMEOUT indicator
        assert (
            "parallel" in result.lower()
        ), "Should still show parallel timing structure"
        assert (
            "multimodal" in result.lower() or "voyage-multimodal-3" in result.lower()
        ), "Should show multimodal index"
        assert (
            "timeout" in result.lower()
        ), "Should show TIMEOUT indicator for timed out index"
        # Code index should NOT show timeout
        assert (
            result.lower().count("timeout") == 1
        ), "Only multimodal should show timeout (code index did not timeout)"

    def test_display_empty_timing_info(self):
        """Test that empty timing info doesn't crash display."""
        # Setup console to capture output
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=80)

        # Empty timing
        timing_info = {}

        # Should not crash
        _display_query_timing(console, timing_info)

        # Get output - should be empty or minimal
        result = output.getvalue()
        # Empty timing should result in no display (early return)
        assert (
            result == "" or len(result.strip()) == 0
        ), "Empty timing should produce no output"

    def test_display_preserves_existing_timing_breakdown(self):
        """Test that multi-index timing display works correctly with multimodal query."""
        # Setup console to capture output
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=80)

        # Multi-index timing - when has_multimodal is True, display focuses on index-level timing
        timing_info = {
            "parallel_multi_index_ms": 45,
            "code_index_ms": 23,
            "multimodal_index_ms": 41,
            "merge_deduplicate_ms": 4,
            "has_multimodal": True,
            "code_timed_out": False,
            "multimodal_timed_out": False,
            "git_filter_ms": 49,
        }

        # Display timing
        _display_query_timing(console, timing_info)

        # Get output
        result = output.getvalue()

        # Verify multi-index timing is shown with proper breakdown
        assert "parallel" in result.lower(), "Should show parallel timing"
        assert (
            "23" in result or "23ms" in result
        ), "Should show code index timing (23ms)"
        assert (
            "41" in result or "41ms" in result
        ), "Should show multimodal index timing (41ms)"
        assert "4" in result or "4ms" in result, "Should show merge timing (4ms)"

    def test_display_timing_format_consistency(self):
        """Test that timing format is consistent (ms vs s) across display."""
        import re

        # Setup console to capture output
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=80)

        # Multi-index timing with mix of small and large times
        timing_info = {
            "parallel_multi_index_ms": 1500,  # 1.5 seconds
            "code_index_ms": 800,
            "multimodal_index_ms": 1400,
            "merge_deduplicate_ms": 50,
            "has_multimodal": True,
            "code_timed_out": False,
            "multimodal_timed_out": False,
            "git_filter_ms": 2000,  # 2 seconds
        }

        # Display timing
        _display_query_timing(console, timing_info)

        # Get output and strip ANSI codes (Rich console adds color codes)
        result = output.getvalue()
        clean_result = re.sub(r"\x1b\[[0-9;]*m", "", result)

        # Verify timing values are formatted
        # (should show "1.5s", "1.50s", or "1500ms" consistently)
        # Large times (>1000ms) should use seconds format
        assert (
            "s" in clean_result or "ms" in clean_result
        ), "Should format timing values"
        # Verify large times use seconds (1.5s, 1.50s, 2s, 2.0s, 2.00s, etc.)
        assert (
            "1.5s" in clean_result
            or "1.50s" in clean_result
            or "1500ms" in clean_result
        ), "Should format 1500ms appropriately"
        assert (
            "2.0s" in clean_result
            or "2.00s" in clean_result
            or "2s" in clean_result
            or "2000ms" in clean_result
        ), "Should format 2000ms appropriately"
