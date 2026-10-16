"""Stale-index signal of the refresh scheduler's Bug #1508 self-heal, and
the bound on forced reconciles that leave it unchanged.

When git reports no new commits, RefreshScheduler still forces a reconcile
if a provider's index metadata shows an interrupted run or a commit other
than the working-tree HEAD. This module turns the metadata of EVERY provider
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
from typing import Any, NamedTuple, Optional, Sequence

from code_indexer.server.services.metadata_reader import IndexState

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


class StaleSignal(NamedTuple):
    key: str  # identity of the condition (provider file, value, HEAD)
    message: str  # human-readable reason, for logs
    kind: str  # STATUS_SIGNAL or COMMIT_DRIFT_SIGNAL
    all_completed: bool  # every provider's recorded status is "completed"

    @property
    def may_skip_unchanged_publish(self) -> bool:
        """True when a forced reconcile of this signal that changed nothing
        may publish nothing: only commit drift with every provider's run
        completed (the published snapshot already holds that content). A
        status signal means the previous run never published, so its
        reconcile always publishes even when it finds nothing to index."""
        return self.kind == COMMIT_DRIFT_SIGNAL and self.all_completed


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
