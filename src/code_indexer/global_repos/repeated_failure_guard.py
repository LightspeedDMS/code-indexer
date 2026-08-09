"""In-process guard against retrying a structurally unresolvable failure forever.

Bug #1539: every scheduled cidx-meta refresh cycle re-attempted a git rebase
that failed with a conflict-resolution error -- a FRESH attempt each time,
never a stuck job, so nothing ever timed out or alerted.  Because the retry
loop had no memory of previous attempts, an underlying condition that
retrying can never fix (e.g. genuinely corrupt content the LLM-based
resolver can't repair) held /health degraded indefinitely with zero
forward progress and no actionable error surfaced anywhere.

This module tracks a normalized "failure shape" fingerprint per tracking
key (typically a filesystem path) across consecutive calls, entirely in
process memory.  This is deliberately NOT persisted or made cross-node:
description_refresh_scheduler.py's PROMPT_FAILURE_QUARANTINE_THRESHOLD
counters (Bug #984) establish the same precedent for this exact class of
problem -- a single owning process retrying a recurring background job --
and its own comment explicitly documents that cross-worker quarantine-
counter consistency is intentionally out of scope for that mechanism.
Here, worst case, a fresh process (restart, failover to another node)
simply gets `threshold` more attempts before tripping again; it never
masks a fixed condition as broken forever, since a fingerprint change
(i.e. the underlying condition actually changed) always resets the count.

The sole caller today is
``code_indexer.server.services.cidx_meta_backup.sync.CidxMetaBackupSync``,
which records each conflict-resolution failure here and escalates to
``StructurallyUnresolvableConflictError`` once the identical failure shape
has recurred ``threshold`` times in a row.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict

DEFAULT_REPEATED_FAILURE_THRESHOLD = 3


@dataclass
class _TrackerEntry:
    fingerprint: str
    count: int


class RepeatedFailureGuard:
    """Tracks consecutive identical-shape failures per key, in-process."""

    def __init__(self, threshold: int = DEFAULT_REPEATED_FAILURE_THRESHOLD) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        self._threshold = threshold
        self._lock = threading.Lock()
        self._entries: Dict[str, _TrackerEntry] = {}

    def record_failure(self, key: str, fingerprint: str) -> int:
        """Record a failure for ``key`` with the given ``fingerprint``.

        Returns the new consecutive-occurrence count for this exact
        fingerprint.  A change in fingerprint resets the count to 1 -- a
        genuinely different failure shape is not "the same" stuck
        condition and must not inherit a prior unrelated tally.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.fingerprint == fingerprint:
                entry.count += 1
            else:
                entry = _TrackerEntry(fingerprint=fingerprint, count=1)
                self._entries[key] = entry
            return entry.count

    def is_exhausted(self, count: int) -> bool:
        return count >= self._threshold

    def reset(self, key: str) -> None:
        """Clear tracked failures for ``key`` -- call this on genuine success."""
        with self._lock:
            self._entries.pop(key, None)
