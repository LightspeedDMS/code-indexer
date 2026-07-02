"""
Tests for select_outcome() weighted random selection.

Story #746 — Scenarios 22, 23, 24.

TDD: tests written BEFORE production code.
"""

import random

from code_indexer.server.fault_injection.fault_profile import FaultProfile
from code_indexer.server.fault_injection.fault_injection_service import select_outcome

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SEED_MAIN = 42
SEED_ALT = 99
TARGET = "provider-a.test"
BOUNDARY_DRAWS = 50
TIMEOUT_DRAWS = 20
DETERMINISM_DRAWS = 40
DISTRIBUTION_DRAWS = 200
DEFAULT_ERROR_CODES = (429,)
HALF_RATE = 0.5


def _full_error_profile() -> FaultProfile:
    return FaultProfile(
        target=TARGET,
        error_rate=1.0,
        error_codes=list(DEFAULT_ERROR_CODES),
    )


def _passthrough_profile() -> FaultProfile:
    return FaultProfile(target=TARGET)


def _timeout_profile() -> FaultProfile:
    return FaultProfile(target=TARGET, connect_timeout_rate=1.0)


def _split_profile() -> FaultProfile:
    """50% http_error + 50% connect_error — no pass-through possible."""
    return FaultProfile(
        target=TARGET,
        error_rate=HALF_RATE,
        connect_error_rate=HALF_RATE,
        error_codes=list(DEFAULT_ERROR_CODES),
    )


def _half_error_profile() -> FaultProfile:
    return FaultProfile(
        target=TARGET,
        error_rate=HALF_RATE,
        error_codes=list(DEFAULT_ERROR_CODES),
    )


# ===========================================================================
# Boundary cases
# ===========================================================================


def test_full_error_rate_always_http_error():
    rng = random.Random(SEED_MAIN)
    profile = _full_error_profile()
    for _ in range(BOUNDARY_DRAWS):
        assert select_outcome(profile, rng) == "http_error"


def test_all_rates_zero_always_pass_through():
    rng = random.Random(SEED_MAIN)
    profile = _passthrough_profile()
    for _ in range(BOUNDARY_DRAWS):
        assert select_outcome(profile, rng) == "pass_through"


def test_connect_timeout_full_rate_always_connect_timeout():
    rng = random.Random(SEED_MAIN)
    profile = _timeout_profile()
    for _ in range(TIMEOUT_DRAWS):
        assert select_outcome(profile, rng) == "connect_timeout"


# ===========================================================================
# Determinism (Scenarios 22, 23)
# ===========================================================================


def test_seeded_outcome_sequence_is_deterministic():
    profile = _half_error_profile()
    rng_a = random.Random(SEED_ALT)
    rng_b = random.Random(SEED_ALT)
    seq_a = [select_outcome(profile, rng_a) for _ in range(DETERMINISM_DRAWS)]
    seq_b = [select_outcome(profile, rng_b) for _ in range(DETERMINISM_DRAWS)]
    assert seq_a == seq_b


# ===========================================================================
# Distribution (Scenario 22)
# ===========================================================================


def test_split_profile_produces_both_modes():
    """50/50 split must produce both http_error and connect_error in distribution."""
    profile = _split_profile()
    rng = random.Random(SEED_MAIN)
    outcomes = [select_outcome(profile, rng) for _ in range(DISTRIBUTION_DRAWS)]
    assert "http_error" in outcomes
    assert "connect_error" in outcomes
    assert "pass_through" not in outcomes
