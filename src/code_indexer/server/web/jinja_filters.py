"""
Jinja2 custom filters for CIDX Server web templates.

Story #142: Research Assistant - Conversation Resume
Implements AC3: Message Timestamps
"""

from datetime import datetime, timedelta, timezone


def relative_time(timestamp_str: str) -> str:
    """
    Format timestamp as relative or absolute based on age (AC3).

    Formats timestamps as:
    - Relative: "X seconds/minutes/hours ago" for messages <24 hours old
    - Absolute: "MMM DD, HH:MM" for messages >=24 hours old

    Args:
        timestamp_str: ISO format timestamp string (e.g., "2026-01-30T14:32:00+00:00" or with Z)

    Returns:
        Formatted timestamp string

    Raises:
        ValueError: If timestamp format is invalid

    Examples:
        >>> relative_time("2026-01-30T14:30:00Z")  # 2 minutes ago
        "2 minutes ago"
        >>> relative_time("2026-01-25T14:30:00Z")  # 5 days ago
        "Jan 25, 14:30"
    """
    try:
        # Parse ISO format timestamp
        # Handle both +00:00 and Z suffix for UTC
        if timestamp_str.endswith("Z"):
            timestamp_str = timestamp_str[:-1] + "+00:00"

        timestamp_dt = datetime.fromisoformat(timestamp_str)

        # Ensure timezone-aware comparison
        if timestamp_dt.tzinfo is None:
            timestamp_dt = timestamp_dt.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        delta = now - timestamp_dt

        # Handle edge case of future timestamps (clock skew, test data)
        if delta.total_seconds() < 0:
            return "just now"

        # Use relative format for <24 hours
        if delta < timedelta(hours=24):
            total_seconds = int(delta.total_seconds())

            if total_seconds < 60:
                # Seconds
                if total_seconds == 1:
                    return "1 second ago"
                return f"{total_seconds} seconds ago"

            elif total_seconds < 3600:
                # Minutes
                minutes = total_seconds // 60
                if minutes == 1:
                    return "1 minute ago"
                return f"{minutes} minutes ago"

            else:
                # Hours
                hours = total_seconds // 3600
                if hours == 1:
                    return "1 hour ago"
                return f"{hours} hours ago"

        # Use absolute format for >=24 hours
        else:
            return timestamp_dt.strftime("%b %d, %H:%M")

    except (ValueError, AttributeError) as e:
        raise ValueError(f"Invalid timestamp format: {timestamp_str}") from e
