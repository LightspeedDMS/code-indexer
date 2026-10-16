"""What a jobs-page status filter value selects (one definition shared by the
SQLite and PostgreSQL background-jobs backends, BackgroundJobManager's
in-memory filter and the web route's JobTracker-job filter).

A partial completion (Bug #679/#2125: finished, part of it still owed) is a
completion: the dashboard's completed card counts it and links to the
"completed" filter, so "completed" selects both stored statuses, while
"completed_partial" selects only the partial ones.
"""

from typing import Tuple

_STATUSES_BY_FILTER = {"completed": ("completed", "completed_partial")}


def statuses_for_filter(status_filter: str) -> Tuple[str, ...]:
    """The stored job statuses a non-empty status filter value selects."""
    return _STATUSES_BY_FILTER.get(status_filter, (status_filter,))
