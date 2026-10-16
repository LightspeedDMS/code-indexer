"""Timing constants of SIEM delivery (code constants, never settings).

Production values are the spec's.  A process whose NON-PRODUCTION
fault-injection harness passed its startup gate (``fault_injection_enabled``
plus ``fault_injection_nonprod_ack``, refused in production) runs the
compressed HARNESS profile instead, so end-to-end tests against the mock
SecOps receiver finish in minutes rather than hours.  This is a test hook,
not an operator knob: there is no setting, and a production process can
never select it (the gate refuses to start there).

Values that bound correctness or signal real incidents are NOT compressed:
the 90 s capture-snapshot bound, the quarantine cap and window, the
process TTL, and the operator ERROR alert thresholds.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass


@dataclass(frozen=True)
class SiemTimings:
    cycle_idle_seconds: float = 30.0
    cycle_busy_seconds: float = 1.0
    stats_refresh_seconds: float = 60.0
    lease_seconds: float = 600.0
    lease_renew_margin_seconds: float = 60.0
    request_timeout_seconds: float = 60.0
    token_timeout_seconds: float = 20.0
    probe_interval_seconds: float = 900.0  # systemic-halt probe
    credential_probe_every_seconds: float = 300.0
    probe_fresh_seconds: float = 600.0
    process_ttl_seconds: float = 180.0
    process_refresh_when_remaining_seconds: float = 120.0
    backoff_base_seconds: float = 30.0
    backoff_cap_seconds: float = 3600.0
    tick_time_budget_seconds: float = 25.0
    backlog_degraded_age_seconds: float = 900.0
    quarantine_window_seconds: float = 3600.0
    # operator ERROR alert thresholds (never compressed)
    alert_oldest_pending_seconds: float = 3600.0
    alert_halted_seconds: float = 900.0
    alert_repeat_seconds: float = 3600.0


PRODUCTION_TIMINGS = SiemTimings()

HARNESS_TIMINGS = dataclasses.replace(
    PRODUCTION_TIMINGS,
    cycle_idle_seconds=1.0,
    cycle_busy_seconds=0.5,
    stats_refresh_seconds=1.0,
    lease_seconds=30.0,
    lease_renew_margin_seconds=2.0,
    request_timeout_seconds=10.0,
    token_timeout_seconds=5.0,
    probe_interval_seconds=10.0,
    credential_probe_every_seconds=60.0,
    probe_fresh_seconds=180.0,
    backoff_base_seconds=4.0,
    backoff_cap_seconds=30.0,
    backlog_degraded_age_seconds=10.0,
)


def timings_for(harness_active: bool) -> SiemTimings:
    return HARNESS_TIMINGS if harness_active else PRODUCTION_TIMINGS
