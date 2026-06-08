"""Unit tests for ProviderConcurrencyGovernor (Story #1079 Phase B+C, 4 lanes).

The governor was refactored from 2 per-PROVIDER budgets ("voyage", "cohere")
to 4 per-LANE budgets:
    voyage:embed, voyage:rerank, cohere:embed, cohere:rerank

Each lane owns its own ResizableLimiter + AimdController + sinbin health key.
The execute(budget, fn, *, acquire_timeout) API is PRESERVED — only the budget
keys change and an AIMD hook is added (success -> aimd.record(success=True),
429 -> aimd.record(success=False) then re-raise).

Telemetry (in_flight_high_water_mark / acquire_wait_count) is keyed by the 4
lane names. High-water is sourced from each lane's ResizableLimiter (single
source of truth); acquire_wait_count stays governor-maintained.

All tests use controllable fake callables -- NO mocked providers.
"""

import threading
import time
from typing import List
from unittest.mock import patch

import httpx
import pytest

from code_indexer.server.services.provider_concurrency_governor import (
    GovernorBusyError,
    ProviderConcurrencyGovernor,
    ProviderSinbinnedError,
)

# The 4 lane budget keys.
LANES = ["voyage:embed", "voyage:rerank", "cohere:embed", "cohere:rerank"]

# Per-lane expected health keys (sinbin pre-check map).
LANE_HEALTH_KEY = {
    "voyage:embed": "voyage-ai",
    "voyage:rerank": "voyage-reranker",
    "cohere:embed": "cohere",
    "cohere:rerank": "cohere-reranker",
}

_PHM_PATH = "code_indexer.services.provider_health_monitor.ProviderHealthMonitor"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_governor(k: int = 4) -> ProviderConcurrencyGovernor:
    """Create a fresh (non-singleton) governor with initial concurrency K."""
    return ProviderConcurrencyGovernor(max_concurrency=k)


def _make_429_exc() -> httpx.HTTPStatusError:
    """A canonical 429 that provider_backoff.is_rate_limited() recognizes."""
    request = httpx.Request("POST", "https://api.example.com/embed")
    response = httpx.Response(429, request=request)
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


# ---------------------------------------------------------------------------
# Construction — 4 lanes
# ---------------------------------------------------------------------------


class TestGovernorConstruction:
    def test_all_four_lanes_exist(self):
        g = _make_governor()
        for lane in LANES:
            assert g.in_flight_high_water_mark[lane] == 0
            assert g.acquire_wait_count[lane] == 0

    def test_telemetry_dicts_keyed_by_four_lanes(self):
        g = _make_governor()
        assert set(g.in_flight_high_water_mark.keys()) == set(LANES)
        assert set(g.acquire_wait_count.keys()) == set(LANES)

    def test_invalid_budget_raises(self):
        g = _make_governor()
        with pytest.raises(KeyError):
            g.execute("unknown_budget", lambda: None, acquire_timeout=1.0)

    def test_old_provider_only_keys_now_invalid(self):
        """The pre-refactor 'voyage'/'cohere' keys are no longer valid lanes."""
        g = _make_governor()
        with pytest.raises(KeyError):
            g.execute("voyage", lambda: None, acquire_timeout=1.0)
        with pytest.raises(KeyError):
            g.execute("cohere", lambda: None, acquire_timeout=1.0)


# ---------------------------------------------------------------------------
# Concurrency cap (per lane, sourced from ResizableLimiter)
# ---------------------------------------------------------------------------


