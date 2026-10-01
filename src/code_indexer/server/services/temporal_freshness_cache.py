"""Bug #1547 Finding 1: bounded, rate-limited, single-flight cache for the
live temporal dedup freshness signal.

_compute_temporal_freshness_signal (temporal_live_dispatch.py) does a
directory scan plus one os.stat per temporal shard against the golden-repos
NFSv3 `hard` mount, PLUS a golden-lineage metadata-store lookup
(_resolve_golden_temporal_context) -- both real, potentially-blocking I/O
that must not run synchronously on every live temporal dispatch (20
concurrent queries against a 70-shard repo == ~1400 independent blocking
stats in caller threads if uncached).

Mirrors Bug #1538's hnsw_index_cache freshness-check pattern (see that
module's _FRESHNESS_RECHECK_MIN_INTERVAL_SECONDS / _freshness_checking /
_verify_entry_freshness), applied to a different payload (an
application-level freshness SIGNAL computed from potentially many blocking
stats plus a metadata lookup, rather than a single hnsw_index.bin
fingerprint check against one already-loaded cache entry):

    (a) the compute() call that may block on NFS I/O NEVER runs while
        holding the cache's lock;
    (b) a per-key in-flight guard (a threading.Event sentinel, mirroring
        hnsw_index_cache._loading / _freshness_checking) ensures AT MOST
        ONE thread performs the blocking compute() for a given key at a
        time -- concurrent callers for the SAME key wait and then reuse
        the result, rather than piling into N independent blocking calls;
    (c) a minimum re-check interval bounds how often compute() is invoked
        AT ALL for a given key, regardless of request rate.

Bug #1547 Finding 2 (generation tagging): each NEW recompute pass (a cache
miss, or a stale entry past the recheck interval) is assigned a
process-wide, strictly-increasing generation id, passed into compute() so
the CALLER (temporal_live_dispatch._compute_temporal_freshness_signal) can
tag any sub-result it cannot verify (a failed per-shard stat, a failed
golden-lineage resolution, an unexpected exception) with a marker that
embeds this generation instead of a constant sentinel (None / []). Two
degraded computations from the SAME recompute pass are byte-identical (so
concurrent identical dispatches still dedup with each other -- the whole
point of this cache); a degraded computation from a LATER pass is always
DIFFERENT from one from an earlier pass, so it can never be mistaken for
(and join a dedup entry created under) an earlier, possibly pre-refresh,
freshness state. See temporal_live_dispatch.py's _DEGRADED_MARKER_TAG.

Bounded like TemporalDedupCache (temporal_dedup_cache.py): the key space
here is (username, repository_alias) pairs, which can grow unboundedly
over a long-lived server process. Unlike TemporalDedupCache -- which never
evicts an ACTIVE entry, only expired terminal ones -- every entry in this
cache is a cheap, re-derivable signal with no in-flight work attached, so a
plain oldest-entry eviction on overflow is sufficient and never discards
anything irreplaceable.

Bug #1547 round-2 hardening (Codex review), two further fixes:

FIX 1 -- the generation counter (Finding 2 above) is now PROCESS-WIDE
(module-level, shared by every cache instance) rather than per-instance.
A per-instance counter restarts at 1 on a fresh instance, so if a cache
instance were ever replaced while the SEPARATE TemporalDedupCache
singleton survives, the new instance's first degraded signal would
byte-match the old instance's first degraded signal and could rejoin a
pre-refresh dedup entry -- exactly the bug Finding 2 exists to prevent.
Latent in production today (both singletons share the process's full
lifetime), but a trap for any future code resetting one independently of
the other. See _next_process_wide_generation().

FIX 3 -- the cold-key single-flight wait is now BOUNDED. Property (b)
above (at most one thread inside compute() per key) is unchanged for the
common case, but a caller waiting on ANOTHER thread's in-flight compute()
for a key that has NO cached entry at all (a genuinely cold key -- as
opposed to a STALE-but-present entry, whose waiters are unaffected by this
fix) now waits at most cold_key_wait_timeout_seconds before giving up and
returning a degraded, generation-tagged fallback instead of blocking
further. This matters because on a `hard` NFS mount a stuck os.stat blocks
in UNINTERRUPTIBLE kernel retry (it does not fail), and this wait lands on
the REQUEST DISPATCH thread -- an unbounded wait there can exhaust the
request pool during an outage. The fallback is tagged with the SAME
generation the stuck computer claimed, so multiple callers that all time
out waiting on the SAME stuck computer still collapse into one dedup
signature (no duplicate-submission stampede), while remaining distinct
from any other recompute pass's signal. See _cold_wait_timeout_signal().
"""

