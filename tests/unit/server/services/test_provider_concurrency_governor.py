"""Unit tests for ProviderConcurrencyGovernor (Bug #1078 Phase 1).

Tests governor construction, semaphore cap, high-water-mark tracking,
acquire_wait_count, sinbin pre-check (fast-fail without slot consumption),
and GovernorBusyError on timeout.

All tests use controllable fake callables -- NO mocked providers.
"""

import threading
import time
from typing import List
from unittest.mock import patch

import pytest

from code_indexer.server.services.provider_concurrency_governor import (
    GovernorBusyError,
    ProviderSinbinnedError,
    ProviderConcurrencyGovernor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_governor(k: int = 4) -> ProviderConcurrencyGovernor:
    """Create a fresh (non-singleton) governor with concurrency K."""
    return ProviderConcurrencyGovernor(max_concurrency=k)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestGovernorConstruction:
    def test_default_budgets_exist(self):
        g = _make_governor()
        # Both budget keys must be present
        assert g.in_flight_high_water_mark["voyage"] == 0
        assert g.in_flight_high_water_mark["cohere"] == 0
        assert g.acquire_wait_count["voyage"] == 0
        assert g.acquire_wait_count["cohere"] == 0

    def test_invalid_budget_raises(self):
        g = _make_governor()
        with pytest.raises(KeyError):
            g.execute("unknown_budget", lambda: None, acquire_timeout=1.0)


# ---------------------------------------------------------------------------
# Semaphore cap
# ---------------------------------------------------------------------------


class TestSemaphoreCap:
    def test_at_most_k_concurrent(self):
        """No more than K callables run at the same time."""
        K = 3
        g = _make_governor(k=K)
        in_flight = [0]
        peak = [0]
        lock = threading.Lock()
        barrier = threading.Barrier(K + 1)  # K workers + 1 main thread

        def slow_fn():
            with lock:
                in_flight[0] += 1
                if in_flight[0] > peak[0]:
                    peak[0] = in_flight[0]
            barrier.wait()  # hold the slot
            with lock:
                in_flight[0] -= 1

        threads = []
        for _ in range(K):
            t = threading.Thread(
                target=lambda: g.execute("voyage", slow_fn, acquire_timeout=5.0)
            )
            threads.append(t)

        for t in threads:
            t.start()

        # Give threads time to acquire their slots
        time.sleep(0.2)
        barrier.wait()  # release all

        for t in threads:
            t.join(timeout=5)

        assert peak[0] == K, f"Expected peak {K}, got {peak[0]}"
        assert g.in_flight_high_water_mark["voyage"] == K

    def test_k_plus_one_blocks_then_proceeds(self):
        """K+1-th request blocks until a slot is freed."""
        K = 2
        g = _make_governor(k=K)
        results = []
        release_event = threading.Event()

        def hold_slot():
            release_event.wait(timeout=5)
            return "held"

        def quick_fn():
            return "quick"

        # Fill all K slots
        holders = []
        for _ in range(K):
            t = threading.Thread(
                target=lambda: results.append(
                    g.execute("voyage", hold_slot, acquire_timeout=5.0)
                )
            )
            holders.append(t)
            t.start()

        time.sleep(0.15)  # let holders acquire slots

        # K+1-th request will block
        late_result = []
        late_t = threading.Thread(
            target=lambda: late_result.append(
                g.execute("voyage", quick_fn, acquire_timeout=5.0)
            )
        )
        late_t.start()

        time.sleep(0.1)
        # Late thread is blocked; release holders
        release_event.set()

        late_t.join(timeout=5)
        for t in holders:
            t.join(timeout=5)

        assert "quick" in late_result, "Late request should have run after slot freed"

    def test_acquire_timeout_raises_governor_busy_error(self):
        """When all slots are occupied and timeout is reached, raise GovernorBusyError."""
        K = 1
        g = _make_governor(k=K)
        holding = threading.Event()
        release = threading.Event()

        def hold():
            holding.set()
            release.wait(timeout=5)

        holder = threading.Thread(
            target=lambda: g.execute("voyage", hold, acquire_timeout=5.0)
        )
        holder.start()
        holding.wait(timeout=2)

        with pytest.raises(GovernorBusyError):
            g.execute("voyage", lambda: None, acquire_timeout=0.05)

        release.set()
        holder.join(timeout=5)

    def test_acquire_wait_count_increments_when_blocked(self):
        """acquire_wait_count increments when a request had to wait for a slot."""
        K = 1
        g = _make_governor(k=K)
        release = threading.Event()
        ready = threading.Event()

        def hold():
            ready.set()
            release.wait(timeout=5)

        def quick():
            return "done"

        holder = threading.Thread(
            target=lambda: g.execute("voyage", hold, acquire_timeout=5.0)
        )
        holder.start()
        ready.wait(timeout=2)

        # This one will have to wait
        result_holder = []
        waiter = threading.Thread(
            target=lambda: result_holder.append(
                g.execute("voyage", quick, acquire_timeout=5.0)
            )
        )
        waiter.start()

        time.sleep(0.05)
        release.set()
        waiter.join(timeout=5)
        holder.join(timeout=5)

        assert g.acquire_wait_count["voyage"] >= 1, (
            "Should record that a request waited"
        )


# ---------------------------------------------------------------------------
# Sinbin pre-check
# ---------------------------------------------------------------------------


_PHM_PATH = "code_indexer.services.provider_health_monitor.ProviderHealthMonitor"


class TestSinbinPrecheck:
    def test_sinbinned_provider_raises_without_consuming_slot(self):
        """When all mapped health keys are sinbinned, raise fast without acquiring semaphore."""
        K = 1
        g = _make_governor(k=K)

        with patch(f"{_PHM_PATH}.is_sinbinned", return_value=True):
            with pytest.raises(ProviderSinbinnedError):
                g.execute("voyage", lambda: None, acquire_timeout=0.1)

        # Semaphore should not have been acquired (count should still be K)
        # Verify by immediately running K tasks without blocking
        results: List[bool] = []

        def quick() -> bool:
            results.append(True)
            return True

        # If semaphore was consumed, this would block/fail
        for _ in range(K):
            g.execute("voyage", quick, acquire_timeout=0.2)

        assert len(results) == K, (
            "Semaphore should not have been consumed by sinbinned fast-fail"
        )

    def test_not_sinbinned_proceeds_normally(self):
        """When provider is not sinbinned, execute runs normally."""
        g = _make_governor(k=2)

        with patch(f"{_PHM_PATH}.is_sinbinned", return_value=False):
            result = g.execute("voyage", lambda: "ok", acquire_timeout=1.0)
            assert result == "ok"

    def test_cohere_budget_checks_cohere_health_keys(self):
        """The 'cohere' budget checks cohere-* health keys, not voyage-* keys."""
        g = _make_governor(k=2)
        checked_keys: List[str] = []

        def record_sinbin_check(key: str) -> bool:
            checked_keys.append(key)
            return False

        with patch(f"{_PHM_PATH}.is_sinbinned", side_effect=record_sinbin_check):
            g.execute("cohere", lambda: None, acquire_timeout=1.0)

        assert all("cohere" in k for k in checked_keys), (
            f"Expected only cohere keys, got: {checked_keys}"
        )
        assert not any("voyage" in k for k in checked_keys)

    def test_voyage_budget_checks_voyage_health_keys(self):
        """The 'voyage' budget checks voyage-* health keys, not cohere-* keys."""
        g = _make_governor(k=2)
        checked_keys: List[str] = []

        def record_sinbin_check(key: str) -> bool:
            checked_keys.append(key)
            return False

        with patch(f"{_PHM_PATH}.is_sinbinned", side_effect=record_sinbin_check):
            g.execute("voyage", lambda: None, acquire_timeout=1.0)

        assert all("voyage" in k for k in checked_keys), (
            f"Expected only voyage keys, got: {checked_keys}"
        )
        assert not any("cohere" in k for k in checked_keys)


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
# Result propagation
# ---------------------------------------------------------------------------


class TestResultPropagation:
    def test_execute_returns_fn_result(self):
        g = _make_governor(k=2)
        result = g.execute("voyage", lambda: 42, acquire_timeout=1.0)
        assert result == 42

    def test_execute_propagates_exception(self):
        g = _make_governor(k=2)
        with pytest.raises(ValueError, match="boom"):
            g.execute(
                "voyage",
                lambda: (_ for _ in ()).throw(ValueError("boom")),
                acquire_timeout=1.0,
            )

    def test_slot_released_after_exception(self):
        """Slot is released even when fn raises, so subsequent calls succeed."""
        K = 1
        g = _make_governor(k=K)

        with pytest.raises(RuntimeError):
            g.execute(
                "voyage",
                lambda: (_ for _ in ()).throw(RuntimeError("fail")),
                acquire_timeout=1.0,
            )

        # Slot must be released — this must not timeout
        result = g.execute("voyage", lambda: "after_error", acquire_timeout=0.5)
        assert result == "after_error"
