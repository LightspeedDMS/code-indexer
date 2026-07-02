"""
FaultInjectionService and related types for the fault injection harness.

Story #746 — Phase B foundations.

Provides:
  InjectionEvent       — immutable record of one injected fault
  select_outcome       — weighted mutually-exclusive terminating mode selection
  FaultInjectionService — lock-guarded registry with snapshot, ring buffer, counters
"""

from __future__ import annotations

import copy
import logging
import random
import threading
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from code_indexer.server.fault_injection.fault_profile import (
    FaultProfile,
    target_matches,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_HISTORY_CAPACITY = 100

_logger = logging.getLogger("fault_injection")


# ---------------------------------------------------------------------------
# InjectionEvent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InjectionEvent:
    """Immutable record of one fault injection event."""

    target: str
    fault_type: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Outcome selection
# ---------------------------------------------------------------------------

# Ordered list of (fault_type, rate_field) for terminating modes.
# The order defines priority when rates sum exactly to 1.0.
_TERMINATING_MODES: Tuple[Tuple[str, str], ...] = (
    ("http_error", "error_rate"),
    ("connect_timeout", "connect_timeout_rate"),
    ("read_timeout", "read_timeout_rate"),
    ("write_timeout", "write_timeout_rate"),
    ("pool_timeout", "pool_timeout_rate"),
    ("connect_error", "connect_error_rate"),
    ("dns_failure", "dns_failure_rate"),
    ("tls_error", "tls_error_rate"),
    ("malformed_json", "malformed_rate"),
    ("stream_disconnect", "stream_disconnect_rate"),
    ("redirect_loop", "redirect_loop_rate"),
)


def select_outcome(profile: FaultProfile, rng: random.Random) -> str:
    """
    Select a mutually-exclusive terminating outcome (or pass_through) for a
    request hitting *profile*, using *rng* for all random draws.

    Algorithm (Story #746 SELECT_OUTCOME spec):
      Build cumulative probability thresholds for each terminating mode in
      _TERMINATING_MODES order.  Draw one uniform random number u in [0, 1).
      Walk the thresholds; return the first mode whose cumulative probability
      exceeds u.  If no mode matched, return "pass_through".

    This guarantees:
      - rate=1.0 always selects that mode (u < 1.0 always)
      - rate=0.0 for all modes always returns "pass_through"
      - Seeded RNG produces deterministic sequences (Scenario 23)
    """
    cumulative = 0.0
    u = rng.random()
    for fault_type, rate_field in _TERMINATING_MODES:
        rate = getattr(profile, rate_field)
        if rate <= 0.0:
            continue
        cumulative += rate
        if u < cumulative:
            return fault_type
    return "pass_through"


# ---------------------------------------------------------------------------
# FaultInjectionService
# ---------------------------------------------------------------------------


class FaultInjectionService:
    """
    Central registry and state tracker for the fault injection harness.

    Thread-safety contract:
      - register_profile, remove_profile, get_profile, get_all_profiles,
        record_injection, get_counters, get_history, and reset all acquire
        _lock for their entire body.
      - match_profile_snapshot acquires _lock for the full iteration over
        _profiles and the deepcopy of the matched profile.
      - set_seed acquires _lock before reseeding _rng.
      - The rng property returns the internal random.Random instance without
        locking — it is intended for single-threaded deterministic use in
        test scenarios (Scenarios 14, 23) and must not be called from
        concurrent production code paths.
    """

    def __init__(
        self, enabled: bool = True, rng: Optional[random.Random] = None
    ) -> None:
        self._enabled = enabled
        self._rng: random.Random = rng if rng is not None else random.Random()
        self._lock = threading.Lock()
        self._profiles: Dict[str, FaultProfile] = {}
        self._counters: Dict[Tuple[str, str], int] = {}
        self._history: deque = deque(maxlen=_HISTORY_CAPACITY)

    # ------------------------------------------------------------------
    # Registry CRUD
    # ------------------------------------------------------------------

    def register_profile(self, target: str, profile: FaultProfile) -> None:
        with self._lock:
            self._profiles[target] = profile

    def get_profile(self, target: str) -> Optional[FaultProfile]:
        with self._lock:
            return self._profiles.get(target)

    def remove_profile(self, target: str) -> None:
        with self._lock:
            self._profiles.pop(target, None)

    def get_all_profiles(self) -> Dict[str, FaultProfile]:
        with self._lock:
            return dict(self._profiles)

    # ------------------------------------------------------------------
    # Snapshot matching (Scenario 15)
    # ------------------------------------------------------------------

    def match_profile_snapshot(self, url: str) -> Optional[FaultProfile]:
        """
        Return a deepcopy of the first enabled profile whose target matches
        the hostname extracted from *url*, or None if service is disabled,
        no profile matches, or the matching profile is disabled.

        The caller receives a snapshot; subsequent mutations to the registry
        do not affect the returned copy.

        Returns None (does not raise) when *url* is malformed and urlparse
        raises ValueError.
        """
        if not self._enabled:
            return None

        try:
            hostname = urlparse(url).hostname or ""
        except ValueError:
            return None

        with self._lock:
            for target, profile in self._profiles.items():
                if not profile.enabled:
                    continue
                if target_matches(target, hostname):
                    return copy.deepcopy(profile)
        return None

    # ------------------------------------------------------------------
    # Event recording
    # ------------------------------------------------------------------

    def record_injection(
        self, target: str, fault_type: str, correlation_id: str
    ) -> None:
        """Increment counter and append to ring buffer (both under lock)."""
        event = InjectionEvent(
            target=target,
            fault_type=fault_type,
            correlation_id=correlation_id,
        )
        with self._lock:
            key = (target, fault_type)
            self._counters[key] = self._counters.get(key, 0) + 1
            self._history.append(event)
        _logger.info(
            "fault_injection: target=%s fault_type=%s correlation_id=%s",
            target,
            fault_type,
            correlation_id,
            extra={
                "target": target,
                "fault_type": fault_type,
                "correlation_id": correlation_id,
            },
        )

    def get_counters(self) -> Dict[Tuple[str, str], int]:
        with self._lock:
            return dict(self._counters)

    def get_history(self) -> List[InjectionEvent]:
        with self._lock:
            return list(self._history)

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear all profiles, counters, and history (Scenario 16)."""
        with self._lock:
            self._profiles.clear()
            self._counters.clear()
            self._history.clear()

    # ------------------------------------------------------------------
    # Seed control and RNG access (Scenarios 14, 23)
    # ------------------------------------------------------------------

    def set_seed(self, seed: int) -> None:
        """Re-seed the internal RNG for reproducible sequences (Scenario 14)."""
        with self._lock:
            self._rng.seed(seed)

    def draw_per_request_seed(self) -> int:
        """
        Atomically draw a 64-bit seed from the shared RNG under lock.

        The caller uses this seed to construct an isolated random.Random instance
        for one request, so that concurrent set_seed() calls cannot corrupt
        in-flight outcome selection (M4 per-request RNG isolation).
        """
        with self._lock:
            return self._rng.getrandbits(64)

    @property
    def enabled(self) -> bool:
        """Return whether the fault injection service is active."""
        return self._enabled

    @property
    def rng(self) -> random.Random:
        """
        Return the internal RNG instance.

        Intended for single-threaded deterministic test use only (Scenarios 14, 23).
        Do not call from concurrent production code paths.
        """
        return self._rng
