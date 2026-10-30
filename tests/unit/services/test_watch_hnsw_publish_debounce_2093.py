"""#2093: the watch's HNSW publish debounce (hnsw_publish_due)."""

from __future__ import annotations

from code_indexer.services.git_aware_watch_handler import (
    HNSW_PUBLISH_IDLE_FACTOR,
    HNSW_PUBLISH_MAX_DELAY_SECONDS,
    hnsw_publish_due,
)

NEVER = float("-inf")


def test_quiet_queue_publishes_at_once() -> None:
    assert hnsw_publish_due(100.0, 99.0, NEVER, 0.0, changes_queued=False)


def test_queued_changes_defer_the_publish() -> None:
    assert not hnsw_publish_due(100.0, 99.0, NEVER, 0.0, changes_queued=True)


def test_queued_changes_never_defer_past_the_max_delay() -> None:
    owed_since = 100.0
    now = owed_since + HNSW_PUBLISH_MAX_DELAY_SECONDS

    assert hnsw_publish_due(now, owed_since, NEVER, 0.0, changes_queued=True)


def test_slow_publish_spaces_the_next_one() -> None:
    last_end, last_seconds = 100.0, 10.0
    gap = HNSW_PUBLISH_IDLE_FACTOR * last_seconds

    assert not hnsw_publish_due(
        last_end + gap - 0.1, 50.0, last_end, last_seconds, changes_queued=False
    )
    assert hnsw_publish_due(
        last_end + gap, 50.0, last_end, last_seconds, changes_queued=False
    )