class TestConcurrencyCap:
    def test_at_most_k_concurrent_per_lane(self):
        """No more than K callables run at the same time on a lane."""
        K = 3
        g = _make_governor(k=K)
        in_flight = [0]
        peak = [0]
        lock = threading.Lock()
        barrier = threading.Barrier(K + 1)

        def slow_fn():
            with lock:
                in_flight[0] += 1
                if in_flight[0] > peak[0]:
                    peak[0] = in_flight[0]
            barrier.wait()
            with lock:
                in_flight[0] -= 1

        threads = [
            threading.Thread(
                target=lambda: g.execute("voyage:embed", slow_fn, acquire_timeout=5.0)
            )
            for _ in range(K)
        ]
        for t in threads:
            t.start()
        time.sleep(0.2)
        barrier.wait()
        for t in threads:
            t.join(timeout=5)

        assert peak[0] == K
        # High-water is sourced from the lane's ResizableLimiter.
        assert g.in_flight_high_water_mark["voyage:embed"] == K

    def test_acquire_timeout_raises_governor_busy_error(self):
        # The limiter floor is K_MIN=8, so a k=1 seed still yields a lane with 8
        # slots. Saturate the lane to its actual limit before testing the timeout.
        g = _make_governor(k=1)
        limit = g.aimd("voyage:embed").k  # == limiter limit == 8 (clamped)
        release = threading.Event()
        ready = threading.Barrier(limit + 1)

        def hold():
            ready.wait(timeout=5)
            release.wait(timeout=5)

        holders = [
            threading.Thread(
                target=lambda: g.execute("voyage:embed", hold, acquire_timeout=5.0)
            )
            for _ in range(limit)
        ]
        for t in holders:
            t.start()
        ready.wait(timeout=5)  # all `limit` holders have acquired their slots

        with pytest.raises(GovernorBusyError):
            g.execute("voyage:embed", lambda: None, acquire_timeout=0.05)

        release.set()
        for t in holders:
            t.join(timeout=5)

    def test_acquire_wait_count_increments_when_blocked(self):
        g = _make_governor(k=1)
        limit = g.aimd("cohere:rerank").k  # == 8 (clamped floor)
        release = threading.Event()
        ready = threading.Barrier(limit + 1)

        def hold():
            ready.wait(timeout=5)
            release.wait(timeout=5)

        holders = [
            threading.Thread(
                target=lambda: g.execute("cohere:rerank", hold, acquire_timeout=5.0)
            )
            for _ in range(limit)
        ]
        for t in holders:
            t.start()
        ready.wait(timeout=5)  # lane fully saturated

        result_holder: List[str] = []
        waiter = threading.Thread(
            target=lambda: result_holder.append(
                g.execute("cohere:rerank", lambda: "done", acquire_timeout=5.0)
            )
        )
        waiter.start()
        time.sleep(0.05)  # let the waiter park (in_flight == limit)
        release.set()
        waiter.join(timeout=5)
        for t in holders:
            t.join(timeout=5)

        assert g.acquire_wait_count["cohere:rerank"] >= 1
        # Other lanes unaffected
        assert g.acquire_wait_count["voyage:embed"] == 0


# ---------------------------------------------------------------------------
# Sinbin pre-check — per-lane health key
# ---------------------------------------------------------------------------


class TestSinbinPrecheck:
    def test_sinbinned_lane_raises_without_consuming_slot(self):
        K = 1
        g = _make_governor(k=K)
        with patch(f"{_PHM_PATH}.is_sinbinned", return_value=True):
            with pytest.raises(ProviderSinbinnedError):
                g.execute("voyage:embed", lambda: None, acquire_timeout=0.1)

        # Slot not consumed — a subsequent call must run immediately.
        ran: List[bool] = []
        g.execute("voyage:embed", lambda: ran.append(True), acquire_timeout=0.2)
        assert ran == [True]

    @pytest.mark.parametrize("lane", LANES)
    def test_each_lane_checks_only_its_own_health_key(self, lane: str):
        g = _make_governor(k=2)
        checked: List[str] = []

        def record_check(key: str) -> bool:
            checked.append(key)
            return False

        with patch(f"{_PHM_PATH}.is_sinbinned", side_effect=record_check):
            g.execute(lane, lambda: None, acquire_timeout=1.0)

        assert checked == [LANE_HEALTH_KEY[lane]], (
            f"Lane {lane} should check exactly [{LANE_HEALTH_KEY[lane]}], got {checked}"
        )


# ---------------------------------------------------------------------------
# AIMD hook — success / 429
# ---------------------------------------------------------------------------