import itertools
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

#: Mirrors hnsw_index_cache._FRESHNESS_RECHECK_MIN_INTERVAL_SECONDS exactly
#: -- same golden-repos NFSv3 `hard` mount, same blocking-stat hazard.
DEFAULT_MIN_RECHECK_INTERVAL_SECONDS = 2.0

#: Bug #1547 round-2 FIX 3: default bound on how long a caller may wait on
#: ANOTHER thread's in-flight COLD-key compute() before giving up and
#: returning a degraded fallback instead of blocking further. Chosen well
#: below the smallest configured request-handler timeout in this codebase
#: (SearchTimeoutsConfig.default_handler_timeout_seconds, 60s default) so a
#: genuinely stuck compute() (a hard-mount NFS hang) can never itself
#: exhaust that budget, while comfortably above the wall-clock cost of a
#: normal (non-hung) compute pass (a directory scan plus a handful of
#: os.stat calls, typically low milliseconds, occasionally a second or two
#: under load) so it essentially never fires during normal operation.
DEFAULT_COLD_KEY_WAIT_TIMEOUT_SECONDS = 5.0

#: Mirrors TemporalDedupCache.DEFAULT_MAX_ENTRIES -- same per-node,
#: unboundedly-growing key-space concern (username, repository_alias).
DEFAULT_MAX_ENTRIES = 4096

#: Messi #14 anti-unbounded-loop: get_or_compute's claim/wait loop can only
#: re-iterate when a concurrent computer for the SAME key releases without
#: caching (i.e. compute() raised), or after a bounded cold-key wait times
#: out (which now returns immediately rather than re-looping). A real
#: request storm of PERSISTENT compute() failures for one key is the only
#: way to approach this bound; exceeding it indicates a non-converging
#: failure pattern, not a single unlucky interleaving.
_MAX_CLAIM_ATTEMPTS = 1000

#: Sentinel distinguishing "no cached signal available" from a legitimate
#: cached signal value of None.
_NOT_CACHED = object()

#: Bug #1547 round-2 FIX 3: tag embedded in the fallback signal returned
#: when a cold-key waiter's bounded wait times out. Mirrors
#: temporal_live_dispatch._DEGRADED_MARKER_TAG's role -- a literal that
#: cannot collide with a real computed signal -- but is deliberately a
#: SEPARATE, cache-level constant: this module has no knowledge of (and
#: must not depend on) the specific shape any particular caller's compute()
#: produces.
_COLD_WAIT_TIMEOUT_MARKER_TAG = "__temporal_freshness_cache_cold_wait_timeout__"

#: Bug #1547 round-2 FIX 1: process-wide (NEVER per-instance) generation
#: counter, shared by EVERY TemporalFreshnessSignalCache instance in this
#: process. See the module docstring's FIX 1 discussion.
_process_wide_generation_lock = threading.Lock()
_process_wide_generation_counter = itertools.count(1)


def _next_process_wide_generation() -> int:
    """Thread-safe: itertools.count().__next__ is already atomic under
    CPython's GIL, but this explicit lock removes any doubt and costs
    nothing at this call rate (at most one call per cache-instance
    recompute-pass claim, across the whole process)."""
    with _process_wide_generation_lock:
        return next(_process_wide_generation_counter)


def _cold_wait_timeout_signal(generation: int) -> List[Any]:
    """Bug #1547 round-2 FIX 3: the fallback signal returned when a
    cold-key waiter's bounded wait on another thread's in-flight compute()
    times out. Generation-tagged (mirrors Finding 2's rationale, embedding
    the STUCK computer's own claimed generation) so it can never collapse
    with a signal computed in a DIFFERENT recompute pass, while multiple
    callers that all time out waiting on the SAME stuck computer share
    this SAME value (same generation) and therefore still dedup with each
    other rather than each independently resubmitting duplicate work."""
    return [_COLD_WAIT_TIMEOUT_MARKER_TAG, generation]


