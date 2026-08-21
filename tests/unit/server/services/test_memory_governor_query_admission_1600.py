"""MemoryGovernor additions for the query-path admission gate (Story #1600).

Covers:
  - GovernorCounters.query_admissions_denied field (defaults to 0)
  - MemoryGovernor._counters_lock (thread-safe increment helper)
  - MemoryGovernor.last_red_min_dwell_seconds property (getattr-with-default,
    mirrors last_used_pct) and its caching in _tick()
"""

from __future__ import annotations

import threading

import pytest

from tests.unit.server.services.test_memory_governor_fixtures import (
    CGROUP_LIMIT_4GB,
    FakeMemoryReaders,
    make_gov,
)

# ---------------------------------------------------------------------------
# Named constants (no magic numbers in assertions)
# ---------------------------------------------------------------------------

_DEFAULT_RED_MIN_DWELL_SECONDS = 30.0  # MemoryGovernor's own hardcoded default
_CUSTOM_RED_MIN_DWELL_SECONDS = 42.0  # arbitrary non-default constructor value
_LIVE_CONFIG_RED_MIN_DWELL_SECONDS = 77.0  # arbitrary value distinct from both above
_PRE_FAILURE_RED_MIN_DWELL_SECONDS = 55.0  # value cached before a reader failure
# H3 fix (Story #1600 review remediation): a THIRD value, distinct from both
# _PRE_FAILURE_RED_MIN_DWELL_SECONDS and every other dwell constant above,
# that the live config_service starts returning on the SECOND (failing)
# tick. Without this, "correctly did not update" and "incorrectly did
# update" are indistinguishable when the governor has no config_service --
# the fallback path re-reads the identical cached constructor value on
# every tick, so a caching-order bug could never be observed.
_THIRD_DISTINCT_RED_MIN_DWELL_SECONDS = 63.0
_DEFAULT_YELLOW_PCT = 70.0
_DEFAULT_RED_PCT = 85.0
_DEFAULT_HYSTERESIS_PCT = 10.0
_DEFAULT_SWAP_PSWPIN_RED_THRESHOLD = 100
_DEFAULT_SAMPLE_INTERVAL_SECONDS = 2.0
_DEFAULT_RSS_INFLATION_FACTOR = 2.0
_SINGLE_INCREMENT_COUNT = 1
_TWO_INCREMENTS_COUNT = 2
_ZERO_COUNT = 0
_CONCURRENT_THREAD_COUNT = 50
_INCREMENTS_PER_THREAD = 200
_EXPECTED_TOTAL_INCREMENTS = _CONCURRENT_THREAD_COUNT * _INCREMENTS_PER_THREAD
_THREAD_JOIN_TIMEOUT_SECONDS = 10.0


@pytest.fixture()
def MemoryGovernor():  # noqa: N802
    from code_indexer.server.services.memory_governor import MemoryGovernor as _MG

    return _MG


@pytest.fixture()
def GovernorCounters():  # noqa: N802
    from code_indexer.server.services.memory_governor import (
        GovernorCounters as _GC,
    )

    return _GC


def _make_stable_governor(MemoryGovernor, **kwargs):
    """A governor whose cgroup usage is fixed at 1% of a 4GB limit — deep in
    GREEN, far from any band-transition edge, so these tests exercise only
    the caching/locking behavior under test, never a band flip."""
    used_bytes = CGROUP_LIMIT_4GB // 100
    readers = FakeMemoryReaders(
        cgroup_v2_max=str(CGROUP_LIMIT_4GB),
        cgroup_v2_current=str(used_bytes),
    )
    return make_gov(readers, MemoryGovernor, **kwargs), readers


class TestGovernorCountersQueryAdmissionsDenied:
    def test_defaults_to_zero(self, GovernorCounters):
        counters = GovernorCounters()
        assert counters.query_admissions_denied == _ZERO_COUNT


