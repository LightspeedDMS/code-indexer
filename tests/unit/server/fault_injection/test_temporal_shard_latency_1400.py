"""
Tests for Story #1400 Phase 10: the "temporal-shard" fault-injection
testability lever.

Resolution (per orchestrator direction): the CLI-safe services/temporal/
package must not import server/app.state directly. The shard loop accepts
an OPTIONAL injectable callable, defaulting to a no-op when None (CLI/
solo/daemon call sites never pass one -- byte-identical behavior). The
SERVER-side caller is the only place that constructs the real
fault-injection-aware callable, reusing the EXISTING FaultProfile.
latency_rate/latency_ms_range mechanism, RNG, and history recording via
FaultInjectionService.get_profile() (exact-key lookup, already existing --
no new lookup method needed) -- NOT the URL/hostname-based
match_profile_snapshot(), since "temporal-shard" is not a hostname.

TDD: written BEFORE implementation.
"""

import random

import pytest

from code_indexer.server.fault_injection.fault_injection_service import (
    FaultInjectionService,
)
from code_indexer.server.fault_injection.fault_profile import FaultProfile
from code_indexer.server.fault_injection.temporal_shard_latency import (
    build_temporal_shard_latency_injector,
)

_TARGET = "temporal-shard"


@pytest.mark.parametrize(
    "service_enabled,register_profile",
    [
        (True, False),  # no profile registered at all
        (False, True),  # profile registered but service disabled
    ],
)
def test_no_op_paths_never_raise_or_record(service_enabled, register_profile):
    service = FaultInjectionService(enabled=service_enabled)
    if register_profile:
        service.register_profile(
            _TARGET,
            FaultProfile(target=_TARGET, latency_rate=1.0, latency_ms_range=(1, 1)),
        )
    injector = build_temporal_shard_latency_injector(service)

    injector(_TARGET)  # must not raise

    counters = service.get_counters()
    assert counters.get((_TARGET, "latency"), 0) == 0


@pytest.mark.parametrize(
    "latency_rate,expected_count",
    [
        (1.0, 1),
        (0.0, 0),
    ],
)
def test_latency_rate_gates_injection_and_recording(latency_rate, expected_count):
    service = FaultInjectionService(enabled=True, rng=random.Random(42))
    service.register_profile(
        _TARGET,
        FaultProfile(
            target=_TARGET, latency_rate=latency_rate, latency_ms_range=(5, 5)
        ),
    )
    injector = build_temporal_shard_latency_injector(service)

    injector(_TARGET)

    counters = service.get_counters()
    assert counters.get((_TARGET, "latency"), 0) == expected_count


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
