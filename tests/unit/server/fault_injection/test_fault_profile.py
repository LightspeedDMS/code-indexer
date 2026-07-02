"""
Tests for FaultProfile dataclass + validation, target_matches helper,
roll_bernoulli helper, and jitter_uniform helper.

Story #746 — Scenarios 6, 7, 12, 25, 26, 27.

Parametrize strategy:
- Repeated identical-structure cases use pytest.mark.parametrize with ids.
- Unique single-scenario tests (valid construction, zero-rate, enabled flag,
  sum constraints, probabilistic/determinism) remain standalone because each
  covers a distinct behavior with no matching partner case.

TDD: these tests are written BEFORE the production code.
"""

import random

import pytest

from code_indexer.server.fault_injection.fault_profile import (
    FaultProfile,
    target_matches,
    roll_bernoulli,
    jitter_uniform,
)

# ---------------------------------------------------------------------------
# Constants for genuinely ambiguous values only.
# Mathematically obvious literals (0.0, 0.5, 1.0) and well-known HTTP
# status codes (429, 500) remain inline for readability.
# ---------------------------------------------------------------------------
SEED_MAIN = 42
SEED_ALT = 99
SEED_PROB = 12345

SAMPLE_LARGE = 10_000
SAMPLE_MEDIUM = 200
SAMPLE_SMALL = 100
SEQUENCE_LEN = 50
REPEAT_FIXED = 10

JITTER_LO = 500
JITTER_HI = 1500
JITTER_MID = 1000
JITTER_FIXED = 750
JITTER_TOLERANCE_FRACTION = 0.05

INT_RANGE_LO = 100
INT_RANGE_HI = 200

LATENCY_RANGE_REVERSED = (1000, 500)
LATENCY_RANGE_NEGATIVE_MIN = (-100, 500)
TRUNCATE_RANGE_NEGATIVE = (-1, 100)
TRUNCATE_RANGE_REVERSED = (200, 100)

TARGET_VOYAGE = "api.voyageai.com"


# ===========================================================================
# target_matches — parametrized (Scenarios 12, 27)
# ===========================================================================

_EXACT_CASES = [
    (TARGET_VOYAGE, TARGET_VOYAGE, True, "exact-positive"),
    ("Api.VoyageAI.com", TARGET_VOYAGE, True, "exact-case-insensitive"),
    (TARGET_VOYAGE, "api.cohere.com", False, "exact-different-host"),
    ("api", "malapi.corp", False, "exact-no-substring-api-in-malapi"),
    ("voyageai.com", "apivoyageai.com", False, "exact-no-suffix-prefix-confusion"),
    ("", TARGET_VOYAGE, False, "exact-empty-configured"),
    (TARGET_VOYAGE, "", False, "exact-empty-hostname"),
    ("", "", False, "exact-both-empty"),
]

_WILDCARD_CASES = [
    ("*.voyageai.com", TARGET_VOYAGE, True, "wildcard-direct-child"),
    ("*.voyageai.com", "www.voyageai.com", True, "wildcard-www"),
    ("*.voyageai.com", "voyageai.com", True, "wildcard-apex-itself"),
    ("*.voyageai.com", "malvoyageai.com", False, "wildcard-no-substring"),
    ("*.voyageai.com", "apivoyageai.com", False, "wildcard-no-apex-prefix-confusion"),
    (
        "*.voyageai.com",
        "evilvoyageai.com.attacker.net",
        False,
        "wildcard-attacker-suffix",
    ),
    (
        "*.voyageai.com",
        "api.voyageai.com.evil.net",
        False,
        "wildcard-attacker-subdomain",
    ),
    ("*.VoyageAI.com", "API.voyageai.com", True, "wildcard-case-insensitive"),
]


@pytest.mark.parametrize(
    "configured,hostname,expected",
    [(c, h, e) for c, h, e, _ in _EXACT_CASES],
    ids=[t for _, _, _, t in _EXACT_CASES],
)
def test_target_matches_exact(configured, hostname, expected):
    assert target_matches(configured, hostname) is expected


