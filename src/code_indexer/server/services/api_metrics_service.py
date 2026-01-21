"""
API Metrics Service for Story #4 AC2.

Tracks API call counts aggregated over the dashboard refresh interval:
- Semantic Searches (search_code with semantic mode)
- Other Index Searches (FTS, temporal, hybrid searches)
- Regex Searches (regex_search calls)
- All Other API Calls (remaining API endpoints)

Thread-safe counters that reset on dashboard refresh cycle.
"""

import logging
import threading
from dataclasses import dataclass
from typing import Dict

logger = logging.getLogger(__name__)


@dataclass
class ApiMetrics:
    """API call metrics data."""

    semantic_searches: int = 0
    other_index_searches: int = 0
    regex_searches: int = 0
    other_api_calls: int = 0


class ApiMetricsService:
    """Service for tracking API call metrics.

    Thread-safe counters that can be reset on each dashboard refresh cycle.
    Similar pattern to IO metrics in health_service.
    """

    def __init__(self):
        """Initialize the API metrics service."""
        self._lock = threading.Lock()
        self._metrics = ApiMetrics()

    def increment_semantic_search(self) -> None:
        """Increment the semantic search counter."""
        with self._lock:
            self._metrics.semantic_searches += 1

    def increment_other_index_search(self) -> None:
        """Increment the other index search counter (FTS, temporal, hybrid)."""
        with self._lock:
            self._metrics.other_index_searches += 1

    def increment_regex_search(self) -> None:
        """Increment the regex search counter."""
        with self._lock:
            self._metrics.regex_searches += 1

    def increment_other_api_call(self) -> None:
        """Increment the other API calls counter."""
        with self._lock:
            self._metrics.other_api_calls += 1

    def get_metrics(self) -> Dict[str, int]:
        """Get current metrics as a dictionary."""
        with self._lock:
            return {
                "semantic_searches": self._metrics.semantic_searches,
                "other_index_searches": self._metrics.other_index_searches,
                "regex_searches": self._metrics.regex_searches,
                "other_api_calls": self._metrics.other_api_calls,
            }

    def reset(self) -> None:
        """Reset all counters to zero (called on dashboard refresh cycle)."""
        with self._lock:
            self._metrics = ApiMetrics()


# Global service instance
api_metrics_service = ApiMetricsService()
