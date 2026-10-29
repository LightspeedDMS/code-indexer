import threading
from typing import List

from code_indexer.services.provider_health_monitor import (
    DEFAULT_DOWN_CONSECUTIVE_FAILURES,
    ProviderHealthMonitor,
)

# ^ invariant pytest runs one file's tests sequentially in one thread => this handoff needs no lock
_EARLIER_PROBES: List[threading.Thread] = []


# ^ order this test before test_singleton_not_sinbinned_by_an_earlier_test; pytest runs a file top to bottom
def test_an_earlier_test_drives_the_singleton_provider_down() -> None:
    monitor = ProviderHealthMonitor.get_instance()
    for _ in range(DEFAULT_DOWN_CONSECUTIVE_FAILURES):
        monitor.record_call("voyage-ai", latency_ms=0.0, success=False)
    monitor.sinbin("voyage-ai")
    _EARLIER_PROBES.append(monitor._probe_threads["voyage-ai"])
    assert monitor.is_sinbinned("voyage-ai")


def test_singleton_not_sinbinned_by_an_earlier_test() -> None:
    assert _EARLIER_PROBES, "the first test of this file must run first"
    assert not ProviderHealthMonitor.get_instance().is_sinbinned("voyage-ai")
    probe = _EARLIER_PROBES[0]
    probe.join(timeout=ProviderHealthMonitor.PROBE_JOIN_TIMEOUT_SEC)
    assert not probe.is_alive()
