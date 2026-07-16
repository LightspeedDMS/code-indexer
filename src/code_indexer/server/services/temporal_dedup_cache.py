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
"""

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

DEFAULT_MAX_ENTRIES = 4096
DEFAULT_TERMINAL_TTL_SECONDS = 3600.0

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


@dataclass
class _DedupEntry:
    job_id: str
    terminal_observed_at: Optional[float] = None


class TemporalDedupCache:
    """Bounded, single-mutex, same-node signature -> job_id dedup index."""

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        terminal_ttl_seconds: float = DEFAULT_TERMINAL_TTL_SECONDS,
    ) -> None:
        if max_entries <= 0:
            raise ValueError(f"max_entries must be > 0, got {max_entries}")
        if terminal_ttl_seconds < 0:
            raise ValueError(
                f"terminal_ttl_seconds must be >= 0, got {terminal_ttl_seconds}"
            )
        self._lock = threading.Lock()
        self._entries: Dict[str, _DedupEntry] = {}
        self._max_entries = max_entries
        self._terminal_ttl_seconds = terminal_ttl_seconds

    def _evict_expired_terminal_entries_locked(self) -> None:
        """Must be called with self._lock held. Removes terminal entries
        whose TTL has elapsed -- NEVER touches active entries."""
        now = time.monotonic()
        expired = [
            sig
            for sig, entry in self._entries.items()
            if entry.terminal_observed_at is not None
            and (now - entry.terminal_observed_at) >= self._terminal_ttl_seconds
        ]
        for sig in expired:
            del self._entries[sig]

    def get_or_submit(
        self,
        signature: str,
        status_check: Callable[[str], Optional[str]],
        submit: Callable[[], str],
    ) -> str:
        """Return the job_id for `signature`, joining an existing
        active-or-within-TTL-terminal entry, or submitting a new job.

        Args:
            signature: canonical_signature() output for this request.
            status_check: given a job_id, returns its current status
                string, or None if not found/unauthorized.
            submit: zero-arg callable that submits a new job and returns
                its job_id. Called at most once per invocation, only when
                no joinable entry exists.

        Raises:
            TemporalDedupCapacityExhaustedError: cache is full of active
                entries and this is a genuinely new signature.
        """
        with self._lock:
            entry = self._entries.get(signature)
            if entry is not None:
                status = status_check(entry.job_id)
                if status not in _TERMINAL_STATUSES:
                    return entry.job_id
                # Terminal (or absent). Within the terminal TTL window, a
                # fast-follow identical query still joins the just-finished
                # result -- avoid recomputation.
                if entry.terminal_observed_at is None:
                    entry.terminal_observed_at = time.monotonic()
                elapsed = time.monotonic() - entry.terminal_observed_at
                if status is not None and elapsed < self._terminal_ttl_seconds:
                    return entry.job_id
                # Absent, or terminal past its TTL -> replace with a fresh
                # submission (falls through to the insert-new-entry path).
                del self._entries[signature]

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
    either door via the SAME node joins the same in-flight job."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = TemporalDedupCache()
    return _singleton
