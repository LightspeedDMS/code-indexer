"""Shared fixtures and constants for MemoryGovernor unit tests (Story #1213).

Import this module's helpers in each focused test module to avoid duplication.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Byte-size helpers
# ---------------------------------------------------------------------------

GB = 1024 * 1024 * 1024

# Standard test host dimensions (32 GB total, 8 GB used = 25%)
DEFAULT_HOST_TOTAL = 32 * GB
DEFAULT_HOST_USED = 8 * GB

# A 4 GB container limit used across cgroup tests
CGROUP_LIMIT_4GB = 4 * GB

# A 64 GB container limit — larger than host to exercise min(host, cgroup)
CGROUP_LIMIT_LARGER_THAN_HOST = 64 * GB

# Specific host memory sizes for individual tests
HOST_8GB = 8 * GB
HOST_16GB = 16 * GB
HOST_100GB = 100 * GB  # 100 GB host for band-transition tests (easy %-to-bytes math)

# Specific used-memory values
USED_2GB = 2 * GB
USED_4GB = 4 * GB

# Assertion tolerances
PCT_TOLERANCE_EXACT = 0.01  # for computed values where math is exact
PCT_TOLERANCE_ROUNDED = 0.1  # for integer-truncated byte values

# ---------------------------------------------------------------------------
# Threshold constants (must match MemoryGovernor defaults)
# ---------------------------------------------------------------------------

YELLOW_PCT = 70.0
RED_PCT = 85.0
HYSTERESIS_PCT = 10.0

# Derived exit thresholds (used in assertions and test calculations)
YELLOW_EXIT_PCT = YELLOW_PCT - HYSTERESIS_PCT  # 60.0
RED_EXIT_PCT = RED_PCT - HYSTERESIS_PCT  # 75.0

# Aliases for readability in test assertions
RED_EXIT_THRESHOLD = RED_EXIT_PCT  # must drop below this to leave RED
YELLOW_EXIT_THRESHOLD = YELLOW_EXIT_PCT  # must drop below this to leave YELLOW

# Expected counter values for transition assertions
EXPECTED_ZERO_TRANSITIONS = 0
EXPECTED_ONE_TRANSITION = 1
EXPECTED_AT_LEAST_ONE = 1  # used with >= comparisons

# Hysteresis oscillation loop bound (avoids magic `range(3)`)
HYSTERESIS_OSCILLATION_ROUNDS = 3

# ---------------------------------------------------------------------------
# cgroup sentinel values
# ---------------------------------------------------------------------------

CGROUP_V1_UNLIMITED_SENTINEL = 9223372036854771712  # common Linux "no limit" value

# ---------------------------------------------------------------------------
# pswpin scenario values
# ---------------------------------------------------------------------------

PSWPIN_BASELINE = 100
# delta = 100 => meets default threshold (memory_governor_swap_pswpin_red_threshold=100)
# and forces RED.  Before Bug #1225 this was 105 (delta=5), which forced RED under
# the old `> 0` rule.  After the fix only deltas >= threshold trigger RED.
PSWPIN_AFTER_SWAP_ACTIVITY = 200  # delta = 100 => forces RED (meets default threshold)
PSWPIN_BELOW_THRESHOLD = (
    PSWPIN_BASELINE + 5
)  # delta = 5 => below default threshold, no RED
PSWPIN_STABLE_HIGH = 500  # high absolute but no delta
PSWPIN_FIRST_SAMPLE_HIGH = 999  # large on first sample (delta treated as 0)

# ---------------------------------------------------------------------------
# Percentage scenario values
# ---------------------------------------------------------------------------

PCT_DIVISOR = 100.0  # use in `int(limit * PCT / PCT_DIVISOR)` expressions

HALF_USED_PCT = 50.0
FIFTY_PCT = 50.0
FIFTY_NINE_PCT = 59.0
SIXTY_PCT = 60.0
SIXTY_TWO_PCT = 62.0
SIXTY_EIGHT_PCT = 68.0
TWENTY_FIVE_PCT = 25.0
THIRTY_PCT = 30.0
SEVENTY_TWO_PCT = 72.0
SEVENTY_FOUR_PCT = 74.0
SEVENTY_FIVE_PCT = 75.0
SEVENTY_SIX_PCT = 76.0
EIGHTY_FIVE_PCT = 85.0
EIGHTY_SEVEN_PCT = 87.0
NINETY_PCT = 90.0
TWELVE_PT_FIVE_PCT = 12.5

# ---------------------------------------------------------------------------
# RED min-dwell test constants
# ---------------------------------------------------------------------------

RED_MIN_DWELL_SECONDS = 30  # default dwell before RED can be exited
FAKE_TIME_START = 1000.0  # fake monotonic start time for dwell tests
DWELL_ELAPSED_BEFORE = 15.0  # elapsed < dwell => still in RED
DWELL_ELAPSED_AFTER = 31.0  # elapsed > dwell => can exit RED

# ---------------------------------------------------------------------------
# Threading test constants
# ---------------------------------------------------------------------------

FAST_SAMPLE_INTERVAL = 0.05  # seconds — fast enough for unit tests
FAST_SAMPLE_WAIT = 0.15  # seconds — wait at least three fast samples
TEST_STOP_TIMEOUT = 2.0  # seconds — generous join timeout

# ---------------------------------------------------------------------------
# FakeMemoryReaders
# ---------------------------------------------------------------------------


class FakeMemoryReaders:
    """Injectable memory readers — no real cgroup or psutil I/O.

    All source values are plain attributes; tests update them between calls.
    Setting a cgroup_v2_* or cgroup_v1_* value to None makes that reader raise
    FileNotFoundError (simulating "not present on this host").
    """

    def __init__(
        self,
        *,
        cgroup_v2_max: str | None = None,
        cgroup_v2_current: str | None = None,
        cgroup_v1_limit: str | None = None,
        cgroup_v1_usage: str | None = None,
        host_total: int = DEFAULT_HOST_TOTAL,
        host_used: int = DEFAULT_HOST_USED,
        pswpin: int = 0,
    ) -> None:
        self.cgroup_v2_max = cgroup_v2_max
        self.cgroup_v2_current = cgroup_v2_current
        self.cgroup_v1_limit = cgroup_v1_limit
        self.cgroup_v1_usage = cgroup_v1_usage
        self.host_total = host_total
        self.host_used = host_used
        self.pswpin = pswpin

    def read_cgroup_v2_max(self) -> str:
        if self.cgroup_v2_max is None:
            raise FileNotFoundError("cgroup v2 not available")
        return self.cgroup_v2_max

    def read_cgroup_v2_current(self) -> int:
        if self.cgroup_v2_current is None:
            raise FileNotFoundError("cgroup v2 not available")
        return int(self.cgroup_v2_current)

    def read_cgroup_v1_limit(self) -> int:
        if self.cgroup_v1_limit is None:
            raise FileNotFoundError("cgroup v1 not available")
        return int(self.cgroup_v1_limit)

    def read_cgroup_v1_usage(self) -> int:
        if self.cgroup_v1_usage is None:
            raise FileNotFoundError("cgroup v1 not available")
        return int(self.cgroup_v1_usage)

    def read_host_memory(self) -> object:
        class _Vm:
            pass

        vm = _Vm()
        vm.total = self.host_total  # type: ignore[attr-defined]
        vm.used = self.host_used  # type: ignore[attr-defined]
        return vm

    def read_pswpin(self) -> int:
        return self.pswpin


def make_gov(readers: FakeMemoryReaders, MemoryGovernor, **kwargs):
    """Build a governor with standard test defaults; override via kwargs."""
    # Assemble defaults first, then overlay caller overrides so that
    # parameters like red_min_dwell_seconds and swap_forces_red can be
    # overridden by kwargs without causing "multiple values" TypeError.
    defaults = {
        "yellow_pct": YELLOW_PCT,
        "red_pct": RED_PCT,
        "hysteresis_pct": HYSTERESIS_PCT,
        "red_min_dwell_seconds": 0,
        "swap_forces_red": False,
    }
    defaults.update(kwargs)
    return MemoryGovernor(
        readers=readers,
        enabled=True,
        start_sampler=False,
        **defaults,
    )


def gov_at_pct(pct: float, MemoryGovernor, **kwargs):
    """Create a governor with host memory at `pct`% of a 100 GB host."""
    total = 100 * GB
    used = int(total * (pct / 100.0))
    readers = FakeMemoryReaders(host_total=total, host_used=used)
    return make_gov(readers, MemoryGovernor, **kwargs)
