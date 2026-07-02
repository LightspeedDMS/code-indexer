"""
Tests for FaultInjectionService profile registry (CRUD operations).

Story #746 — Scenarios 6, 15 (partial: registry state only).

TDD: tests written BEFORE production code.
"""

import random

import pytest

from code_indexer.server.fault_injection.fault_profile import FaultProfile
from code_indexer.server.fault_injection.fault_injection_service import (
    FaultInjectionService,
)

# ---------------------------------------------------------------------------
# Constants — no vendor hostnames, no magic numbers or domain literals inline
# ---------------------------------------------------------------------------
SEED = 42
TARGET_A = "provider-a.test"
TARGET_B = "provider-b.test"
UNKNOWN_TARGET = "unknown.test"
DEFAULT_ERROR_CODES = (503,)  # immutable to prevent cross-test mutation leakage
DEFAULT_ERROR_RATE = 1.0
INITIAL_ERROR_RATE = 0.3
UPDATED_ERROR_RATE = 0.9


def _make_service(enabled: bool = True) -> FaultInjectionService:
    return FaultInjectionService(enabled=enabled, rng=random.Random(SEED))


def _profile(
    target: str = TARGET_A,
    error_rate: float = DEFAULT_ERROR_RATE,
    **kwargs,
) -> FaultProfile:
    return FaultProfile(
        target=target,
        error_rate=error_rate,
        error_codes=list(DEFAULT_ERROR_CODES),
        **kwargs,
    )


@pytest.fixture()
def svc() -> FaultInjectionService:
    return _make_service()


@pytest.fixture()
def svc_with_profile(svc) -> FaultInjectionService:
    svc.register_profile(TARGET_A, _profile())
    return svc


# ===========================================================================
# Registry CRUD
# ===========================================================================


def test_register_and_get_profile(svc):
    svc.register_profile(TARGET_A, _profile())
    got = svc.get_profile(TARGET_A)
    assert got is not None
    assert got.target == TARGET_A


def test_register_overwrites_existing(svc):
    svc.register_profile(TARGET_A, _profile(error_rate=INITIAL_ERROR_RATE))
    svc.register_profile(TARGET_A, _profile(error_rate=UPDATED_ERROR_RATE))
    assert svc.get_profile(TARGET_A).error_rate == UPDATED_ERROR_RATE


def test_remove_profile(svc_with_profile):
    svc_with_profile.remove_profile(TARGET_A)
    assert svc_with_profile.get_profile(TARGET_A) is None


def test_remove_nonexistent_profile_does_not_raise(svc):
    svc.remove_profile(UNKNOWN_TARGET)  # must not raise


def test_get_all_profiles_empty(svc):
    assert svc.get_all_profiles() == {}


def test_get_all_profiles_returns_both_registered(svc):
    svc.register_profile(TARGET_A, _profile(target=TARGET_A))
    svc.register_profile(TARGET_B, _profile(target=TARGET_B))
    result = svc.get_all_profiles()
    assert TARGET_A in result
    assert TARGET_B in result
