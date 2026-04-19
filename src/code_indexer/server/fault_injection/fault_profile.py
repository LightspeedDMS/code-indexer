"""
FaultProfile dataclass and related helpers for the fault injection harness.

Story #746 — Phase A foundations.

Provides:
  FaultProfile   — validated configuration for one injection target
  target_matches — exact or *.suffix hostname matching (no substring)
  roll_bernoulli — Bernoulli trial with injected RNG (Scenario 23)
  jitter_uniform — uniform random int draw with injected RNG (Scenario 26)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import List, Tuple

# ---------------------------------------------------------------------------
# Terminating-mode rate field names — used to compute the sum constraint.
# latency_rate and slow_tail_rate are ADDITIVE and excluded from this list.
# ---------------------------------------------------------------------------
_TERMINATING_RATE_FIELDS: Tuple[str, ...] = (
    "error_rate",
    "connect_timeout_rate",
    "read_timeout_rate",
    "write_timeout_rate",
    "pool_timeout_rate",
    "connect_error_rate",
    "dns_failure_rate",
    "tls_error_rate",
    "malformed_rate",
    "stream_disconnect_rate",
    "redirect_loop_rate",
)

# All rate fields (terminating + additive) — validated to be in [0.0, 1.0].
_ALL_RATE_FIELDS: Tuple[str, ...] = _TERMINATING_RATE_FIELDS + (
    "latency_rate",
    "slow_tail_rate",
)

# Allowed corruption modes for malformed_json injection.  Any mode outside this
# set is rejected at profile construction so unknown modes never reach the
# transport layer.
_ALLOWED_CORRUPTION_MODES: frozenset = frozenset(
    {"truncate", "invalid_utf8", "wrong_schema", "empty"}
)


@dataclass
class FaultProfile:
    """
    Configuration for fault injection against a single target hostname.

    All rate fields must be in [0.0, 1.0].
    The sum of all terminating-mode rates must not exceed 1.0.
    error_codes must be non-empty when error_rate > 0.
    corruption_modes must be non-empty when malformed_rate > 0.
    All corruption_modes values must be in: truncate, invalid_utf8, wrong_schema, empty.
    Range tuples must have non-negative values and min <= max.
    """

    target: str
    enabled: bool = True

    # Terminating fault modes
    error_rate: float = 0.0
    error_codes: List[int] = field(default_factory=list)
    retry_after_sec_range: Tuple[int, int] = (1, 5)

    connect_timeout_rate: float = 0.0
    read_timeout_rate: float = 0.0
    write_timeout_rate: float = 0.0
    pool_timeout_rate: float = 0.0
    connect_error_rate: float = 0.0
    dns_failure_rate: float = 0.0
    tls_error_rate: float = 0.0

    malformed_rate: float = 0.0
    corruption_modes: List[str] = field(default_factory=list)

    stream_disconnect_rate: float = 0.0
    truncate_after_bytes_range: Tuple[int, int] = (50, 200)

    redirect_loop_rate: float = 0.0

    # Additive (non-terminating) latency modes
    latency_rate: float = 0.0
    latency_ms_range: Tuple[int, int] = (100, 500)

    slow_tail_rate: float = 0.0
    slow_tail_ms_range: Tuple[int, int] = (1000, 5000)

    def __post_init__(self) -> None:
        self._validate_rate_bounds()
        self._validate_terminating_sum()
        self._validate_dependent_fields()
        self._validate_ranges()

    def _validate_rate_bounds(self) -> None:
        for field_name in _ALL_RATE_FIELDS:
            value = getattr(self, field_name)
            if not (0.0 <= value <= 1.0):
                raise ValueError(f"{field_name} must be in [0.0, 1.0], got {value}")

    def _validate_terminating_sum(self) -> None:
        total = sum(getattr(self, f) for f in _TERMINATING_RATE_FIELDS)
        if total > 1.0 + 1e-9:  # small epsilon for floating-point comparison
            raise ValueError(
                f"sum of terminating rates must not exceed 1.0, got {total:.4f}"
            )

    def _validate_dependent_fields(self) -> None:
        if self.error_rate > 0.0 and not self.error_codes:
            raise ValueError("error_codes must be a non-empty list when error_rate > 0")
        if self.malformed_rate > 0.0 and not self.corruption_modes:
            raise ValueError(
                "corruption_modes must be a non-empty list when malformed_rate > 0"
            )
        unknown = [
            m for m in self.corruption_modes if m not in _ALLOWED_CORRUPTION_MODES
        ]
        if unknown:
            raise ValueError(
                f"corruption_modes contains unknown modes: {unknown!r}. "
                f"Allowed: {sorted(_ALLOWED_CORRUPTION_MODES)}"
            )

    def _validate_ranges(self) -> None:
        _check_range("latency_ms_range", self.latency_ms_range)
        _check_range("slow_tail_ms_range", self.slow_tail_ms_range)
        _check_range("retry_after_sec_range", self.retry_after_sec_range)
        _check_range("truncate_after_bytes_range", self.truncate_after_bytes_range)


def _check_range(field_name: str, range_tuple: Tuple[int, int]) -> None:
    lo, hi = range_tuple
    if lo < 0:
        raise ValueError(f"{field_name} minimum must be non-negative, got {lo}")
    if lo > hi:
        raise ValueError(f"{field_name} minimum must be <= maximum, got ({lo}, {hi})")


# ---------------------------------------------------------------------------
# Hostname matching helper
# ---------------------------------------------------------------------------


def target_matches(configured_target: str, hostname: str) -> bool:
    """
    Return True if hostname matches configured_target.

    Rules (Story #746 Scenarios 12 and 27, spec algorithm):
      - Both must be non-empty.
      - If configured_target starts with "*.", treat it as a suffix pattern:
          hostname matches if:
            hostname.lower() == suffix.lower()          (apex root match)
            OR hostname.lower() ends with "." + suffix.lower()  (subdomain match)
          The apex match is intentional per the story spec algorithm.
      - Otherwise: exact case-insensitive equality only.
      - Substring matching is NEVER performed (e.g. "api" never matches "malapi.corp").
    """
    if not configured_target or not hostname:
        return False

    ct_lower = configured_target.lower()
    h_lower = hostname.lower()

    if ct_lower.startswith("*."):
        suffix = ct_lower[2:]
        # Apex root match OR subdomain match — per story spec algorithm
        return h_lower == suffix or h_lower.endswith("." + suffix)

    return ct_lower == h_lower


# ---------------------------------------------------------------------------
# Probabilistic helpers — both require an explicit random.Random instance
# ---------------------------------------------------------------------------


def roll_bernoulli(rate: float, rng: random.Random) -> bool:
    """
    Return True with probability `rate` using the provided RNG.

    Boundary behaviour:
      rate <= 0.0  -> always False
      rate >= 1.0  -> always True
    """
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    return rng.random() < rate


def jitter_uniform(min_ms: int, max_ms: int, rng: random.Random) -> int:
    """
    Return a uniform random integer in [min_ms, max_ms].

    When min_ms == max_ms, returns min_ms without calling the RNG.
    """
    if min_ms == max_ms:
        return min_ms
    return rng.randint(min_ms, max_ms)
