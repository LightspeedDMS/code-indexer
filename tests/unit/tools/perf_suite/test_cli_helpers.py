"""
Unit tests for tools/perf-suite/cli_helpers.py

Story #334: Concurrency Escalation Tests with Degradation Detection
AC1: --concurrency-levels CLI argument parsing and validation.

TDD: Tests for zero/negative level validation (code review Finding 2).
"""

from __future__ import annotations

import sys
import os

import pytest

# Add the perf-suite directory to path so we can import from it
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../tools/perf-suite"))


class TestParseConcurrencyLevels:
    """Tests for cli_helpers.parse_concurrency_levels()."""

    def test_none_returns_defaults(self):
        """None input returns the default concurrency levels."""
        from cli_helpers import parse_concurrency_levels, DEFAULT_CONCURRENCY_LEVELS

        result = parse_concurrency_levels(None)
        assert result == DEFAULT_CONCURRENCY_LEVELS

    def test_valid_levels_parsed_and_sorted(self):
        """Valid comma-separated integers are parsed and sorted ascending."""
        from cli_helpers import parse_concurrency_levels

        result = parse_concurrency_levels("5,1,10,2")
        assert result == [1, 2, 5, 10]

    def test_single_valid_level(self):
        """A single valid level is accepted."""
        from cli_helpers import parse_concurrency_levels

        result = parse_concurrency_levels("1")
        assert result == [1]

    def test_zero_level_rejected(self):
        """Zero concurrency level is rejected with a clear error (would deadlock Semaphore)."""
        from cli_helpers import parse_concurrency_levels

        with pytest.raises(SystemExit):
            parse_concurrency_levels("0,1,5")

    def test_zero_only_rejected(self):
        """A single zero value is rejected."""
        from cli_helpers import parse_concurrency_levels

        with pytest.raises(SystemExit):
            parse_concurrency_levels("0")

    def test_negative_level_rejected(self):
        """Negative concurrency levels are rejected with a clear error."""
        from cli_helpers import parse_concurrency_levels

        with pytest.raises(SystemExit):
            parse_concurrency_levels("-1,5,10")

    def test_negative_only_rejected(self):
        """A single negative value is rejected."""
        from cli_helpers import parse_concurrency_levels

        with pytest.raises(SystemExit):
            parse_concurrency_levels("-5")

    def test_mixed_valid_invalid_rejected(self):
        """Mix of valid and zero/negative levels is rejected (not silently filtered)."""
        from cli_helpers import parse_concurrency_levels

        with pytest.raises(SystemExit):
            parse_concurrency_levels("1,0,5")

    def test_non_integer_rejected(self):
        """Non-integer input exits with error."""
        from cli_helpers import parse_concurrency_levels

        with pytest.raises(SystemExit):
            parse_concurrency_levels("1,two,5")

    def test_empty_string_rejected(self):
        """Empty string exits with error (no levels found)."""
        from cli_helpers import parse_concurrency_levels

        with pytest.raises(SystemExit):
            parse_concurrency_levels("")

    def test_whitespace_around_values_tolerated(self):
        """Spaces around comma-separated values are tolerated."""
        from cli_helpers import parse_concurrency_levels

        result = parse_concurrency_levels("1, 2 , 5")
        assert result == [1, 2, 5]
