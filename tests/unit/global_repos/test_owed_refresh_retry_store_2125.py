"""Bug #2125: an OWED refresh retry (files still owed after a partial
publish, or a trigger-only refresh that failed on an ordinary error) is NOT
a failure. It is a pending trigger with its own escalating counter,
``owed_retry_count``; ``consecutive_failure_count`` -- what defers new-data
triggers and the git schedule -- stays untouched.

Real stores: SQLite always, PostgreSQL when ``TEST_POSTGRES_DSN`` is set
(migrated by the real ``MigrationRunner``)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

from code_indexer.global_repos import refresh_failure_recovery as recovery
from code_indexer.server.services.metadata_reader import RecordedFailures
from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)

ALIAS = "repo-x-global"
DETAIL = "3 file(s) still owed"
#: Consecutive owed retries that take the interval past its cap.
RETRIES_BEYOND_THE_CAP = 8
#: Lease end a little after the owed retry's due time.
SHORT_LEASE_SECONDS = 10


@pytest.fixture(params=STORE_KINDS)
def metadata(request, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


def _state(metadata: Any) -> Dict[str, Any]:
    state = metadata.get_refresh_failure_backoff_state(ALIAS)
    assert state is not None
    return dict(state)


def _generation(metadata: Any) -> int:
    state = metadata.get_refresh_failure_backoff_state(ALIAS)
    return (
        recovery.NO_TRIGGER_GENERATION if state is None else state["trigger_generation"]
    )


def _owe(metadata: Any, covered: Optional[int], min_count: int = 0) -> Any:
    return metadata.record_owed_refresh_retry(
        ALIAS, DETAIL, covered, min_count, recovery.failure_backoff_seconds
    )


def test_owed_retry_escalates_without_a_failure_and_is_capped(metadata: Any) -> None:
    observed = []
    for n in range(RETRIES_BEYOND_THE_CAP):
        before = time.time()
        # Each retry cycle served the pending trigger it covered.
        count, due_at = _owe(metadata, _generation(metadata))
        state = _state(metadata)
        assert count == n + 1 and state["owed_retry_count"] == n + 1
        assert state["consecutive_failure_count"] == 0, "an owed retry is a failure"
        assert state["pending_trigger"] is True
        assert state["pending_due_at"] == pytest.approx(due_at)
        observed.append(round(due_at - before))

    expected = [
        min(
            recovery.FAILURE_BACKOFF_BASE_SECONDS * 2**n,
            recovery.FAILURE_BACKOFF_CAP_SECONDS,
        )
        for n in range(RETRIES_BEYOND_THE_CAP)
    ]
    assert observed == expected
    assert recovery.active_backoff_until(metadata, ALIAS) is None, (
        "an owed retry must not defer new-data triggers"
    )


def test_owed_retry_keeps_an_earlier_trigger_deferred_mid_cycle(
    metadata: Any,
) -> None:
    covered = _generation(metadata)  # the cycle began with no row
    soon = time.time() + 60
    metadata.mark_volume_alarm_deferral(ALIAS, soon)  # arrives mid-cycle

    _count, due_at = _owe(metadata, covered)

    assert due_at == pytest.approx(soon)
    assert _state(metadata)["pending_due_at"] == pytest.approx(soon)


def test_owed_retry_replaces_the_lease_of_the_trigger_its_cycle_served(
    metadata: Any,
) -> None:
    _count, first_due = _owe(metadata, _generation(metadata))
    covered = _generation(metadata)
    assert metadata.lease_pending_refresh_trigger(
        ALIAS, first_due, first_due + SHORT_LEASE_SECONDS
    )
    before = time.time()

    count, due_at = _owe(metadata, covered)

    assert count == 2
    assert due_at == pytest.approx(before + recovery.failure_backoff_seconds(2), abs=2)


def test_min_count_carries_the_owed_count_across_a_publish(metadata: Any) -> None:
    count, _due = _owe(metadata, _generation(metadata), min_count=3)

    assert count == 3
    assert _state(metadata)["owed_retry_count"] == 3


def test_clean_publish_deletes_the_owed_retry_it_covered(metadata: Any) -> None:
    _owe(metadata, _generation(metadata))

    metadata.resolve_refresh_failure_backoff(ALIAS, _generation(metadata))

    assert metadata.get_refresh_failure_backoff_state(ALIAS) is None


def test_a_surviving_row_resets_the_owed_count(metadata: Any) -> None:
    covered = _generation(metadata)
    _owe(metadata, covered)
    _owe(metadata, _generation(metadata))  # a trigger newer than the cycle

    metadata.resolve_refresh_failure_backoff(ALIAS, covered)

    state = _state(metadata)
    assert state["owed_retry_count"] == 0
    assert state["pending_trigger"] is True
    assert state["pending_due_at"] <= time.time() + 1, "re-armed to fire at once"


def test_skip_escalation_of_an_owed_retry_raises_the_owed_count(
    metadata: Any,
) -> None:
    _owe(metadata, _generation(metadata))

    assert metadata.escalate_refresh_trigger(ALIAS, _generation(metadata)) == 2

    state = _state(metadata)
    assert state["owed_retry_count"] == 2
    assert state["consecutive_failure_count"] == 0


def test_skip_escalation_of_a_failure_still_raises_the_failure_count(
    metadata: Any,
) -> None:
    metadata.record_refresh_failure_backoff(ALIAS, "disk full")
    metadata.mark_refresh_trigger_pending(ALIAS, time.time())

    assert metadata.escalate_refresh_trigger(ALIAS, _generation(metadata)) == 2

    state = _state(metadata)
    assert state["consecutive_failure_count"] == 2
    assert state["owed_retry_count"] == 0


# A partial publish resolves what its cycle covered AND owes a retry in ONE
# transaction: a failure in between never loses the retry.


def test_a_partial_publish_resolves_the_failure_and_owes_in_one_call(
    metadata: Any,
) -> None:
    metadata.record_refresh_failure_backoff(ALIAS, "disk full")
    metadata.mark_refresh_trigger_pending(ALIAS, time.time())
    _owe(metadata, _generation(metadata))
    _owe(metadata, _generation(metadata))  # owed 2
    covered = _generation(metadata)
    before = time.time()

    count, due_at = metadata.resolve_and_record_owed_retry(
        ALIAS, covered, DETAIL, recovery.failure_backoff_seconds
    )

    state = _state(metadata)
    assert count == 3 and state["owed_retry_count"] == 3
    assert state["consecutive_failure_count"] == 0, "the failure was not resolved"
    assert state["pending_trigger"] is True
    assert due_at == pytest.approx(before + recovery.failure_backoff_seconds(3), abs=2)
    assert recovery.active_backoff_until(metadata, ALIAS) is None


def test_a_trigger_newer_than_the_publishing_cycle_stays_due_now(
    metadata: Any,
) -> None:
    _owe(metadata, _generation(metadata))
    covered = _generation(metadata)
    metadata.mark_volume_alarm_deferral(ALIAS, time.time() + 3600)  # mid-cycle

    _count, due_at = metadata.resolve_and_record_owed_retry(
        ALIAS, covered, DETAIL, recovery.failure_backoff_seconds
    )

    assert due_at <= time.time() + 1


class _InjectedStoreFailure(RuntimeError):
    pass


def _fail_inside_the_transaction(count: int) -> float:
    raise _InjectedStoreFailure(f"injected failure planning owed retry {count}")


def test_a_failure_inside_the_publish_transaction_keeps_the_pre_publish_state(
    metadata: Any,
) -> None:
    metadata.record_refresh_failure_backoff(ALIAS, "disk full")
    metadata.mark_refresh_trigger_pending(ALIAS, time.time())
    _owe(metadata, _generation(metadata))
    covered = _generation(metadata)
    before = _state(metadata)

    with pytest.raises(_InjectedStoreFailure):
        metadata.resolve_and_record_owed_retry(
            ALIAS, covered, DETAIL, _fail_inside_the_transaction
        )

    assert metadata.get_refresh_failure_backoff_state(ALIAS) == before, (
        "a failed publish bookkeeping lost the covered pending trigger"
    )


_UNKNOWN = RecordedFailures(known=False, failed_files=0, retryable_paths=0)


def test_unknown_failures_resolve_like_a_clean_publish_then_escalate(
    metadata: Any,
) -> None:
    metadata.record_refresh_failure_backoff(ALIAS, "disk full")  # Bug #2022
    metadata.mark_refresh_trigger_pending(ALIAS, time.time())

    recovery.settle_publish(
        metadata, ALIAS, _generation(metadata), _UNKNOWN, schedule_retry=True
    )

    state = _state(metadata)
    assert state["consecutive_failure_count"] == 0, "the failure was kept"
    assert recovery.active_backoff_until(metadata, ALIAS) is None
    assert state["owed_retry_count"] == 1 and state["pending_trigger"] is True

    recovery.settle_publish(
        metadata, ALIAS, _generation(metadata), _UNKNOWN, schedule_retry=True
    )

    assert _state(metadata)["owed_retry_count"] == 2, "unknown never escalates"


def test_retryable_failures_resolve_and_owe(metadata: Any) -> None:
    metadata.record_refresh_failure_backoff(ALIAS, "disk full")
    metadata.mark_refresh_trigger_pending(ALIAS, time.time())
    owed = RecordedFailures(known=True, failed_files=3, retryable_paths=3)

    due_at = recovery.settle_publish(
        metadata, ALIAS, _generation(metadata), owed, schedule_retry=True
    )

    state = _state(metadata)
    assert due_at is not None
    assert state["consecutive_failure_count"] == 0
    assert state["owed_retry_count"] == 1


class _NoThrottle:
    def on_db_success(self, _logger: Any) -> None:
        return None

    def on_db_error(self, _exc: Exception, _logger: Any) -> bool:
        return False


def test_the_lease_of_an_owed_retry_follows_its_owed_count(
    metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owe(metadata, _generation(metadata))
    _owe(metadata, _generation(metadata))
    _owe(metadata, _generation(metadata))  # owed 3: 1200 s
    real_time = time.time
    later = real_time() + recovery.FAILURE_BACKOFF_CAP_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    submitted: List[str] = []

    recovery.fire_expired_deferred_triggers(metadata, submitted.append, _NoThrottle())

    assert submitted == [ALIAS]
    assert _state(metadata)["pending_due_at"] == pytest.approx(
        later + recovery.failure_backoff_seconds(3)
    )
