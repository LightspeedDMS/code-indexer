"""The cidx-meta refresh debouncer: coalesces catalog refresh requests and
keeps each write's refresh OWED until a refresh that started after the write
covers it.

A submitted refresh job covers the writes signalled before its submission
only when the job store shows it finished with a result stating
``covered_source`` (it published, or verified nothing changed). A job that
skipped, failed, was cancelled or cannot be found leaves those writes owed:
they are retried at a doubling interval capped at ``_MAX_RETRY_INTERVAL_SECONDS``,
paced, never given up. The job is read through the scheduler's
``BackgroundJobManager``, whose store is shared across nodes.
"""

import enum
import logging
import threading
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_DEBOUNCE_SECONDS = 30
#: Pacing cap on the debouncer's retry interval after a generic failure
#: (store or job tracker unavailable): retries slow down, never give up.
_MAX_RETRY_INTERVAL_SECONDS = 900.0
META_ALIAS = "cidx-meta-global"
#: Result key a refresh sets (True) only when it covered the repository's
#: source: it published, or verified nothing changed. Every other return,
#: e.g. a skip on a held lock, an unknown alias or a deferral, covers nothing.
COVERED_SOURCE = "covered_source"
_UNFINISHED_JOB_STATES = frozenset({"pending", "running", "resolving_prerequisites"})
_FINISHED_JOB_STATES = frozenset({"completed", "completed_partial"})


class RefreshJobVerdict(enum.Enum):
    RUNNING = "running"
    COVERED = "covered"
    OWED = "owed"


def refresh_job_verdict(refresh_scheduler: Any, job_id: str) -> RefreshJobVerdict:
    """Whether refresh job *job_id* covered the writes signalled before it
    was submitted. Store errors propagate (the caller retries, paced)."""
    status = refresh_scheduler.background_job_manager.get_job_status(
        job_id, "system", is_admin=True
    )
    if status is None:
        logger.info(f"cidx-meta refresh job {job_id} not found; the write stays owed")
        return RefreshJobVerdict.OWED
    state = status.get("status")
    if state in _UNFINISHED_JOB_STATES:
        return RefreshJobVerdict.RUNNING
    result = status.get("result")
    if (
        state in _FINISHED_JOB_STATES
        and isinstance(result, dict)
        and result.get(COVERED_SOURCE) is True
    ):
        return RefreshJobVerdict.COVERED
    return RefreshJobVerdict.OWED


