"""Bounded same-node temporal query dedup cache -- Story #1400 Phase 6.

FINAL LOCKED DESIGN (adjudicated, Codex's stricter design adopted over
Opus's "generous TTL for everything" proposal): this project has an
explicit no-artificial-timeout policy for legitimate long-running work
(Bug #1218). A dedup index that could evict a still-running entry under
memory pressure would silently duplicate a multi-minute, ~70-shard query --
a real, needless-duplicate-work bug. This cache closes that entirely:

- Active (pending/running) entries are NEVER evicted by TTL or LRU, full
  stop -- only TERMINAL entries (a resolved job's dedup record, kept
  briefly so a fast-follow identical query still joins the just-finished
  result rather than redoing the work) get a TTL.
- A SINGLE global mutex (never a per-signature lock dict, which would
  itself be an unbounded-lifecycle structure -- exactly the leak class
  this story eliminates elsewhere) guards lookup -> status-decision ->
  submit -> publish. Never held during the wait loop or worker execution;
  contention is irrelevant at this request rate.
- Capped at 4096 total entries. If the cap is reached while EVERY entry is
  still active, a new unique submission is rejected with
  TemporalDedupCapacityExhaustedError rather than evicting live work.

INTENTIONALLY per-node (in-RAM): cross-node dedup is explicitly out of
scope / deferred (dedup is per-node -- on a cluster, MCP/REST requests
routed to different nodes will not join the same job).

Bug #1547 defect 2: the terminal TTL must never exceed
payload_cache_ttl_seconds -- the dedup'd result a terminal entry points at
lives in PayloadCache, itself TTL-evicted. A dedup entry outliving that
payload produced a spurious "result expired -- resubmit" response for a
fast-follow query that joined the still-live-looking dedup entry. See
_effective_terminal_ttl_seconds().

Bug #1547 Finding 3 (Codex review of the defect-2 fix above): matching the
TTL *duration* to payload_cache_ttl_seconds was not sufficient on its own,
because the two windows had different ORIGINS. terminal_observed_at was
stamped when a LATER request first OBSERVED a job as terminal, not when
the job actually completed -- so a duplicate arriving well after the
payload's real TTL window (anchored at the snapshot WRITE, i.e. real
completion) could still see elapsed=0 and incorrectly rejoin an entry
whose PayloadCache snapshot had already been evicted. Fixed by anchoring
terminal_observed_at on the job's ACTUAL completion time whenever it is
known: status_check's return contract is extended (backward-compatibly --
a bare status string/None is still accepted, unchanged, for every existing
caller) to optionally return a (status, completed_at_epoch) tuple, where
completed_at_epoch is a WALL-CLOCK (time.time()-comparable) timestamp --
see _unpack_status_result / _anchor_terminal_observed_at for the
monotonic/wall-clock conversion this requires (this cache's own elapsed-
time math uses time.monotonic(), a DIFFERENT clock that cannot be mixed
with a wall-clock value directly). Separately, the config-read-failure
fallback used DEFAULT_TERMINAL_TTL_SECONDS (3600s) -- LONGER than
payload_cache_ttl_seconds' own compiled default (900s) -- which
re-introduced this exact bug whenever a config_service read failed; fixed
by a dedicated, conservative _CONFIG_READ_FAILURE_FALLBACK_TTL_SECONDS.
"""

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple

if TYPE_CHECKING:
    from code_indexer.server.services.config_service import ConfigService

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRIES = 4096
DEFAULT_TERMINAL_TTL_SECONDS = 3600.0

#: Bug #1547 Finding 3: the FAIL-SAFE used ONLY when config_service is
#: supplied but its read raises. Must be conservative -- i.e. never LONGER
#: than the payload TTL it protects -- so a config-read outage cannot
#: re-introduce the "dedup entry outlives its payload" bug. Matches
#: CacheConfig.payload_cache_ttl_seconds' OWN compiled default (900s), not
#: DEFAULT_TERMINAL_TTL_SECONDS (3600s, which is longer and was the root
#: cause of this exact symptom when config reads fail). DEFAULT_TERMINAL_
#: TTL_SECONDS itself is unchanged and still governs the (different,
#: deliberate) "no config_service supplied at all" case.
_CONFIG_READ_FAILURE_FALLBACK_TTL_SECONDS = 900.0

# Statuses treated as terminal (mirrors BackgroundJob's terminal set, plus
# None for "job not found / unauthorized").
_TERMINAL_STATUSES = {None, "failed", "completed", "cancelled"}


