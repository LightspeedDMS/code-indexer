"""Bug #2125: the decision behind ``record_owed_refresh_retry``, shared by
the SQLite and PostgreSQL ``refresh_failure_backoff_state`` stores.

An owed retry (files still owed after a partial publish, or a trigger-only
refresh that failed on an ordinary error) is not a failure: it escalates
its own ``owed_retry_count`` and never touches
``consecutive_failure_count``.
"""

from __future__ import annotations

from typing import Callable, Optional, Tuple


def plan_owed_retry(
    existing_owed_count: Optional[int],
    existing_pending_due_at: Optional[float],
    existing_generation: Optional[int],
    covered_generation: Optional[int],
    min_count: int,
    now: float,
    retry_seconds: Callable[[int], float],
) -> Tuple[int, float]:
    """(owed count, due time) for one more owed retry of an alias.

    ``existing_*`` describe the alias's row (all None: no row;
    ``existing_pending_due_at`` None: no pending trigger). The count is one
    more than the row's, at least ``min_count`` (a partial publish carries
    the count across its own resolution) and at least 1. The retry is due
    ``retry_seconds(count)`` from now, except that a pending trigger the
    finishing cycle did not serve (its generation differs from
    ``covered_generation``, or the cycle's generation is unknown) keeps its
    earlier due time, so a trigger that arrived mid-cycle is never delayed.
    The lease of the trigger the cycle served is replaced, so retries
    escalate."""
    count = max((existing_owed_count or 0) + 1, min_count, 1)
    due_at = now + retry_seconds(count)
    unserved_trigger = existing_pending_due_at is not None and (
        covered_generation is None or existing_generation != covered_generation
    )
    if unserved_trigger and existing_pending_due_at is not None:
        due_at = min(due_at, existing_pending_due_at)
    return count, due_at
