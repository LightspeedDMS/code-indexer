"""The cidx-meta debouncer settles an owed write only on a refresh job whose
result states it covered the source (``covered_source``: it published, or
found nothing to do); each job-store state maps to one verdict, and the
awaiting state never resubmits while the job runs, nor drops a write that
arrived meanwhile.

Timers are never waited on: each step runs the pending timer's callback."""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.global_repos.cidx_meta_refresh_debouncer import (
    CidxMetaRefreshDebouncer,
    RefreshJobVerdict,
    refresh_job_verdict,
)

DEBOUNCE_SECONDS = 3600.0
COVERING = {"success": True, "message": "Refresh complete", "covered_source": True}


class _JobStore:
    """The scheduler side: hands out job ids and answers job-status reads
    from a table the test fills in."""

    def __init__(self) -> None:
        self.statuses: Dict[str, Optional[Dict[str, Any]]] = {}
        self.submitted: List[str] = []
        self.read_error: Optional[Exception] = None
        self.background_job_manager = self

    def trigger_refresh_for_repo(self, alias_name: str) -> str:
        job_id = f"job-{len(self.submitted) + 1}"
        self.submitted.append(job_id)
        self.statuses[job_id] = {"status": "running", "result": None}
        return job_id

    def get_job_status(
        self, job_id: str, username: str, is_admin: bool = False
    ) -> Optional[Dict[str, Any]]:
        assert username == "system" and is_admin
        if self.read_error is not None:
            raise self.read_error
        return self.statuses.get(job_id)

    def finish(self, job_id: str, status: str, result: Any = None) -> None:
        self.statuses[job_id] = {"status": status, "result": result}


def _expire_now(debouncer: CidxMetaRefreshDebouncer) -> None:
    with debouncer._lock:
        pending: Optional[threading.Timer] = debouncer._timer
    assert pending is not None, "the refresh is owed but no timer is pending"
    pending.cancel()
    pending.function(*pending.args, **pending.kwargs)


@pytest.mark.parametrize(
    "status, result, verdict",
    [
        ("pending", None, RefreshJobVerdict.RUNNING),
        ("running", None, RefreshJobVerdict.RUNNING),
        ("resolving_prerequisites", None, RefreshJobVerdict.RUNNING),
        ("completed", COVERING, RefreshJobVerdict.COVERED),
        (
            "completed",
            {"message": "No changes detected", "covered_source": True},
            RefreshJobVerdict.COVERED,
        ),
        (
            "completed_partial",
            {"partial": True, "covered_source": True},
            RefreshJobVerdict.COVERED,
        ),
        # A finished job that does not state it covered the source:
        ("completed", {"message": "Refresh complete"}, RefreshJobVerdict.OWED),
        (
            "completed",
            {"success": True, "message": "Alias not found, skipped"},
            RefreshJobVerdict.OWED,
        ),
        (
            "completed",
            {"success": True, "message": "Not yet initialized, skipped"},
            RefreshJobVerdict.OWED,
        ),
        (
            "completed",
            {"success": True, "message": "Skipped, write lock held"},
            RefreshJobVerdict.OWED,
        ),
        (
            "completed",
            {"success": True, "skipped": "volume_alarm_deferred"},
            RefreshJobVerdict.OWED,
        ),
        ("completed", {"covered_source": "yes"}, RefreshJobVerdict.OWED),
        ("failed", {"covered_source": True}, RefreshJobVerdict.OWED),
        ("cancelled", None, RefreshJobVerdict.OWED),
        ("interrupted", None, RefreshJobVerdict.OWED),
    ],
)
def test_job_state_verdicts(
    status: str, result: Any, verdict: RefreshJobVerdict
) -> None:
    store = _JobStore()
    store.finish("job-1", status, result)

    assert refresh_job_verdict(store, "job-1") is verdict


def test_unknown_job_is_owed() -> None:
    assert refresh_job_verdict(_JobStore(), "job-404") is RefreshJobVerdict.OWED


class TestAwaitingTransitions:
    def test_running_job_is_rechecked_never_resubmitted(self) -> None:
        store = _JobStore()
        debouncer = CidxMetaRefreshDebouncer(store, debounce_seconds=DEBOUNCE_SECONDS)
        try:
            debouncer.signal_dirty()
            _expire_now(debouncer)
            for _ in range(3):
                _expire_now(debouncer)  # still running

            assert store.submitted == ["job-1"]
            store.finish("job-1", "completed", COVERING)
            _expire_now(debouncer)
            assert debouncer._dirty is False and debouncer._timer is None
        finally:
            debouncer.shutdown()

    def test_write_during_the_job_is_submitted_after_it_covers_the_earlier(
        self,
    ) -> None:
        store = _JobStore()
        debouncer = CidxMetaRefreshDebouncer(store, debounce_seconds=DEBOUNCE_SECONDS)
        try:
            debouncer.signal_dirty()
            _expire_now(debouncer)
            debouncer.signal_dirty()  # a write while job-1 runs
            store.finish("job-1", "completed", COVERING)

            _expire_now(debouncer)

            assert store.submitted == ["job-1", "job-2"]
            assert debouncer._dirty is True
        finally:
            debouncer.shutdown()

    def test_unreadable_job_store_keeps_the_write_owed_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = _JobStore()
        debouncer = CidxMetaRefreshDebouncer(store, debounce_seconds=DEBOUNCE_SECONDS)
        try:
            debouncer.signal_dirty()
            _expire_now(debouncer)
            store.read_error = RuntimeError("job store unavailable")
            with caplog.at_level(logging.INFO):
                _expire_now(debouncer)

            assert debouncer._dirty is True
            assert store.submitted == ["job-1"]
            assert any(
                r.levelno == logging.WARNING and "status unreadable" in r.getMessage()
                for r in caplog.records
            )
        finally:
            debouncer.shutdown()

    def test_non_covering_job_is_retried_paced_and_logged_at_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = _JobStore()
        debouncer = CidxMetaRefreshDebouncer(store, debounce_seconds=2.0)
        try:
            debouncer.signal_dirty()
            _expire_now(debouncer)
            store.finish(
                "job-1",
                "completed",
                {"success": True, "message": "Skipped, write lock held"},
            )
            with caplog.at_level(logging.INFO):
                _expire_now(debouncer)

            with debouncer._lock:
                assert debouncer._timer is not None
                assert debouncer._timer.interval == 4.0
            assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
            _expire_now(debouncer)
            assert store.submitted == ["job-1", "job-2"]
        finally:
            debouncer.shutdown()
