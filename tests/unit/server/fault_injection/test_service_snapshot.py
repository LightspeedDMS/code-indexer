"""
Tests for FaultInjectionService.match_profile_snapshot().

Story #746 — Scenarios 15, 23.

TDD: tests written BEFORE production code.
"""

import random

import pytest

from code_indexer.server.fault_injection.fault_profile import FaultProfile
from code_indexer.server.fault_injection.fault_injection_service import (
    FaultInjectionService,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SEED = 42
TARGET_A = "provider-a.test"
TARGET_B = "provider-b.test"
URL_A = f"https://{TARGET_A}/v1/embed"
URL_B = f"https://{TARGET_B}/v1/embed"
DEFAULT_ERROR_CODES = (503,)
DEFAULT_ERROR_RATE = 1.0
SNAPSHOT_ERROR_RATE = 1.0
SNAPSHOT_ERROR_CODE = 429


def _make_service(enabled: bool = True) -> FaultInjectionService:
    return FaultInjectionService(enabled=enabled, rng=random.Random(SEED))


def _profile(
    target: str = TARGET_A,
    error_rate: float = DEFAULT_ERROR_RATE,
    enabled: bool = True,
    **kwargs,
) -> FaultProfile:
    return FaultProfile(
        target=target,
        error_rate=error_rate,
        error_codes=list(DEFAULT_ERROR_CODES),
        enabled=enabled,
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
# match_profile_snapshot
# ===========================================================================


def test_snapshot_returned_when_enabled_and_match(svc_with_profile):
    snap = svc_with_profile.match_profile_snapshot(URL_A)
    assert snap is not None
    assert snap.target == TARGET_A


def test_snapshot_is_deepcopy_not_same_object(svc_with_profile):
    snap = svc_with_profile.match_profile_snapshot(URL_A)
    live = svc_with_profile.get_profile(TARGET_A)
    assert snap is not live


def test_no_snapshot_when_service_disabled(svc):
    disabled_svc = _make_service(enabled=False)
    disabled_svc.register_profile(TARGET_A, _profile())
    assert disabled_svc.match_profile_snapshot(URL_A) is None


def test_no_snapshot_when_no_matching_profile(svc):
    svc.register_profile(TARGET_B, _profile(target=TARGET_B))
    assert svc.match_profile_snapshot(URL_A) is None


def test_no_snapshot_when_profile_disabled(svc):
    svc.register_profile(TARGET_A, _profile(enabled=False))
    assert svc.match_profile_snapshot(URL_A) is None


def test_snapshot_retains_values_after_profile_deleted(svc):
    """
    Scenario 15: snapshot captured before deletion remains valid even after
    the profile is removed from the registry.
    """
    svc.register_profile(
        TARGET_A,
        FaultProfile(
            target=TARGET_A,
            error_rate=SNAPSHOT_ERROR_RATE,
            error_codes=[SNAPSHOT_ERROR_CODE],
        ),
    )
    snap = svc.match_profile_snapshot(URL_A)
    assert snap is not None, "snapshot must be available before delete"

    svc.remove_profile(TARGET_A)

    # New match returns None — profile is gone from registry
    assert svc.match_profile_snapshot(URL_A) is None

    # Previously captured snapshot is still fully usable (Scenario 15)
    assert snap.target == TARGET_A
    assert snap.error_rate == SNAPSHOT_ERROR_RATE
    assert snap.error_codes == [SNAPSHOT_ERROR_CODE]


def test_no_snapshot_for_malformed_url(svc_with_profile):
    """
    A URL that causes urlparse to raise ValueError must return None, not
    propagate the exception (covers except ValueError branch in
    match_profile_snapshot).
    """
    # Brackets in the netloc without a valid IPv6 literal trigger ValueError
    # in Python's urlparse on some inputs; using a known-bad IPv6 form.
    malformed = "https://[::invalid/"
    assert svc_with_profile.match_profile_snapshot(malformed) is None