class TestAimdHook:
    def test_success_records_success_on_lane_aimd(self):
        g = _make_governor(k=8)
        calls: List[bool] = []
        with patch.object(
            g.aimd("voyage:embed"),
            "record",
            side_effect=lambda **kw: calls.append(kw["success"]),
        ):
            g.execute("voyage:embed", lambda: "ok", acquire_timeout=1.0)
        assert calls == [True]

    def test_rate_limited_exc_records_429_then_reraises(self):
        g = _make_governor(k=8)
        calls: List[bool] = []
        exc = _make_429_exc()

        def boom():
            raise exc

        with patch.object(
            g.aimd("voyage:rerank"),
            "record",
            side_effect=lambda **kw: calls.append(kw["success"]),
        ):
            with pytest.raises(httpx.HTTPStatusError):
                g.execute("voyage:rerank", boom, acquire_timeout=1.0)
        assert calls == [False], (
            "429 must record success=False (multiplicative decrease)"
        )

    def test_non_rate_limited_exc_does_not_record_429(self):
        g = _make_governor(k=8)
        calls: List[bool] = []

        def boom():
            raise ValueError("not a 429")

        with patch.object(
            g.aimd("cohere:embed"),
            "record",
            side_effect=lambda **kw: calls.append(kw["success"]),
        ):
            with pytest.raises(ValueError):
                g.execute("cohere:embed", boom, acquire_timeout=1.0)
        assert calls == [], "non-429 errors must NOT trigger an AIMD decrease"

    def test_slot_released_after_exception(self):
        K = 1
        g = _make_governor(k=K)
        with pytest.raises(RuntimeError):
            g.execute(
                "voyage:embed",
                lambda: (_ for _ in ()).throw(RuntimeError("fail")),
                acquire_timeout=1.0,
            )
        # Slot must be released — this must not timeout.
        assert (
            g.execute("voyage:embed", lambda: "after", acquire_timeout=0.5) == "after"
        )


# ---------------------------------------------------------------------------
# Lane independence — a 429 on one lane never changes another lane's K
# ---------------------------------------------------------------------------


class TestLaneIndependence:
    def test_429_on_one_lane_does_not_change_other_lanes_k(self):
        g = _make_governor(k=8)
        # Drive voyage:rerank K up so a halving is observable.
        rerank_aimd = g.aimd("voyage:rerank")
        rerank_aimd._k = 32  # type: ignore[attr-defined]

        before = {lane: g.aimd(lane).k for lane in LANES}

        exc = _make_429_exc()

        def boom():
            raise exc

        with pytest.raises(httpx.HTTPStatusError):
            g.execute("voyage:rerank", boom, acquire_timeout=1.0)

        after = {lane: g.aimd(lane).k for lane in LANES}

        # The hit lane decreased.
        assert after["voyage:rerank"] < before["voyage:rerank"]
        # Every OTHER lane is untouched.
        for lane in LANES:
            if lane == "voyage:rerank":
                continue
            assert after[lane] == before[lane], (
                f"lane {lane} K changed ({before[lane]} -> {after[lane]}) "
                "after a 429 on voyage:rerank — lanes must be independent"
            )


# ---------------------------------------------------------------------------
# Result propagation
# ---------------------------------------------------------------------------


class TestResultPropagation:
    def test_execute_returns_fn_result(self):
        g = _make_governor(k=2)
        assert g.execute("voyage:embed", lambda: 42, acquire_timeout=1.0) == 42

    def test_execute_propagates_exception(self):
        g = _make_governor(k=2)
        with pytest.raises(ValueError, match="boom"):
            g.execute(
                "cohere:embed",
                lambda: (_ for _ in ()).throw(ValueError("boom")),
                acquire_timeout=1.0,
            )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def setup_method(self):
        ProviderConcurrencyGovernor.reset_instance()

    def teardown_method(self):
        ProviderConcurrencyGovernor.reset_instance()

    def test_get_instance_returns_same_object(self):
        a = ProviderConcurrencyGovernor.get_instance()
        b = ProviderConcurrencyGovernor.get_instance()
        assert a is b

    def test_reset_instance_creates_fresh(self):
        a = ProviderConcurrencyGovernor.get_instance()
        ProviderConcurrencyGovernor.reset_instance()
        b = ProviderConcurrencyGovernor.get_instance()
        assert a is not b


# ---------------------------------------------------------------------------
# Config seed clamping
# ---------------------------------------------------------------------------


class TestConfigSeedClamp:
    def test_initial_k_clamped_into_range(self):
        # Default config value (16) must clamp into [8, 32].
        g_low = ProviderConcurrencyGovernor(max_concurrency=1)
        assert g_low.aimd("voyage:embed").k == 8
        g_high = ProviderConcurrencyGovernor(max_concurrency=999)
        assert g_high.aimd("voyage:embed").k == 32
        g_mid = ProviderConcurrencyGovernor(max_concurrency=20)
        assert g_mid.aimd("voyage:embed").k == 20

    def test_unreadable_config_seeds_default_floor(self):
        """When the config service raises, the seed defaults to K_MIN (8)."""
        with patch(
            "code_indexer.server.services.config_service.get_config_service",
            side_effect=RuntimeError("config not initialized"),
        ):
            g = ProviderConcurrencyGovernor()  # no explicit max_concurrency
        for lane in LANES:
            assert g.aimd(lane).k == 8, (
                f"lane {lane} should seed to K_MIN=8 when config is unreadable"
            )