class TestLastRedMinDwellSecondsProperty:
    def test_default_before_first_tick_is_thirty(self, MemoryGovernor):
        """Mirrors last_used_pct's getattr-with-default pattern."""
        gov, _ = _make_stable_governor(MemoryGovernor)  # not ticked yet
        assert gov.last_red_min_dwell_seconds == _DEFAULT_RED_MIN_DWELL_SECONDS

    def test_cached_from_constructor_default_after_tick(self, MemoryGovernor):
        gov, _ = _make_stable_governor(
            MemoryGovernor, red_min_dwell_seconds=_CUSTOM_RED_MIN_DWELL_SECONDS
        )
        gov._tick()
        assert gov.last_red_min_dwell_seconds == _CUSTOM_RED_MIN_DWELL_SECONDS

    def test_cached_from_live_config_after_tick(self, MemoryGovernor):
        """When a config_service is set, _tick() must cache the LIVE value,
        not the constructor-frozen fallback."""

        class _FakeCacheConfig:
            memory_governor_yellow_pct = _DEFAULT_YELLOW_PCT
            memory_governor_red_pct = _DEFAULT_RED_PCT
            memory_governor_hysteresis_pct = _DEFAULT_HYSTERESIS_PCT
            memory_governor_swap_forces_red = False
            memory_governor_swap_pswpin_red_threshold = (
                _DEFAULT_SWAP_PSWPIN_RED_THRESHOLD
            )
            memory_governor_red_min_dwell_seconds = _LIVE_CONFIG_RED_MIN_DWELL_SECONDS
            memory_governor_enabled = True
            memory_governor_sample_interval_seconds = _DEFAULT_SAMPLE_INTERVAL_SECONDS
            memory_governor_rss_inflation_factor = _DEFAULT_RSS_INFLATION_FACTOR

        class _FakeConfig:
            cache_config = _FakeCacheConfig()

        class _FakeConfigService:
            def get_config(self):
                return _FakeConfig()

        gov, _ = _make_stable_governor(
            MemoryGovernor,
            red_min_dwell_seconds=_CUSTOM_RED_MIN_DWELL_SECONDS,
            config_service=_FakeConfigService(),
        )
        gov._tick()
        assert gov.last_red_min_dwell_seconds == _LIVE_CONFIG_RED_MIN_DWELL_SECONDS

    def test_not_updated_on_reader_failure(self, MemoryGovernor):
        """A reader exception must leave the previously cached value intact
        (fail-safe RED does not touch _last_red_min_dwell_seconds).

        H3 fix (Story #1600 review remediation): uses a STATEFUL live
        config_service whose memory_governor_red_min_dwell_seconds returns
        a NEW, third distinct value on the second (failing) tick's read --
        so the final assertion actually distinguishes "correctly did not
        update the cache" from "incorrectly did update it". The prior
        version built the governor WITHOUT a config_service, so the
        fallback path re-read the identical constructor-frozen constant on
        both ticks and the assertion would have passed even if the failure
        path incorrectly cached the (indistinguishable) new value.
        """

        class _StatefulCacheConfig:
            memory_governor_yellow_pct = _DEFAULT_YELLOW_PCT
            memory_governor_red_pct = _DEFAULT_RED_PCT
            memory_governor_hysteresis_pct = _DEFAULT_HYSTERESIS_PCT
            memory_governor_swap_forces_red = False
            memory_governor_swap_pswpin_red_threshold = (
                _DEFAULT_SWAP_PSWPIN_RED_THRESHOLD
            )
            memory_governor_enabled = True
            memory_governor_sample_interval_seconds = _DEFAULT_SAMPLE_INTERVAL_SECONDS
            memory_governor_rss_inflation_factor = _DEFAULT_RSS_INFLATION_FACTOR

            def __init__(self) -> None:
                self._read_count = 0

            @property
            def memory_governor_red_min_dwell_seconds(self) -> float:
                self._read_count += 1
                if self._read_count == 1:
                    return _PRE_FAILURE_RED_MIN_DWELL_SECONDS
                return _THIRD_DISTINCT_RED_MIN_DWELL_SECONDS

        class _StatefulConfig:
            def __init__(self) -> None:
                self.cache_config = _StatefulCacheConfig()

        class _StatefulConfigService:
            def __init__(self) -> None:
                self._config = _StatefulConfig()

            def get_config(self) -> _StatefulConfig:
                return self._config

        gov, _ = _make_stable_governor(
            MemoryGovernor, config_service=_StatefulConfigService()
        )
        gov._tick()
        assert gov.last_red_min_dwell_seconds == _PRE_FAILURE_RED_MIN_DWELL_SECONDS

        # Break the reader so the next _tick() hits the except branch --
        # AFTER the live config service has already moved on to returning
        # the THIRD distinct value.
        def _boom():
            raise OSError("simulated reader failure")

        gov._readers.read_pswpin = _boom
        gov._tick()
        # Value from the last SUCCESSFUL tick must still be cached -- NOT
        # the new live value, proving the cache-update is genuinely skipped
        # on the failure path (not just coincidentally unchanged).
        assert gov.last_red_min_dwell_seconds == _PRE_FAILURE_RED_MIN_DWELL_SECONDS
        assert gov.last_red_min_dwell_seconds != _THIRD_DISTINCT_RED_MIN_DWELL_SECONDS


class TestCountersLockAndIncrement:
    def test_counters_lock_exists(self, MemoryGovernor):
        gov, _ = _make_stable_governor(MemoryGovernor)
        assert isinstance(gov._counters_lock, type(threading.Lock()))

    def test_increment_query_admissions_denied_single_call(self, MemoryGovernor):
        gov, _ = _make_stable_governor(MemoryGovernor)
        assert gov.counters.query_admissions_denied == _ZERO_COUNT
        gov.increment_query_admissions_denied()
        assert gov.counters.query_admissions_denied == _SINGLE_INCREMENT_COUNT

    def test_increment_is_thread_safe_under_concurrent_deny_storm(self, MemoryGovernor):
        """Fires the increment from many threads concurrently, synchronized
        to start together via a Barrier to maximize interleaving. The lock
        must prevent silent undercounting (the exact failure mode a bare
        `+= 1` would exhibit under real contention)."""
        gov, _ = _make_stable_governor(MemoryGovernor)

        barrier = threading.Barrier(_CONCURRENT_THREAD_COUNT)

        def _worker():
            barrier.wait()
            for _ in range(_INCREMENTS_PER_THREAD):
                gov.increment_query_admissions_denied()

        threads = [
            threading.Thread(target=_worker) for _ in range(_CONCURRENT_THREAD_COUNT)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
        for t in threads:
            assert not t.is_alive(), "worker thread did not complete within timeout"

        assert gov.counters.query_admissions_denied == _EXPECTED_TOTAL_INCREMENTS


class TestSnapshotIncludesQueryAdmissionsDenied:
    def test_get_snapshot_echoes_counter(self, MemoryGovernor):
        gov, _ = _make_stable_governor(MemoryGovernor)
        gov.increment_query_admissions_denied()
        gov.increment_query_admissions_denied()
        snapshot = gov.get_snapshot()
        assert snapshot["query_admissions_denied"] == _TWO_INCREMENTS_COUNT