class CidxMetaRefreshDebouncer:
    """
    Debounces cidx-meta refresh triggers to coalesce rapid batch registrations.

    When multiple repositories are registered in rapid succession, each
    on_repo_added() call attempts trigger_refresh_for_repo("cidx-meta-global").
    The first succeeds, but subsequent calls raise DuplicateJobError. This
    debouncer coalesces those failures into a single deferred refresh that fires
    after the debounce interval with no further activity.

    Usage:
        debouncer = CidxMetaRefreshDebouncer(refresh_scheduler, debounce_seconds=30)
        debouncer.signal_dirty()   # Called when DuplicateJobError is caught
        debouncer.shutdown()       # Called on server shutdown
    """

    def __init__(
        self,
        refresh_scheduler: Any,
        debounce_seconds: float = _DEFAULT_DEBOUNCE_SECONDS,
    ) -> None:
        self._refresh_scheduler = refresh_scheduler
        self._debounce_seconds = debounce_seconds
        # Paced retry interval after a generic failure (doubles, capped).
        self._retry_delay: float = debounce_seconds
        # Bumped by every signal_dirty: a submission settles only the
        # signals that arrived before it started.
        self._signal_seq: int = 0
        # Identifies the owned timer: only the callback carrying the current
        # token acts (a cancelled timer's callback may still run).
        self._timer_token: int = 0
        # (job id, signal seq it covers) of the submitted, unconfirmed job.
        self._awaiting: Optional[Tuple[str, int]] = None
        self._dirty: bool = False
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._shutdown: bool = False

    def signal_dirty(self) -> None:
        """
        Mark cidx-meta as needing a refresh and (re)start the debounce timer.

        If a timer is already running it is cancelled and a new one is started
        with the full debounce interval (coalescing behavior).  After shutdown
        the call is silently ignored.
        """
        with self._lock:
            self._signal_seq += 1
            self._dirty = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._shutdown:
                logger.debug(
                    "cidx-meta debouncer: shutdown in progress, ignoring signal_dirty"
                )
                return
            self._start_timer_locked(self._debounce_seconds)
        logger.debug(
            "cidx-meta marked dirty, debounce timer (re)started "
            f"(interval={self._debounce_seconds}s)"
        )

    def _on_timer_expired(self, timer_token: int) -> None:
        """
        Called by the timer thread when the debounce interval elapses.

        Bug #2022: acts only for the timer it belongs to (*timer_token*).
        Timer.run checks cancellation only before calling back, so a timer
        cancelled and replaced by signal_dirty can still get here: it then
        returns without touching the replacement or submitting.

        A submitted job is confirmed first (``_confirm_awaited``); the owed
        refresh is then submitted. If trigger raises DuplicateJobError the job
        is still running; re-mark dirty and retry after another debounce
        interval.  A RefreshDeferredError was persisted durably and needs
        nothing more here.  Any other exception (store or job tracker
        unavailable) keeps the refresh owed: it is logged and retried at a
        doubling interval capped at _MAX_RETRY_INTERVAL_SECONDS -- paced,
        never given up.
        """
        with self._lock:
            if timer_token != self._timer_token:
                return  # a newer timer owns the refresh
            if not self._dirty or self._shutdown:
                return
            self._timer = None
            awaiting = self._awaiting
            submitted_seq = self._signal_seq
        if awaiting is not None and not self._confirm_awaited(awaiting):
            return
        self._submit(submitted_seq)

    def _confirm_awaited(self, awaiting: Tuple[str, int]) -> bool:
        """Settle what the awaited job covered; True when a refresh is still
        owed and must be submitted now."""
        job_id, covered_seq = awaiting
        try:
            verdict = refresh_job_verdict(self._refresh_scheduler, job_id)
        except Exception as exc:
            self._retry_paced(
                f"job {job_id} status unreadable: {type(exc).__name__}: {exc}",
                failed=True,
            )
            return False
        if verdict is RefreshJobVerdict.RUNNING:
            with self._lock:
                self._schedule_retry_locked(self._debounce_seconds)
            return False
        with self._lock:
            self._awaiting = None
        if verdict is RefreshJobVerdict.OWED:
            self._retry_paced(f"job {job_id} did not cover the write", failed=False)
            return False
        self._settle_submission(covered_seq)
        with self._lock:
            return self._dirty and not self._shutdown

    def _submit(self, submitted_seq: int) -> None:
        from code_indexer.global_repos.refresh_failure_recovery import (
            RefreshDeferredError,
        )
        from code_indexer.server.repositories.background_jobs import DuplicateJobError

        try:
            job_id = self._refresh_scheduler.trigger_refresh_for_repo(META_ALIAS)
        except RefreshDeferredError as exc:
            # Bug #2022: the trigger was persisted as pending; the refresh
            # scheduler fires it once the backoff ends -- no retry spin here.
            logger.info(
                f"cidx-meta refresh deferred until {exc.backoff_until:.0f}: "
                f"persisted failure backoff; the refresh scheduler fires it "
                f"once the backoff ends"
            )
            self._settle_submission(submitted_seq)
            return
        except DuplicateJobError:
            logger.info("cidx-meta refresh still running, will retry after debounce")
            with self._lock:
                self._schedule_retry_locked(self._debounce_seconds)
            return
        except Exception as exc:
            # Bug #2022: nothing durable records this refresh yet, so it stays
            # owed (dirty) and is retried, paced, until it is submitted.
            self._retry_paced(f"{type(exc).__name__}: {exc}", failed=True)
            return
        if job_id is None:
            # Ran in-process (no job manager) or was suppressed by a quarantine.
            self._settle_submission(submitted_seq)
            return
        logger.info(f"Debounced cidx-meta refresh submitted as job {job_id}")
        with self._lock:
            self._awaiting = (job_id, submitted_seq)
            self._schedule_retry_locked(self._debounce_seconds)

    def _retry_paced(self, why: str, failed: bool) -> None:
        """Keep the refresh owed and retry at the doubling, capped interval.
        *failed*: a store or trigger failure (WARNING); otherwise a job that
        did not cover the write, e.g. skipped on a held lock (INFO)."""
        with self._lock:
            self._retry_delay = min(self._retry_delay * 2, _MAX_RETRY_INTERVAL_SECONDS)
            delay = self._retry_delay
            self._schedule_retry_locked(delay)
        logger.log(
            logging.WARNING if failed else logging.INFO,
            f"Debounced cidx-meta refresh {'failed' if failed else 'still owed'} "
            f"({why}); retrying in {delay:.1f}s",
        )

    def _settle_submission(self, submitted_seq: int) -> None:
        """A covering refresh (or durable deferral) that started at
        *submitted_seq* covers the signals up to it. A signal that arrived
        while it was in flight is a newer write it may not cover: that
        refresh stays owed, and the timer that signal started owns it."""
        with self._lock:
            self._retry_delay = self._debounce_seconds
            if self._signal_seq == submitted_seq:
                self._dirty = False

    def _schedule_retry_locked(self, delay: float) -> None:
        """Start a retry timer (caller holds the lock), unless shut down or a
        timer is already pending -- one a concurrent signal started serves."""
        if self._shutdown or self._timer is not None:
            return
        self._start_timer_locked(delay)

    def _start_timer_locked(self, delay: float) -> None:
        """Start the one owned timer (caller holds the lock); its callback
        carries a fresh token, so a callback of any earlier timer is stale."""
        self._timer_token += 1
        self._timer = threading.Timer(
            delay, self._on_timer_expired, args=(self._timer_token,)
        )
        self._timer.daemon = True
        self._timer.start()

    def shutdown(self) -> None:
        """
        Cancel any pending timer and prevent future timers from being started.

        Safe to call multiple times (idempotent).
        """
        with self._lock:
            self._shutdown = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        logger.debug("CidxMetaRefreshDebouncer shut down")
