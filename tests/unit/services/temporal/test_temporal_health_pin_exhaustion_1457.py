"""record_temporal_pin_exhaustion() (Story #1457 AC8 Step 6): a dedicated
observability counter for pin-acquisition exhaustion, deliberately kept OUT
of the provider circuit breaker (ProviderHealthMonitor) -- a lost
resolve/validate race against a concurrent alias swap is not a
provider-health signal and must not degrade `record_temporal_failure`'s
circuit-breaker scoring.
"""

from __future__ import annotations

from code_indexer.services.temporal.temporal_health import (
    get_temporal_pin_exhaustion_count,
    record_temporal_pin_exhaustion,
)


def test_record_pin_exhaustion_increments_dedicated_counter():
    key = "voyage-code-3-pin-exhaustion-test-1457"
    before = get_temporal_pin_exhaustion_count(key)

    record_temporal_pin_exhaustion(key)

    assert get_temporal_pin_exhaustion_count(key) == before + 1


def test_record_pin_exhaustion_does_not_touch_provider_health_monitor():
    """Recording pin exhaustion must NEVER call ProviderHealthMonitor --
    verified by asserting the health status stays unaffected (still healthy,
    default) for a key that has never had a real failure recorded."""
    from code_indexer.services.temporal.temporal_health import (
        is_temporal_provider_healthy,
    )

    key = "voyage-code-3-pin-exhaustion-health-isolation-1457"

    for _ in range(10):
        record_temporal_pin_exhaustion(key)

    assert is_temporal_provider_healthy(key) is True