@pytest.mark.parametrize(
    "configured,hostname,expected",
    [(c, h, e) for c, h, e, _ in _WILDCARD_CASES],
    ids=[t for _, _, _, t in _WILDCARD_CASES],
)
def test_target_matches_wildcard(configured, hostname, expected):
    assert target_matches(configured, hostname) is expected


# ===========================================================================
# FaultProfile rate-bounds — parametrized (all 13 fields, above-1 + below-0)
# ===========================================================================

_RATE_BOUNDS_CASES = [
    ("error_rate", 1.5, "error_rate-above-1"),
    ("error_rate", -0.1, "error_rate-below-0"),
    ("latency_rate", 1.1, "latency_rate-above-1"),
    ("latency_rate", -0.1, "latency_rate-below-0"),
    ("slow_tail_rate", 1.5, "slow_tail_rate-above-1"),
    ("slow_tail_rate", -0.2, "slow_tail_rate-below-0"),
    ("connect_timeout_rate", 2.0, "connect_timeout_rate-above-1"),
    ("connect_timeout_rate", -0.5, "connect_timeout_rate-below-0"),
    ("read_timeout_rate", 1.1, "read_timeout_rate-above-1"),
    ("read_timeout_rate", -0.1, "read_timeout_rate-below-0"),
    ("write_timeout_rate", 1.1, "write_timeout_rate-above-1"),
    ("write_timeout_rate", -0.1, "write_timeout_rate-below-0"),
    ("pool_timeout_rate", 1.1, "pool_timeout_rate-above-1"),
    ("pool_timeout_rate", -0.1, "pool_timeout_rate-below-0"),
    ("connect_error_rate", 1.1, "connect_error_rate-above-1"),
    ("connect_error_rate", -0.1, "connect_error_rate-below-0"),
    ("dns_failure_rate", 1.1, "dns_failure_rate-above-1"),
    ("dns_failure_rate", -0.1, "dns_failure_rate-below-0"),
    ("tls_error_rate", 1.1, "tls_error_rate-above-1"),
    ("tls_error_rate", -0.1, "tls_error_rate-below-0"),
    ("malformed_rate", 1.1, "malformed_rate-above-1"),
    ("malformed_rate", -0.1, "malformed_rate-below-0"),
    ("stream_disconnect_rate", 1.1, "stream_disconnect_rate-above-1"),
    ("stream_disconnect_rate", -0.1, "stream_disconnect_rate-below-0"),
    ("redirect_loop_rate", 1.1, "redirect_loop_rate-above-1"),
    ("redirect_loop_rate", -0.1, "redirect_loop_rate-below-0"),
]


@pytest.mark.parametrize(
    "field_name,invalid_value",
    [(f, v) for f, v, _ in _RATE_BOUNDS_CASES],
    ids=[t for _, _, t in _RATE_BOUNDS_CASES],
)
def test_rate_field_out_of_bounds_raises(field_name, invalid_value):
    with pytest.raises(ValueError, match=field_name):
        FaultProfile(target=TARGET_VOYAGE, **{field_name: invalid_value})


# ===========================================================================
# FaultProfile constraint tests — standalone (each covers a distinct behaviour)
# ===========================================================================


def test_valid_profile_creates_successfully():
    """Smoke test: a correctly configured profile is accepted."""
    profile = FaultProfile(target=TARGET_VOYAGE, error_rate=0.5, error_codes=[429])
    assert profile.target == TARGET_VOYAGE
    assert profile.error_rate == 0.5


def test_all_rates_zero_is_valid():
    """Zero-rate profile is a valid pass-through configuration."""
    assert FaultProfile(target=TARGET_VOYAGE).error_rate == 0.0


def test_enabled_defaults_to_true():
    assert FaultProfile(target=TARGET_VOYAGE).enabled is True


def test_enabled_can_be_set_false():
    assert FaultProfile(target=TARGET_VOYAGE, enabled=False).enabled is False


def test_sum_exactly_1_is_valid():
    """Sum of terminating rates at exactly 1.0 must be accepted."""
    profile = FaultProfile(
        target=TARGET_VOYAGE,
        error_rate=0.5,
        connect_error_rate=0.5,
        error_codes=[429],
    )
    assert profile.error_rate + profile.connect_error_rate == 1.0