class TemporalDedupCapacityExhaustedError(Exception):
    """Raised when the dedup cache is full of ACTIVE entries and a new,
    genuinely-unique signature needs a slot. Active work is never evicted
    to make room -- the caller should surface this as HTTP 503 with
    error_code TEMPORAL_DEDUP_CAPACITY_EXHAUSTED."""


def canonical_signature(payload: Dict[str, Any]) -> str:
    """Sha256 of a canonical (sort_keys, compact-separator) JSON encoding.

    Callers are responsible for normalizing list-typed fields (sorted,
    deduped) BEFORE calling this -- mirrors TemporalWorkerInput's
    diff_type canonicalization -- so two logically-identical payloads with
    differently-ordered lists still hash identically.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _unpack_status_result(result: Any) -> Tuple[Optional[str], Optional[float]]:
    """Bug #1547 Finding 3: status_check may return EITHER a bare status
    string/None (the original, pre-Finding-3 contract -- still accepted
    for backward compatibility with every existing caller/test that
    predates this fix) OR a (status, completed_at_epoch) tuple, where
    completed_at_epoch is a wall-clock (time.time()-comparable) timestamp
    of the job's ACTUAL completion. A bare (non-tuple) result carries no
    completion-time information, so it unpacks to (result, None) -- the
    caller then anchors on "now" (see _anchor_terminal_observed_at),
    matching this cache's original behavior exactly for those callers.

    A tuple result is validated (fail loud on a malformed caller -- this
    is a programming error in status_check, never a legitimate degraded
    state to silently paper over): it must have exactly 2 elements, and
    completed_at_epoch must be None or a real (non-bool) int/float.

    Raises:
        ValueError: a tuple of length != 2.
        TypeError: completed_at_epoch is neither None nor a real number.
    """
    if isinstance(result, tuple):
        if len(result) != 2:
            raise ValueError(
                "status_check returned a tuple of length "
                f"{len(result)}, expected exactly 2: (status, completed_at_epoch)"
            )
        status, completed_at_epoch = result
        if completed_at_epoch is not None and (
            isinstance(completed_at_epoch, bool)
            or not isinstance(completed_at_epoch, (int, float))
        ):
            raise TypeError(
                "status_check's completed_at_epoch must be None, int, or "
                f"float, got {type(completed_at_epoch).__name__}"
            )
        return status, completed_at_epoch
    return result, None


def _anchor_terminal_observed_at(completed_at_epoch: Optional[float]) -> float:
    """Bug #1547 Finding 3: convert a wall-clock completion timestamp
    (time.time()-based) into this cache's monotonic time base, so the
    terminal window is anchored on when the job ACTUALLY completed rather
    than whenever a later request first observes it as terminal.

    time.monotonic() and time.time() are DIFFERENT clocks (monotonic is
    immune to system clock adjustments during the TTL window, which is why
    this cache uses it for its own elapsed-time math) -- they cannot be
    subtracted directly. The conversion below captures both clocks AT THE
    SAME INSTANT (this call), computes how long ago (in wall-clock
    seconds) the job completed, then subtracts that same duration from the
    CURRENT monotonic reading -- yielding the monotonic-clock instant that
    corresponds to the real completion time.

    Falls back to "now" (time.monotonic(), this cache's original pre-fix
    anchor) when completed_at_epoch is unavailable -- see
    _unpack_status_result.

    Bug #1547 round-2 FIX 4: a NEGATIVE computed age (completed_at_epoch in
    the future -- a backward wall-clock step between real completion and
    this observation, or simply a malformed timestamp) must fail toward
    EXPIRY, never toward a fresh full terminal TTL. The original
    `max(0.0, ...)` clamp did the opposite: it silently floored the
    negative age to 0, anchoring the entry at "now" and granting it a
    brand-new full TTL window even though the entry may already be past
    its real terminal window -- a stale/evicted PayloadCache snapshot could
    then be rejoined. A FORWARD clock step is the safe direction (it only
    makes the computed age too large, causing early expiry) and is
    unaffected by this change. Returning -inf here makes
    `time.monotonic() - entry.terminal_observed_at` always +inf, i.e.
    always >= any finite terminal_ttl_seconds -- guaranteed immediate
    expiry regardless of the configured TTL, without this function needing
    to know that TTL.
    """
    if completed_at_epoch is None:
        return time.monotonic()
    raw_age_seconds = time.time() - completed_at_epoch
    if raw_age_seconds < 0.0:
        logger.warning(
            "Bug #1547 FIX 4: completed_at_epoch=%s is in the future "
            "(raw age %.3fs) -- backward wall-clock step or malformed "
            "timestamp; anchoring toward immediate expiry instead of "
            "granting a fresh terminal TTL",
            completed_at_epoch,
            raw_age_seconds,
        )
        return float("-inf")
    return time.monotonic() - raw_age_seconds


@dataclass
class _DedupEntry:
    job_id: str
    terminal_observed_at: Optional[float] = None


class TemporalDedupCache:
    """Bounded, single-mutex, same-node signature -> job_id dedup index."""

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        terminal_ttl_seconds: Optional[float] = None,
        config_service: Optional["ConfigService"] = None,
    ) -> None:
        """
        Args:
            terminal_ttl_seconds: explicit TTL override. When given (not
                None), it is ALWAYS authoritative -- takes precedence over
                config_service, unchanged behavior for every existing
                caller/test that passes this argument explicitly. None
                (the default) means "no explicit override": the effective
                TTL then comes from config_service if one is supplied, else
                the DEFAULT_TERMINAL_TTL_SECONDS module constant.
            config_service: Bug #1547 -- when supplied (and no explicit
                terminal_ttl_seconds override), the terminal TTL is read
                LIVE from config_service.get_config().cache_config.
                payload_cache_ttl_seconds on every use. This keeps a dedup
                entry from ever outliving the PayloadCache snapshot it
                points at -- DEFAULT_TERMINAL_TTL_SECONDS (3600s) can
                otherwise exceed payload_cache_ttl_seconds (900s default),
                producing a spurious "result expired -- resubmit" for a
                fast-follow query that joins a dedup entry whose payload
                has already been evicted.
        """
        if max_entries <= 0:
            raise ValueError(f"max_entries must be > 0, got {max_entries}")
        if terminal_ttl_seconds is not None and terminal_ttl_seconds < 0:
            raise ValueError(
                f"terminal_ttl_seconds must be >= 0, got {terminal_ttl_seconds}"
            )
        self._lock = threading.Lock()
        self._entries: Dict[str, _DedupEntry] = {}
        self._max_entries = max_entries
        self._terminal_ttl_seconds_override = terminal_ttl_seconds
        self._config_service = config_service

    def _effective_terminal_ttl_seconds(self) -> float:
        """Bug #1547: live terminal-TTL resolution.

        Precedence: an explicit constructor override always wins; else
        config_service.get_config().cache_config.payload_cache_ttl_seconds
        when config_service is set and the read succeeds; else the
        DEFAULT_TERMINAL_TTL_SECONDS module constant governs the
        "no config_service supplied at all" case. Bug #1547 Finding 3: a
        config-read FAILURE (config_service supplied but the read raises)
        is a DIFFERENT case and must fall back to the conservative
        _CONFIG_READ_FAILURE_FALLBACK_TTL_SECONDS instead --
        DEFAULT_TERMINAL_TTL_SECONDS (3600s) exceeds
        payload_cache_ttl_seconds' own compiled default (900s) and
        re-introduces the "dedup entry outlives its payload" bug whenever
        config reads fail. Never raises.
        """
        if self._terminal_ttl_seconds_override is not None:
            return self._terminal_ttl_seconds_override
        if self._config_service is not None:
            try:
                return float(
                    self._config_service.get_config().cache_config.payload_cache_ttl_seconds
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "TemporalDedupCache: config_service read failed, "
                    "falling back to the conservative "
                    "_CONFIG_READ_FAILURE_FALLBACK_TTL_SECONDS=%s (never "
                    "DEFAULT_TERMINAL_TTL_SECONDS=%s, which can exceed the "
                    "payload cache's own TTL and re-introduce Bug #1547): %s",
                    _CONFIG_READ_FAILURE_FALLBACK_TTL_SECONDS,
                    DEFAULT_TERMINAL_TTL_SECONDS,
                    exc,
                )
                return _CONFIG_READ_FAILURE_FALLBACK_TTL_SECONDS
        return DEFAULT_TERMINAL_TTL_SECONDS

    def _evict_expired_terminal_entries_locked(self) -> None:
        """Must be called with self._lock held. Removes terminal entries
        whose TTL has elapsed -- NEVER touches active entries."""
        now = time.monotonic()
        terminal_ttl_seconds = self._effective_terminal_ttl_seconds()
        expired = [
            sig
            for sig, entry in self._entries.items()
            if entry.terminal_observed_at is not None
            and (now - entry.terminal_observed_at) >= terminal_ttl_seconds
        ]
        for sig in expired:
            del self._entries[sig]

    def _try_join_existing_entry_locked(
        self, signature: str, status_check: Callable[[str], Any]
    ) -> Optional[str]:
        """Must be called with self._lock held.

        Returns a joinable job_id if the existing entry for `signature` is
        active, or a terminal entry still within its terminal TTL window
        (anchored on the job's real completion time -- Bug #1547 Finding
        3). Deletes the entry and returns None when it must be replaced by
        a fresh submission (absent job, or terminal past its TTL), or when
        no entry exists at all for this signature.
        """
        entry = self._entries.get(signature)
        if entry is None:
            return None

        status, completed_at_epoch = _unpack_status_result(status_check(entry.job_id))
        if status not in _TERMINAL_STATUSES:
            return entry.job_id

        # Terminal (or absent). Within the terminal TTL window, a
        # fast-follow identical query still joins the just-finished
        # result -- avoid recomputation.
        terminal_ttl_seconds = self._effective_terminal_ttl_seconds()
        if entry.terminal_observed_at is None:
            entry.terminal_observed_at = _anchor_terminal_observed_at(
                completed_at_epoch
            )
        elapsed = time.monotonic() - entry.terminal_observed_at
        if status is not None and elapsed < terminal_ttl_seconds:
            return entry.job_id

        # Absent, or terminal past its TTL -> replace with a fresh
        # submission (caller falls through to the insert-new-entry path).
        del self._entries[signature]
        return None

    def get_or_submit(
        self,
        signature: str,
        status_check: Callable[[str], Any],
        submit: Callable[[], str],
    ) -> str:
        """Return the job_id for `signature`, joining an existing
        active-or-within-TTL-terminal entry, or submitting a new job.

        Args:
            signature: canonical_signature() output for this request.
            status_check: given a job_id, returns EITHER a bare status
                string (or None for not-found/unauthorized) -- the
                original contract, still accepted for backward
                compatibility -- OR a (status, completed_at_epoch) tuple
                (Bug #1547 Finding 3): the job's ACTUAL completion time,
                wall-clock seconds since the epoch (time.time()-
                comparable; BackgroundJob.completed_at's ISO string,
                parsed), used to anchor this entry's terminal window on
                when the job REALLY finished rather than whenever a later
                request first observes it as terminal -- so the entry's
                validity window stays a true subset of the PayloadCache
                snapshot's own TTL window (which starts at the snapshot
                WRITE, i.e. real completion). completed_at_epoch is only
                meaningful (and only read) when status is a genuine
                terminal status; omit/None falls back to "now" (this
                cache's original anchor). See _unpack_status_result and
                _try_join_existing_entry_locked.
            submit: zero-arg callable that submits a new job and returns
                its job_id. Called at most once per invocation, only when
                no joinable entry exists.

        Raises:
            TemporalDedupCapacityExhaustedError: cache is full of active
                entries and this is a genuinely new signature.
        """
        with self._lock:
            joinable_job_id = self._try_join_existing_entry_locked(
                signature, status_check
            )
            if joinable_job_id is not None:
                return joinable_job_id

            if len(self._entries) >= self._max_entries:
                self._evict_expired_terminal_entries_locked()
            if len(self._entries) >= self._max_entries:
                raise TemporalDedupCapacityExhaustedError(
                    f"Temporal dedup cache is at capacity ({self._max_entries} "
                    "entries, all active) -- cannot register a new unique "
                    "query signature without evicting live work."
                )

            job_id = submit()
            self._entries[signature] = _DedupEntry(job_id=job_id)
            return job_id


_singleton: Optional[TemporalDedupCache] = None
_singleton_lock = threading.Lock()


def get_temporal_dedup_cache() -> TemporalDedupCache:
    """Return the process-wide (per-node, in-RAM) TemporalDedupCache
    singleton, constructing it on first access. Story #1400: this is the
    ONE dedup cache both the MCP (search_code) and REST (POST /api/query)
    live-wiring doors share -- an identical logical query landing on
    either door via the SAME node joins the same in-flight job.

    Bug #1547: wired with the live ConfigService singleton so its terminal
    TTL tracks payload_cache_ttl_seconds (Web UI configurable) instead of
    staying pinned to the hardcoded 3600s default."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                from code_indexer.server.services.config_service import (
                    get_config_service,
                )

                _singleton = TemporalDedupCache(config_service=get_config_service())
    return _singleton
