"""
API Metrics Service for Story #4 AC2 - Rolling Window Implementation.

Tracks API call timestamps using rolling window approach:
- Semantic Searches (search_code with semantic mode)
- Other Index Searches (FTS, temporal, hybrid searches)
- Regex Searches (regex_search calls)
- All Other API Calls (remaining API endpoints)

Thread-safe timestamp deques with configurable time windows.
Timestamps older than 24 hours are automatically cleaned up.
"""

import logging
import threading
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Dict, Deque

logger = logging.getLogger(__name__)

# Maximum age for timestamps - 24 hours
MAX_TIMESTAMP_AGE_SECONDS = 86400  # 24 hours


class ApiMetricsService:
    """Service for tracking API call metrics using rolling windows.

    Thread-safe timestamp deques that support configurable time windows.
    Timestamps are stored per API call category and filtered by window on read.
    """

    def __init__(self):
        """Initialize the API metrics service with empty deques."""
        self._lock = threading.Lock()
        self._semantic_searches: Deque[datetime] = deque()
        self._other_index_searches: Deque[datetime] = deque()
        self._regex_searches: Deque[datetime] = deque()
        self._other_api_calls: Deque[datetime] = deque()

    def _cleanup_old(self, timestamps: Deque[datetime]) -> None:
        """Remove timestamps older than 24 hours from the front of the deque.

        Since timestamps are appended in chronological order, older timestamps
        are at the front. We pop from the left until we find a timestamp within
        24 hours.

        Args:
            timestamps: The deque to clean up (must be called under lock)
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=MAX_TIMESTAMP_AGE_SECONDS)
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()

    def increment_semantic_search(self) -> None:
        """Record a semantic search call timestamp."""
        with self._lock:
            now = datetime.now(timezone.utc)
            self._semantic_searches.append(now)
            self._cleanup_old(self._semantic_searches)

    def increment_other_index_search(self) -> None:
        """Record an other index search call timestamp (FTS, temporal, hybrid)."""
        with self._lock:
            now = datetime.now(timezone.utc)
            self._other_index_searches.append(now)
            self._cleanup_old(self._other_index_searches)

    def increment_regex_search(self) -> None:
        """Record a regex search call timestamp."""
        with self._lock:
            now = datetime.now(timezone.utc)
            self._regex_searches.append(now)
            self._cleanup_old(self._regex_searches)

    def increment_other_api_call(self) -> None:
        """Record an other API call timestamp."""
        with self._lock:
            now = datetime.now(timezone.utc)
            self._other_api_calls.append(now)
            self._cleanup_old(self._other_api_calls)

    def _count_in_window(
        self, timestamps: Deque[datetime], window_seconds: int
    ) -> int:
        """Count timestamps within the specified window.

        Args:
            timestamps: Deque of timestamps (must be called under lock)
            window_seconds: Number of seconds to look back

        Returns:
            Count of timestamps within the window
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        count = 0
        # Iterate from the end (most recent) since those are likely within window
        for ts in reversed(timestamps):
            if ts >= cutoff:
                count += 1
            else:
                # Once we hit a timestamp outside the window, all earlier ones
                # are also outside (since they're chronologically ordered)
                break
        return count

    def get_metrics(self, window_seconds: int = 60) -> Dict[str, int]:
        """Get metrics for the specified time window.

        Args:
            window_seconds: Time window in seconds. Default is 60 (1 minute).
                Common values: 60 (1 min), 900 (15 min), 3600 (1 hour), 86400 (24 hours)

        Returns:
            Dictionary with counts for each metric category within the window.
        """
        with self._lock:
            return {
                "semantic_searches": self._count_in_window(
                    self._semantic_searches, window_seconds
                ),
                "other_index_searches": self._count_in_window(
                    self._other_index_searches, window_seconds
                ),
                "regex_searches": self._count_in_window(
                    self._regex_searches, window_seconds
                ),
                "other_api_calls": self._count_in_window(
                    self._other_api_calls, window_seconds
                ),
            }

    def reset(self) -> None:
        """Clear all timestamp data.

        Note: With rolling window approach, this method is largely unnecessary
        as timestamps naturally age out. Kept for backward compatibility.
        """
        with self._lock:
            self._semantic_searches.clear()
            self._other_index_searches.clear()
            self._regex_searches.clear()
            self._other_api_calls.clear()


# Global service instance
api_metrics_service = ApiMetricsService()