def test_sum_above_1_raises():
    """Sum of terminating rates exceeding 1.0 must be rejected (Scenario 7)."""
    with pytest.raises(ValueError, match="sum of terminating"):
        FaultProfile(
            target=TARGET_VOYAGE,
            connect_error_rate=0.6,
            error_rate=0.5,
            error_codes=[500],
        )


def test_latency_and_slow_tail_not_counted_in_terminating_sum():
    """Additive modes (latency, slow_tail) do not contribute to sum constraint."""
    profile = FaultProfile(
        target=TARGET_VOYAGE,
        error_rate=0.9,
        latency_rate=1.0,
        slow_tail_rate=1.0,
        error_codes=[429],
    )
    assert profile.error_rate == 0.9


# Dependent-field validation — parametrized (Scenario 6)
_DEPENDENT_FIELD_CASES = [
    ({"error_rate": 0.5}, "error_codes", "error-rate-no-codes"),
    ({"error_rate": 0.5, "error_codes": []}, "error_codes", "error-rate-empty-codes"),
    ({"malformed_rate": 0.3}, "corruption_modes", "malformed-rate-no-modes"),
    (
        {"malformed_rate": 0.3, "corruption_modes": []},
        "corruption_modes",
        "malformed-rate-empty-modes",
    ),
]


@pytest.mark.parametrize(
    "kwargs,match_text",
    [(k, m) for k, m, _ in _DEPENDENT_FIELD_CASES],
    ids=[t for _, _, t in _DEPENDENT_FIELD_CASES],
)
def test_dependent_field_validation_raises(kwargs, match_text):
    with pytest.raises(ValueError, match=match_text):
        FaultProfile(target=TARGET_VOYAGE, **kwargs)


def test_error_rate_zero_without_error_codes_ok():
    """error_rate=0 does not require error_codes."""
    assert FaultProfile(target=TARGET_VOYAGE, error_rate=0.0).error_codes == []


def test_malformed_rate_zero_without_corruption_modes_ok():
    """malformed_rate=0 does not require corruption_modes."""
    assert FaultProfile(target=TARGET_VOYAGE, malformed_rate=0.0).corruption_modes == []


# ===========================================================================
# M3: corruption_modes unknown-mode validation (Story #746 review finding M3)
# ===========================================================================

_VALID_CORRUPTION_MODES = ["truncate", "invalid_utf8", "wrong_schema", "empty"]

_UNKNOWN_CORRUPTION_MODE_CASES = [
    ("banana", "unknown-word"),
    ("TRUNCATE", "uppercase-known"),
    ("", "empty-string"),
    ("truncate ", "trailing-space"),
]


def test_valid_corruption_modes_accepted():
    """All four known corruption modes must be accepted by FaultProfile."""
    profile = FaultProfile(
        target=TARGET_VOYAGE,
        malformed_rate=0.5,
        corruption_modes=_VALID_CORRUPTION_MODES,
    )
    assert set(profile.corruption_modes) == set(_VALID_CORRUPTION_MODES)


@pytest.mark.parametrize(
    "unknown_mode",
    [m for m, _ in _UNKNOWN_CORRUPTION_MODE_CASES],
    ids=[t for _, t in _UNKNOWN_CORRUPTION_MODE_CASES],
)
def test_unknown_corruption_mode_raises_at_profile_construction(unknown_mode: str):
    """M3: FaultProfile must reject unknown corruption modes at construction time."""
    with pytest.raises(ValueError, match="corruption_modes"):
        FaultProfile(
            target=TARGET_VOYAGE,
            malformed_rate=0.5,
            corruption_modes=[unknown_mode],
        )


