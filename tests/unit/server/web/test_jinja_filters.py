"""
Tests for Jinja2 custom filters.

Story #142: Research Assistant - Conversation Resume
Tests for AC3: Message Timestamps
"""

import pytest
from datetime import datetime, timedelta, timezone
from code_indexer.server.web.jinja_filters import relative_time


class TestRelativeTimeFilter:
    """Tests for relative_time Jinja filter (AC3)."""

    def test_relative_time_seconds_ago(self):
        """Test: Formats timestamp as 'X seconds ago' for messages <1 minute old."""
        # Arrange
        now = datetime.now(timezone.utc)
        timestamp = (now - timedelta(seconds=30)).isoformat()

        # Act
        result = relative_time(timestamp)

        # Assert
        assert result == "30 seconds ago"

    def test_relative_time_one_minute_ago(self):
        """Test: Formats timestamp as '1 minute ago' for messages 1 minute old."""
        # Arrange
        now = datetime.now(timezone.utc)
        timestamp = (now - timedelta(minutes=1)).isoformat()

        # Act
        result = relative_time(timestamp)

        # Assert
        assert result == "1 minute ago"

    def test_relative_time_minutes_ago(self):
        """Test: Formats timestamp as 'X minutes ago' for messages <1 hour old."""
        # Arrange
        now = datetime.now(timezone.utc)
        timestamp = (now - timedelta(minutes=45)).isoformat()

        # Act
        result = relative_time(timestamp)

        # Assert
        assert result == "45 minutes ago"

    def test_relative_time_one_hour_ago(self):
        """Test: Formats timestamp as '1 hour ago' for messages 1 hour old."""
        # Arrange
        now = datetime.now(timezone.utc)
        timestamp = (now - timedelta(hours=1)).isoformat()

        # Act
        result = relative_time(timestamp)

        # Assert
        assert result == "1 hour ago"

    def test_relative_time_hours_ago(self):
        """Test: Formats timestamp as 'X hours ago' for messages <24 hours old."""
        # Arrange
        now = datetime.now(timezone.utc)
        timestamp = (now - timedelta(hours=12)).isoformat()

        # Act
        result = relative_time(timestamp)

        # Assert
        assert result == "12 hours ago"

    def test_relative_time_absolute_format_yesterday(self):
        """Test: Formats timestamp as absolute 'MMM DD, HH:MM' for messages >=24 hours old."""
        # Arrange
        now = datetime.now(timezone.utc)
        timestamp_dt = now - timedelta(hours=25)
        timestamp = timestamp_dt.isoformat()

        # Act
        result = relative_time(timestamp)

        # Assert
        # Should be in format like "Jan 30, 14:32"
        expected = timestamp_dt.strftime("%b %d, %H:%M")
        assert result == expected

    def test_relative_time_absolute_format_last_week(self):
        """Test: Formats timestamp as absolute for messages from last week."""
        # Arrange
        now = datetime.now(timezone.utc)
        timestamp_dt = now - timedelta(days=7)
        timestamp = timestamp_dt.isoformat()

        # Act
        result = relative_time(timestamp)

        # Assert
        expected = timestamp_dt.strftime("%b %d, %H:%M")
        assert result == expected

    def test_relative_time_handles_utc_timezone(self):
        """Test: Correctly handles UTC timezone in ISO format."""
        # Arrange
        timestamp_dt = datetime(2026, 1, 30, 14, 32, 0, tzinfo=timezone.utc)
        timestamp = timestamp_dt.isoformat()

        # Act
        result = relative_time(timestamp)

        # Assert
        # Will be absolute since it's in the past
        assert "Jan 30" in result
        assert "14:32" in result

    def test_relative_time_handles_z_suffix(self):
        """Test: Correctly handles 'Z' suffix for UTC in ISO format."""
        # Arrange
        timestamp_dt = datetime(2026, 1, 30, 14, 32, 0, tzinfo=timezone.utc)
        timestamp = timestamp_dt.isoformat().replace("+00:00", "Z")

        # Act
        result = relative_time(timestamp)

        # Assert
        assert "Jan 30" in result
        assert "14:32" in result

    def test_relative_time_edge_case_23_hours_59_minutes(self):
        """Test: Uses relative format at edge of 24-hour threshold."""
        # Arrange
        now = datetime.now(timezone.utc)
        timestamp = (now - timedelta(hours=23, minutes=59)).isoformat()

        # Act
        result = relative_time(timestamp)

        # Assert
        assert result == "23 hours ago"

    def test_relative_time_edge_case_exactly_24_hours(self):
        """Test: Uses absolute format at exactly 24 hours."""
        # Arrange
        now = datetime.now(timezone.utc)
        timestamp_dt = now - timedelta(hours=24)
        timestamp = timestamp_dt.isoformat()

        # Act
        result = relative_time(timestamp)

        # Assert
        # Should be absolute format at 24 hours
        expected = timestamp_dt.strftime("%b %d, %H:%M")
        assert result == expected

    def test_relative_time_zero_seconds(self):
        """Test: Handles timestamp from right now."""
        # Arrange
        now = datetime.now(timezone.utc)
        timestamp = now.isoformat()

        # Act
        result = relative_time(timestamp)

        # Assert
        assert result == "0 seconds ago"

    def test_relative_time_invalid_format_raises_error(self):
        """Test: Raises ValueError for invalid timestamp format."""
        # Arrange
        invalid_timestamp = "not-a-valid-timestamp"

        # Act & Assert
        with pytest.raises(ValueError):
            relative_time(invalid_timestamp)
