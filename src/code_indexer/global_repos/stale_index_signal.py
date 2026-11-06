"""Stale-index signal of the refresh scheduler's Bug #1508 self-heal, and
the bound on forced reconciles that leave it unchanged.

When git reports no new commits, RefreshScheduler still forces a reconcile
if a provider's index metadata shows an interrupted run or a commit other
than the working-tree HEAD -- or recorded failures it still owes (EVO-64616:
an unattributed count or retryable paths, ``owed_failures_signal``). This
module turns the metadata of EVERY provider
(metadata_reader.read_index_states) plus the HEAD into one signal. Its key
names the provider file, the stale value and HEAD, so two checks with the
same key saw the same unchanged condition; a new commit or a status change
gives a new key.

The bound counts only forced reconciles that COMPLETED and left the same
signal (record_forced_reconcile_outcome) -- the genuine non-convergence it
exists for. A forced reconcile that failed, was cancelled or was skipped
(write lock held) never counts; it follows the refresh's own failure/retry
paths and the signal is forced again on the next cycle.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, NamedTuple, Optional, Sequence

from code_indexer.server.services.metadata_reader import IndexState
from code_indexer.services.chunk_coverage import CHUNK_COVERAGE_VERSION

logger = logging.getLogger(__name__)

_INCOMPLETE_STATUSES = ("in_progress", "failed")

# One forced reconcile plus two retries covers a transient cause; a signal
# still unchanged after that many completed reconciles is not changed by
# forcing again.
MAX_FORCED_RECONCILES_PER_SIGNAL = 3

# git's own default abbreviation length: a shorter recorded value is too
# collision-prone to accept as a prefix of HEAD (Bug #1591).
_MIN_COMMIT_PREFIX_LENGTH = 7

_HEX_DIGITS = frozenset("0123456789abcdef")

# A provider's last run did not complete (in_progress/failed): that run
# never published, so the reconcile it forces always publishes.
STATUS_SIGNAL = "status"
# A recorded commit is not HEAD.
COMMIT_DRIFT_SIGNAL = "commit_drift"
# EVO-64616: the last run recorded failures it still owes -- recorded paths
# that still exist (retryable), or a failure COUNT with no recorded path
# (unattributed, which only a reconcile finds). No upstream change will ever
# re-run them, so the refresh must, bounded like every other signal.
OWED_FAILURES_SIGNAL = "owed_failures"
# EVO-64616: a completed index not yet checked against the current chunk
# routing (chunk_coverage) -- files indexed while image chunks bypassed the
# text index are missing chunks, and reconcile re-processes only those. No
# upstream change is needed for that, so the refresh forces it once.
CHUNK_COVERAGE_SIGNAL = "chunk_coverage"
# A completed index with no trigram index: it is built in a refresh's indexing
# step, which a repository with nothing new upstream never reaches, so regex
# search on it full-scans forever. The forced run is incremental (nothing is
# re-embedded) and must publish, because the trigram index is not part of the
# metadata a "nothing changed" check reads.
TRIGRAM_SIGNAL = "trigram"


class StaleSignal(NamedTuple):
    key: str  # identity of the condition (provider file, value, HEAD)
    message: str  # human-readable reason, for logs
    kind: str  # STATUS_SIGNAL or COMMIT_DRIFT_SIGNAL
    all_completed: bool  # every provider's recorded status is "completed"

    @property
    def needs_reconcile(self) -> bool:
        """Whether the forced run must be a ``--reconcile``. A chunk coverage
        check runs in every incremental run of an unchecked index, which
        reads only the stored payloads -- not every file -- so it needs none
        (EVO-64616); neither does building a trigram index, which re-embeds
        nothing."""
        return self.kind not in (CHUNK_COVERAGE_SIGNAL, TRIGRAM_SIGNAL)

    @property
    def may_skip_unchanged_publish(self) -> bool:
        """True when a forced reconcile of this signal that changed nothing
        may publish nothing: only commit drift with every provider's run
        completed (the published snapshot already holds that content). A
        status signal means the previous run never published, so its
        reconcile always publishes even when it finds nothing to index. A
        chunk coverage check that found every file complete changed nothing
        either (EVO-64616)."""
        return (
            self.kind in (COMMIT_DRIFT_SIGNAL, CHUNK_COVERAGE_SIGNAL)
            and self.all_completed
        )


def stale_signal_from_states(
    states: Sequence[IndexState], head: Optional[str], alias_name: str
) -> Optional[StaleSignal]:
    """Return the stale-index signal, or None when every provider's metadata
    is consistent (or nothing usable is recorded).

    A status of in_progress/failed in any provider file is a signal even
    when HEAD is unknown. A recorded commit is compared with HEAD only when
    HEAD is known: the literal "unknown" (written on a git-state detection
    failure) or a value that is not a >=7-hex prefix of HEAD is drift.
    """
    for state in states:
        if state.status in _INCOMPLETE_STATUSES:
            return StaleSignal(
                key=f"{state.source} status={state.status} HEAD={head}",
                message=(
                    f"Stale/interrupted index detected for {alias_name} "
                    f"({state.source} status={state.status}) despite no new "
                    "git changes (Bug #1508)"
                ),
                kind=STATUS_SIGNAL,
                all_completed=False,
            )

    if not head:
        return None
    all_completed = all(state.status == "completed" for state in states)
    for state in states:
        recorded = state.current_commit
        if not recorded or not _recorded_commit_drifted(recorded, head):
            continue
        if recorded.strip().lower() == "unknown":
            message = (
                f"Index metadata for {alias_name} ({state.source}) has no usable "
                'recorded commit ("unknown", written on a git-state detection '
                "failure) (Bug #1591)"
            )
        else:
            message = (
                f"Index metadata for {alias_name} ({state.source}) reflects "
                f"commit {recorded} but working tree HEAD is {head} -- drifted "
                "index (Bug #1508)"
            )
        return StaleSignal(
            key=f"{state.source} current_commit={recorded} HEAD={head}",
            message=message,
            kind=COMMIT_DRIFT_SIGNAL,
            all_completed=all_completed,
        )
    return None


def owed_failures_signal(
    failures: Any, head: Optional[str], alias_name: str
) -> Optional[StaleSignal]:
    """The signal of failures the last run recorded and still owes, or None.

    *failures* is ``metadata_reader.read_recorded_failures``'s answer. Only a
    KNOWN record owes (unknown metadata is the settle path's concern): a
    retryable recorded path, or an unattributed count. Its key carries both
    counts and HEAD, so a reconcile that heals nothing leaves the same key and
    is bounded by ``admit_forced_reconcile`` like any other signal."""
    if not failures.known or not (failures.unattributed or failures.retryable_paths):
        return None
    return StaleSignal(
        key=(
            f"owed failures unattributed={failures.unattributed} "
            f"retryable={failures.retryable_paths} HEAD={head}"
        ),
        message=(
            f"Recorded indexing failures of {alias_name} are still owed "
            f"({failures.unattributed} without a path, {failures.retryable_paths} "
            "retryable file(s)) despite no new git changes (EVO-64616)"
        ),
        kind=OWED_FAILURES_SIGNAL,
        all_completed=False,
    )


def chunk_coverage_signal(
    states: Sequence[IndexState], head: Optional[str], alias_name: str
) -> Optional[StaleSignal]:
    """The signal of a completed provider index not yet checked against the
    current chunk routing, or None (EVO-64616, ``chunk_coverage``).

    Only a COMPLETED run's metadata is judged: any other status is the
    status signal's concern. The key carries the recorded version and HEAD,
    so a reconcile that does not record the current version is bounded by
    ``admit_forced_reconcile`` like any other signal."""
    all_completed = all(state.status == "completed" for state in states)
    for state in states:
        if (
            state.status != "completed"
            or state.chunk_coverage_version >= CHUNK_COVERAGE_VERSION
        ):
            continue
        return StaleSignal(
            key=(
                f"{state.source} chunk_coverage_version="
                f"{state.chunk_coverage_version} HEAD={head}"
            ),
            message=(
                f"Index of {alias_name} ({state.source}) was not checked for "
                "files missing some of their chunks (EVO-64616)"
            ),
            kind=CHUNK_COVERAGE_SIGNAL,
            all_completed=all_completed,
        )
    return None


def trigram_missing_signal(
    states: Sequence[IndexState], head: Optional[str], alias_name: str
) -> Optional[StaleSignal]:
    """The signal of a completed index that has no trigram index, or None.

    The caller has established that the trigram index is missing and can be
    built here. Only a COMPLETED run's index is judged: any other status is
    the status signal's concern, and no recorded state means no index. The
    key carries HEAD, so a run that builds nothing leaves the same key and is
    bounded by ``admit_forced_reconcile`` like any other signal."""
    if not states or not all(state.status == "completed" for state in states):
        return None
    return StaleSignal(
        key=f"trigram index missing HEAD={head}",
        message=f"{alias_name} has no trigram index for regex search",
        kind=TRIGRAM_SIGNAL,
        all_completed=True,
    )


class ForcedRunPacer:
    """At most ``max_runs`` admitted per ``window_seconds`` (sliding window).

    Paces a backlog -- every repository of a fleet is missing the same index --
    so the forced runs drain gradually instead of all at once. It bounds one
    process; the durable per-repository bound is ``admit_forced_reconcile``.
    A refused attempt records nothing, so it neither extends the window nor
    touches a repository's forced-reconcile count (the caller must not clear
    that count to defer: a cleared count would never reach its bound).
    """

    def __init__(
        self,
        max_runs: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_runs = max_runs
        self.window_seconds = window_seconds
        self._clock = clock
        self._admitted: Deque[float] = deque()
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        now = self._clock()
        with self._lock:
            while self._admitted and now - self._admitted[0] >= self.window_seconds:
                self._admitted.popleft()
            if len(self._admitted) >= self.max_runs:
                return False
            self._admitted.append(now)
            return True


#: Turns the trigram backfill ON (``1`` / ``true``); off when unset. A refresh
#: that forces a run for every repository missing a trigram index touches the
#: whole fleet, so an environment opts in deliberately, the way the semantic
#: backfill ships off until an operator enables it.
TRIGRAM_BACKFILL_ENV = "CIDX_TRIGRAM_BACKFILL"


def trigram_backfill_enabled() -> bool:
    return os.environ.get(TRIGRAM_BACKFILL_ENV, "") in ("1", "true", "True")


#: Forced trigram builds admitted per process per window. Each is a full refresh
#: cycle (incremental index, trigram build, publish of a new snapshot), so a
#: fleet of ~900 repositories drains over hours, not in one burst.
TRIGRAM_FORCED_RUNS_PER_WINDOW = 4
TRIGRAM_FORCED_WINDOW_SECONDS = 300.0
trigram_forced_run_pacer = ForcedRunPacer(
    max_runs=TRIGRAM_FORCED_RUNS_PER_WINDOW,
    window_seconds=TRIGRAM_FORCED_WINDOW_SECONDS,
)


def admit_forced_reconcile(
    store: Any, alias_name: str, signal: Optional[StaleSignal]
) -> bool:
    """Decide whether this refresh forces a reconcile for ``signal``.

    ``store`` is the golden-repo metadata backend (durable, shared by every
    node). No signal clears the stored state. Admitting records nothing: an
    attempt counts only once it completes (record_forced_reconcile_outcome).
    A signal already left unchanged by MAX_FORCED_RECONCILES_PER_SIGNAL
    completed forced reconciles is not forced again until it changes; the
    first such refusal logs one ERROR (recorded by bumping the count past
    the bound, so it is logged once even across restarts), later ones log
    at DEBUG so a stuck repository does not log an error every cycle.
    """
    state = store.get_forced_reconcile_state(alias_name)
    if signal is None:
        if state is not None:
            store.clear_forced_reconcile_state(alias_name)
        return False

    same_signal = state is not None and state["signal"] == signal.key
    attempts = int(state["attempt_count"]) if same_signal and state else 0

    if attempts > MAX_FORCED_RECONCILES_PER_SIGNAL:
        logger.debug(
            "Not forcing reconcile for %s: unchanged stale signal (%s)",
            alias_name,
            signal.key,
        )
        return False

    if attempts == MAX_FORCED_RECONCILES_PER_SIGNAL:
        logger.error(
            "%s; stopped forcing reconcile for %s: %d forced reconciles "
            "completed and the same signal (%s) is still present. Not "
            "forcing again until the signal changes (new commit or status "
            "change) -- the index metadata needs investigation.",
            signal.message,
            alias_name,
            attempts,
            signal.key,
        )
        # Bump past the bound: later refusals see attempts > MAX (DEBUG).
        store.record_forced_reconcile(alias_name, signal.key)
        return False

    logger.warning("%s -- forcing reconcile to catch up", signal.message)
    return True


def record_forced_reconcile_outcome(
    store: Any,
    alias_name: str,
    forced: StaleSignal,
    after: Optional[StaleSignal],
) -> None:
    """Record a forced reconcile of ``forced`` that COMPLETED successfully,
    given ``after``, the signal re-read once it returned.

    The same signal still present is one non-converging attempt (counted
    toward MAX_FORCED_RECONCILES_PER_SIGNAL). Otherwise the forced signal is
    gone and the state is cleared; a different signal starts its own count
    once it is forced and completes. Never call this for a reconcile that
    raised, was cancelled or was skipped.
    """
    if after is not None and after.key == forced.key:
        attempts = store.record_forced_reconcile(alias_name, forced.key)
        logger.warning(
            "Forced reconcile for %s completed but the same stale signal is "
            "still present (%s); %d of %d forced reconciles used",
            alias_name,
            forced.key,
            attempts,
            MAX_FORCED_RECONCILES_PER_SIGNAL,
        )
        return
    store.clear_forced_reconcile_state(alias_name)


def _recorded_commit_drifted(recorded: str, head: str) -> bool:
    """True unless ``recorded`` is HEAD or a genuine (>=7 hex characters,
    case-insensitive) prefix of it (Bug #1591 prefix tolerance)."""
    recorded_lower = recorded.strip().lower()
    if recorded_lower == "unknown":
        return True
    is_hex_fragment = len(recorded_lower) >= _MIN_COMMIT_PREFIX_LENGTH and all(
        c in _HEX_DIGITS for c in recorded_lower
    )
    return not (is_hex_fragment and head.lower().startswith(recorded_lower))