# Range validation — parametrized
_RANGE_CASES = [
    (
        {"latency_rate": 0.5, "latency_ms_range": LATENCY_RANGE_REVERSED},
        "latency_ms_range",
        "latency-min-gt-max",
    ),
    (
        {"latency_rate": 0.5, "latency_ms_range": LATENCY_RANGE_NEGATIVE_MIN},
        "latency_ms_range",
        "latency-negative-min",
    ),
    (
        {
            "stream_disconnect_rate": 0.5,
            "truncate_after_bytes_range": TRUNCATE_RANGE_NEGATIVE,
        },
        "truncate_after_bytes_range",
        "truncate-negative",
    ),
    (
        {
            "stream_disconnect_rate": 0.5,
            "truncate_after_bytes_range": TRUNCATE_RANGE_REVERSED,
        },
        "truncate_after_bytes_range",
        "truncate-min-gt-max",
    ),
]


@pytest.mark.parametrize(
    "kwargs,match_text",
    [(k, m) for k, m, _ in _RANGE_CASES],
    ids=[t for _, _, t in _RANGE_CASES],
)
def test_range_field_validation_raises(kwargs, match_text):
    with pytest.raises(ValueError, match=match_text):
        FaultProfile(target=TARGET_VOYAGE, **kwargs)


# ===========================================================================
# roll_bernoulli  (Scenario 25)
# ===========================================================================

_BERNOULLI_BOUNDARY_CASES = [
    (0.0, False, "rate-zero-always-false"),
    (1.0, True, "rate-one-always-true"),
]


@pytest.mark.parametrize(
    "rate,expected",
    [(r, e) for r, e, _ in _BERNOULLI_BOUNDARY_CASES],
    ids=[t for _, _, t in _BERNOULLI_BOUNDARY_CASES],
)
def test_bernoulli_boundary(rate, expected):
    """Boundary rates 0.0 and 1.0 must always return False/True respectively."""
    rng = random.Random(SEED_MAIN)
    for _ in range(SAMPLE_SMALL):
        assert roll_bernoulli(rate, rng) is expected


def test_bernoulli_probabilistic_produces_both_outcomes():
    """Rate=0.5 must produce both True and False across a reasonable sample."""
    rng = random.Random(SEED_PROB)
    results = [roll_bernoulli(0.5, rng) for _ in range(SAMPLE_MEDIUM)]
    assert any(results) and not all(results)


def test_bernoulli_seeded_rng_is_deterministic():
    """Same seed must produce identical outcome sequence (Scenario 23)."""
    rng_a = random.Random(SEED_ALT)
    rng_b = random.Random(SEED_ALT)
    seq_a = [roll_bernoulli(0.5, rng_a) for _ in range(SEQUENCE_LEN)]
    seq_b = [roll_bernoulli(0.5, rng_b) for _ in range(SEQUENCE_LEN)]
    assert seq_a == seq_b


# ===========================================================================
# jitter_uniform  (Scenario 26) — all cases parametrized
# ===========================================================================


def _check_all_in_range(values):
    for v in values:
        assert JITTER_LO <= v <= JITTER_HI


def _check_mean_near_midpoint(values):
    mean = sum(values) / len(values)
    assert abs(mean - JITTER_MID) / JITTER_MID <= JITTER_TOLERANCE_FRACTION


def _check_constant(values):
    assert all(v == JITTER_FIXED for v in values)


def _check_int_type(values):
    assert all(isinstance(v, int) for v in values)


_JITTER_CASES = [
    (JITTER_LO, JITTER_HI, SAMPLE_LARGE, _check_all_in_range, "all-values-in-range"),
    (
        JITTER_LO,
        JITTER_HI,
        SAMPLE_LARGE,
        _check_mean_near_midpoint,
        "mean-near-midpoint",
    ),
    (
        JITTER_FIXED,
        JITTER_FIXED,
        REPEAT_FIXED,
        _check_constant,
        "min-equals-max-constant",
    ),
    (INT_RANGE_LO, INT_RANGE_HI, REPEAT_FIXED, _check_int_type, "returns-int-type"),
]


@pytest.mark.parametrize(
    "lo,hi,n,check_fn",
    [(lo, hi, n, fn) for lo, hi, n, fn, _ in _JITTER_CASES],
    ids=[t for _, _, _, _, t in _JITTER_CASES],
)
def test_jitter_uniform(lo, hi, n, check_fn):
    rng = random.Random(SEED_MAIN)
    values = [jitter_uniform(lo, hi, rng) for _ in range(n)]
    check_fn(values)