@dataclass
class _FreshnessCacheEntry:
    signal: Any
    generation: int
    computed_at: float = field(default_factory=time.monotonic)


def _validate_key(key: Any) -> None:
    if not isinstance(key, str):
        raise TypeError(f"key must be a str, got {type(key).__name__}")
    if not key:
        raise ValueError("key must be a non-empty string")


def _validate_compute(compute: Any) -> None:
    if not callable(compute):
        raise TypeError("compute must be callable")


def _validate_interval(value: Any, *, param_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            f"{param_name} must be an int or float, got {type(value).__name__}"
        )
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{param_name} must be a finite value >= 0, got {value}")


def _validate_max_entries(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"max_entries must be an int, got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"max_entries must be > 0, got {value}")


class TemporalFreshnessSignalCache:
    """Per-node, bounded, single-flight, rate-limited signal cache."""

    def __init__(
        self,
        min_recheck_interval_seconds: float = DEFAULT_MIN_RECHECK_INTERVAL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        cold_key_wait_timeout_seconds: float = DEFAULT_COLD_KEY_WAIT_TIMEOUT_SECONDS,
    ) -> None:
        _validate_interval(
            min_recheck_interval_seconds, param_name="min_recheck_interval_seconds"
        )
        _validate_max_entries(max_entries)
        _validate_interval(
            cold_key_wait_timeout_seconds, param_name="cold_key_wait_timeout_seconds"
        )
        self._interval = min_recheck_interval_seconds
        self._max_entries = max_entries
        self._cold_key_wait_timeout_seconds = cold_key_wait_timeout_seconds
        self._lock = threading.Lock()
        self._entries: Dict[str, _FreshnessCacheEntry] = {}
        # Bug #1547 round-2 FIX 3: each in-flight claim now carries its
        # generation alongside its event, so a cold-key waiter that times
        # out can tag its fallback with the SAME generation the stuck
        # computer claimed (see _cold_wait_timeout_signal).
        self._computing: Dict[str, Tuple[threading.Event, int]] = {}

    def _evict_oldest_locked(self) -> None:
        """Must be called with self._lock held. Drops the single
        least-recently-computed entry -- every entry here is a cheap,
        re-derivable signal (no in-flight work), so a plain oldest-first
        eviction is sufficient (unlike TemporalDedupCache, which must
        never evict an ACTIVE entry)."""
        if not self._entries:
            return
        oldest_key = min(self._entries, key=lambda k: self._entries[k].computed_at)
        del self._entries[oldest_key]

    def _claim_locked(
        self, key: str
    ) -> Tuple[Any, Optional[int], Optional[threading.Event], Optional[int]]:
        """Returns (cached_signal_or__NOT_CACHED, generation_or_None,
        event_or_None, cold_wait_generation_or_None):
          - a real signal + (None, None, None): a fresh-enough cached
            entry.
          - (_NOT_CACHED, None, event, None): another thread is computing
            AND an entry (possibly stale) already exists for this key --
            caller should wait UNBOUNDED on `event` then retry (unchanged
            pre-FIX-3 behavior -- explicitly out of FIX 3's scope).
          - (_NOT_CACHED, None, event, computing_generation): another
            thread is computing and NO entry exists for this key at all
            (a genuinely cold key) -- caller should wait BOUNDED on
            `event`; on timeout, fall back to
            _cold_wait_timeout_signal(computing_generation) rather than
            continuing to block (Bug #1547 round-2 FIX 3).
          - (_NOT_CACHED, generation, event, None): caller is now the
            computer for this key, holding generation's claim.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and (
                time.monotonic() - entry.computed_at < self._interval
            ):
                return entry.signal, None, None, None

            claim = self._computing.get(key)
            if claim is not None:
                computing_event, computing_generation = claim
                cold_wait_generation = computing_generation if entry is None else None
                return _NOT_CACHED, None, computing_event, cold_wait_generation

            event = threading.Event()
            generation = _next_process_wide_generation()
            self._computing[key] = (event, generation)
            return _NOT_CACHED, generation, event, None

    def _compute_and_store(
        self,
        key: str,
        compute: Callable[[int], Any],
        generation: int,
        event: threading.Event,
    ) -> Any:
        """Runs compute() with NO lock held (may block on NFS I/O), then
        caches the result and releases the single-flight claim."""
        try:
            signal = compute(generation)
        except Exception:
            # Release the claim WITHOUT caching anything, so the next
            # caller for this key becomes a fresh computer rather than
            # being wedged behind a permanently-claimed key. The original
            # exception propagates unchanged -- never swallowed.
            with self._lock:
                self._computing.pop(key, None)
            event.set()
            raise

        with self._lock:
            if key not in self._entries and len(self._entries) >= self._max_entries:
                self._evict_oldest_locked()
            self._entries[key] = _FreshnessCacheEntry(
                signal=signal, generation=generation
            )
            self._computing.pop(key, None)
        event.set()
        return signal

    def get_or_compute(self, key: str, compute: Callable[[int], Any]) -> Any:
        """Return the cached signal for `key`, recomputing at most once per
        min_recheck_interval_seconds, with a per-key single-flight guard so
        concurrent callers for the SAME key never pile into concurrent
        blocking computations.

        Args:
            key: cache key (e.g. "{username}:{repository_alias}"). Must be
                a non-empty string.
            compute: called with ONE argument -- the generation id for
                this recompute pass (a strictly-increasing, process-wide
                counter -- Bug #1547 round-2 FIX 1 -- used by the caller to
                tag any DEGRADED sub-result it cannot verify; see the
                module docstring's Finding 2 rationale). Must return the
                signal to cache. Runs with NO lock held, so it may safely
                perform blocking I/O.

        Returns:
            The signal from either a cached entry (still within the
            recheck interval), a freshly computed one, or -- Bug #1547
            round-2 FIX 3 -- a degraded, generation-tagged fallback
            (_cold_wait_timeout_signal) when this call was waiting on a
            COLD key's in-flight compute() and that wait exceeded
            cold_key_wait_timeout_seconds without the other thread
            finishing.

        Raises:
            ValueError: key is empty.
            TypeError: key is not a str, or compute is not callable.
            RuntimeError: exceeded _MAX_CLAIM_ATTEMPTS claim/wait cycles
                (persistent compute() failures for this key).
        """
        _validate_key(key)
        _validate_compute(compute)

        for _ in range(_MAX_CLAIM_ATTEMPTS):
            cached, generation, event, cold_wait_generation = self._claim_locked(key)
            if generation is not None:
                assert event is not None  # narrowed by _claim_locked's contract
                return self._compute_and_store(key, compute, generation, event)
            if cached is not _NOT_CACHED:
                return cached
            assert event is not None  # narrowed by _claim_locked's contract
            if cold_wait_generation is not None:
                # Bug #1547 round-2 FIX 3: bounded wait, cold-key only.
                if not event.wait(timeout=self._cold_key_wait_timeout_seconds):
                    return _cold_wait_timeout_signal(cold_wait_generation)
                # else: the stuck computer finished within the bound --
                # fall through and retry the claim, which will now pick up
                # the freshly cached signal.
            else:
                # Pre-FIX-3, unchanged: an entry (possibly stale) already
                # exists for this key -- wait UNBOUNDED, exactly as before.
                event.wait()

        raise RuntimeError(
            f"TemporalFreshnessSignalCache: exceeded {_MAX_CLAIM_ATTEMPTS} "
            f"claim attempts for key {key!r} -- persistent compute() "
            "failures for this key?"
        )


_singleton: Optional[TemporalFreshnessSignalCache] = None
_singleton_lock = threading.Lock()


def get_temporal_freshness_signal_cache() -> TemporalFreshnessSignalCache:
    """Return the process-wide (per-node, in-RAM) TemporalFreshnessSignalCache
    singleton, constructing it on first access. Mirrors
    temporal_dedup_cache.get_temporal_dedup_cache() -- this is the default
    production dependency wired into execute_live_temporal_search's
    freshness_cache parameter (temporal_live_dispatch.py)."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = TemporalFreshnessSignalCache()
    return _singleton
