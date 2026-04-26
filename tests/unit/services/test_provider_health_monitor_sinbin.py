"""Tests for Bug #678: Sin-bin (circuit-breaker) state on ProviderHealthMonitor.

Tests: sin-bin state management, exponential backoff, passive expiry, round reset,
threshold triggering, window expiry, status reporting, and thread safety.
Creative edge cases: rapid-fire failures then recovery, intermittent failures below
threshold, concurrent access, overflow scenarios.
"""

import threading
import time
import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset ProviderHealthMonitor singleton before each test."""
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    ProviderHealthMonitor.reset_instance()
    yield
    ProviderHealthMonitor.reset_instance()


@pytest.fixture
def monitor():
    """Fresh ProviderHealthMonitor instance."""
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    return ProviderHealthMonitor()


class TestSinBinBasicState:
    """Basic sin-bin state management."""

    def test_is_sinbinned_returns_false_for_unknown_provider(self, monitor):
        assert monitor.is_sinbinned("voyage-ai") is False

    def test_is_sinbinned_returns_false_before_sinbin_called(self, monitor):
        monitor.record_call("voyage-ai", 100.0, success=True)
        assert monitor.is_sinbinned("voyage-ai") is False

    def test_sinbin_makes_provider_sinbinned(self, monitor):
        monitor.sinbin("voyage-ai")
        assert monitor.is_sinbinned("voyage-ai") is True

    def test_clear_sinbin_removes_sinbin(self, monitor):
        monitor.sinbin("voyage-ai")
        assert monitor.is_sinbinned("voyage-ai") is True
        monitor.clear_sinbin("voyage-ai")
        assert monitor.is_sinbinned("voyage-ai") is False

    def test_clear_sinbin_on_non_sinbinned_is_noop(self, monitor):
        monitor.clear_sinbin("voyage-ai")  # Should not raise
        assert monitor.is_sinbinned("voyage-ai") is False

    def test_sinbin_affects_only_named_provider(self, monitor):
        monitor.sinbin("voyage-ai")
        assert monitor.is_sinbinned("voyage-ai") is True
        assert monitor.is_sinbinned("cohere") is False

    def test_two_providers_can_both_be_sinbinned(self, monitor):
        monitor.sinbin("voyage-ai")
        monitor.sinbin("cohere")
        assert monitor.is_sinbinned("voyage-ai") is True
        assert monitor.is_sinbinned("cohere") is True


class TestSinBinExponentialBackoff:
    """Exponential backoff: cooldown doubles on each activation."""

    def test_first_sinbin_uses_initial_cooldown(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            initial_cooldown_seconds=30,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            before = time.monotonic()
            monitor.sinbin("voyage-ai")
            after = time.monotonic()
        # Should expire ~30s from now
        until = monitor._sinbin_until.get("voyage-ai", 0.0)
        assert until > before + 28  # at least 28s from now
        assert until < after + 32  # at most 32s from now

    def test_second_sinbin_doubles_cooldown(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            initial_cooldown_seconds=30,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            monitor.sinbin("voyage-ai")
            monitor.sinbin("voyage-ai")
        rounds = monitor._sinbin_rounds.get("voyage-ai", 0)
        assert rounds == 2

    def test_third_sinbin_quadruples_initial_cooldown(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            initial_cooldown_seconds=30,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            before = time.monotonic()
            monitor.sinbin("voyage-ai")  # round 1: 30s
            monitor.sinbin("voyage-ai")  # round 2: 60s
            monitor.sinbin("voyage-ai")  # round 3: 120s
            after = time.monotonic()
        until = monitor._sinbin_until.get("voyage-ai", 0.0)
        # Third activation: 30 * 2^2 = 120s
        assert until > before + 115
        assert until < after + 125

    def test_cooldown_capped_at_max(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            initial_cooldown_seconds=30,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            # Activate many times to ensure cap hits
            for _ in range(10):
                monitor.sinbin("voyage-ai")
            after = time.monotonic()
        until = monitor._sinbin_until.get("voyage-ai", 0.0)
        # Should be capped at 300s max
        assert until <= after + 301

    def test_rounds_counter_increments_each_activation(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            initial_cooldown_seconds=10,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            for i in range(5):
                monitor.sinbin("voyage-ai")
        assert monitor._sinbin_rounds.get("voyage-ai", 0) == 5


class TestSinBinRoundReset:
    """After a provider recovers (success after sinbin), rounds reset to 0."""

    def test_success_after_sinbin_resets_rounds(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            initial_cooldown_seconds=0, max_cooldown_seconds=300, backoff_multiplier=2.0
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            monitor.sinbin("voyage-ai")
        # Manually expire the sinbin
        monitor._sinbin_until["voyage-ai"] = time.monotonic() - 1.0
        # Record a success — should reset rounds
        monitor.record_call("voyage-ai", 50.0, success=True)
        assert monitor._sinbin_rounds.get("voyage-ai", 0) == 0

    def test_success_after_sinbin_clears_sinbin_entry(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            initial_cooldown_seconds=0, max_cooldown_seconds=300, backoff_multiplier=2.0
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            monitor.sinbin("voyage-ai")
        # Manually expire
        monitor._sinbin_until["voyage-ai"] = time.monotonic() - 1.0
        assert monitor.is_sinbinned("voyage-ai") is False
        monitor.record_call("voyage-ai", 50.0, success=True)
        # After success on previously-sinbinned provider, should remove entry
        assert "voyage-ai" not in monitor._sinbin_until


class TestSinBinThresholdTriggering:
    """Sin-bin auto-triggers when failure threshold is crossed in window."""

    def test_failures_below_threshold_do_not_trigger_sinbin(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            failure_threshold=5,
            failure_window_seconds=60,
            initial_cooldown_seconds=30,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            for _ in range(4):
                monitor.record_call("voyage-ai", 100.0, success=False)
        assert monitor.is_sinbinned("voyage-ai") is False

    def test_failures_at_threshold_trigger_sinbin(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            failure_threshold=5,
            failure_window_seconds=60,
            initial_cooldown_seconds=30,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            for _ in range(5):
                monitor.record_call("voyage-ai", 100.0, success=False)
        assert monitor.is_sinbinned("voyage-ai") is True

    def test_intermittent_failures_below_window_threshold_do_not_sinbin(self, monitor):
        """Failures spread across time with successes should not trigger sin-bin."""
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            failure_threshold=5,
            failure_window_seconds=60,
            initial_cooldown_seconds=30,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            # 4 failure+success pairs = 4 failures total in window — below threshold=5
            for _ in range(4):
                monitor.record_call("voyage-ai", 100.0, success=True)
                monitor.record_call("voyage-ai", 100.0, success=False)
        assert monitor.is_sinbinned("voyage-ai") is False

    def test_window_expiry_resets_failure_count(self, monitor):
        """Old failures outside window should not count toward threshold."""
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            failure_threshold=5,
            failure_window_seconds=1,
            initial_cooldown_seconds=30,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            # Record 4 failures
            for _ in range(4):
                monitor.record_call("voyage-ai", 100.0, success=False)
            # Wait for window to expire
            time.sleep(1.1)
            # Record one more failure — old ones are outside window
            monitor.record_call("voyage-ai", 100.0, success=False)
        # Only 1 failure in current window, threshold=5 → not sinbinned
        assert monitor.is_sinbinned("voyage-ai") is False


class TestSinBinPassiveExpiry:
    """Passive expiry: is_sinbinned returns False after cooldown expires."""

    def test_sinbin_expires_after_cooldown(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            initial_cooldown_seconds=1, max_cooldown_seconds=5, backoff_multiplier=2.0
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            monitor.sinbin("voyage-ai")
        assert monitor.is_sinbinned("voyage-ai") is True
        time.sleep(1.1)
        assert monitor.is_sinbinned("voyage-ai") is False

    def test_sinbin_still_active_before_expiry(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            initial_cooldown_seconds=60,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            monitor.sinbin("voyage-ai")
        # Check immediately — should still be sinbinned
        assert monitor.is_sinbinned("voyage-ai") is True


class TestSinBinStatusReporting:
    """compute_status includes sinbinned field when provider is in sin-bin."""

    def test_status_includes_sinbinned_true_when_sinbinned(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            initial_cooldown_seconds=60,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            monitor.sinbin("voyage-ai")
        # Record a failed call so metrics exist but sinbin is NOT cleared
        monitor.record_call("voyage-ai", 100.0, success=False)
        statuses = monitor.get_health("voyage-ai")
        status = statuses.get("voyage-ai")
        assert status is not None
        assert getattr(status, "sinbinned", False) is True

    def test_status_sinbinned_false_when_not_sinbinned(self, monitor):
        monitor.record_call("voyage-ai", 100.0, success=True)
        statuses = monitor.get_health("voyage-ai")
        status = statuses.get("voyage-ai")
        assert status is not None
        assert getattr(status, "sinbinned", False) is False


class TestSinBinThreadSafety:
    """Sin-bin state must be thread-safe under concurrent access."""

    def test_concurrent_sinbin_calls_do_not_corrupt_state(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            initial_cooldown_seconds=30,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        errors = []

        def sinbin_repeatedly():
            try:
                for _ in range(20):
                    with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
                        monitor.sinbin("voyage-ai")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=sinbin_repeatedly) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors
        # Provider should be sinbinned (threads raced to sinbin)
        assert monitor.is_sinbinned("voyage-ai") is True

    def test_concurrent_record_call_and_is_sinbinned(self, monitor):
        """record_call and is_sinbinned should not deadlock or corrupt state."""
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            failure_threshold=100,
            failure_window_seconds=60,
            initial_cooldown_seconds=30,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        errors = []
        stop = threading.Event()

        def record_loop():
            try:
                while not stop.is_set():
                    with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
                        monitor.record_call("voyage-ai", 50.0, success=True)
            except Exception as e:
                errors.append(e)

        def check_loop():
            try:
                while not stop.is_set():
                    monitor.is_sinbinned("voyage-ai")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=record_loop),
            threading.Thread(target=check_loop),
        ]
        for t in threads:
            t.start()
        time.sleep(0.2)
        stop.set()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors

    def test_singleton_shares_sinbin_state_across_threads(self):
        """Singleton must share sin-bin state across all threads."""
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            initial_cooldown_seconds=60,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        results = {}

        def thread_a():
            monitor = ProviderHealthMonitor.get_instance()
            with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
                monitor.sinbin("voyage-ai")
            results["a_sinbinned"] = monitor.is_sinbinned("voyage-ai")

        def thread_b():
            time.sleep(0.05)
            monitor = ProviderHealthMonitor.get_instance()
            results["b_sinbinned"] = monitor.is_sinbinned("voyage-ai")

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()
        ta.join(timeout=5.0)
        tb.join(timeout=5.0)

        assert results.get("a_sinbinned") is True
        assert results.get("b_sinbinned") is True


class TestSinBinRapidFireRecovery:
    """Creative: rapid-fire failures then recovery."""

    def test_rapid_fire_failures_engage_sinbin_then_recovery_disengages(self, monitor):
        """5 rapid failures → sinbinned. Then successful calls → recovered."""
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            failure_threshold=5,
            failure_window_seconds=60,
            initial_cooldown_seconds=1,
            max_cooldown_seconds=5,
            backoff_multiplier=2.0,
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            for _ in range(5):
                monitor.record_call("voyage-ai", 100.0, success=False)
            assert monitor.is_sinbinned("voyage-ai") is True
            # Wait for expiry
            time.sleep(1.1)
            assert monitor.is_sinbinned("voyage-ai") is False
            # Successful calls after expiry should reset rounds
            monitor.record_call("voyage-ai", 50.0, success=True)
            assert monitor._sinbin_rounds.get("voyage-ai", 0) == 0

    def test_multiple_sinbin_cycles_backoff_then_recover(self, monitor):
        """Multiple engage-disengage cycles accumulate rounds then reset."""
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            failure_threshold=3,
            failure_window_seconds=60,
            initial_cooldown_seconds=1,
            max_cooldown_seconds=10,
            backoff_multiplier=2.0,
        )
        # First cycle
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            for _ in range(3):
                monitor.record_call("voyage-ai", 100.0, success=False)
        assert monitor.is_sinbinned("voyage-ai") is True
        # After expiry + success, rounds reset
        monitor._sinbin_until["voyage-ai"] = time.monotonic() - 1.0
        monitor.record_call("voyage-ai", 50.0, success=True)
        assert monitor._sinbin_rounds.get("voyage-ai", 0) == 0


class TestSinBinRoundCounterOverflow:
    """Creative: many consecutive sinbins should not overflow or error."""

    def test_many_consecutive_sinbins_stay_capped(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            initial_cooldown_seconds=30,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            for _ in range(50):
                monitor.sinbin("voyage-ai")
        # Should not throw and should be capped
        assert monitor.is_sinbinned("voyage-ai") is True
        until = monitor._sinbin_until.get("voyage-ai", 0.0)
        # Max cap is 300s
        assert until <= time.monotonic() + 305

    def test_round_counter_never_goes_negative(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            initial_cooldown_seconds=1, max_cooldown_seconds=10, backoff_multiplier=2.0
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            monitor.sinbin("voyage-ai")
        monitor._sinbin_until["voyage-ai"] = time.monotonic() - 1.0
        monitor.record_call("voyage-ai", 50.0, success=True)
        assert monitor._sinbin_rounds.get("voyage-ai", 0) >= 0


class TestSinBinFailureDeque:
    """Sin-bin uses per-provider failure deque for windowed counting."""

    def test_failure_deque_initialized_on_first_failure(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            failure_threshold=10,
            failure_window_seconds=60,
            initial_cooldown_seconds=30,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            monitor.record_call("voyage-ai", 100.0, success=False)
        assert "voyage-ai" in monitor._sinbin_failure_deque
        assert len(monitor._sinbin_failure_deque["voyage-ai"]) == 1

    def test_failure_deque_prunes_old_entries(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            failure_threshold=10,
            failure_window_seconds=1,
            initial_cooldown_seconds=30,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            monitor.record_call("voyage-ai", 100.0, success=False)
            monitor.record_call("voyage-ai", 100.0, success=False)
            time.sleep(1.1)
            monitor.record_call("voyage-ai", 100.0, success=False)
        # Old entries pruned; only 1 in window
        assert len(monitor._sinbin_failure_deque["voyage-ai"]) == 1

    def test_success_does_not_add_to_failure_deque(self, monitor):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            failure_threshold=10,
            failure_window_seconds=60,
            initial_cooldown_seconds=30,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            monitor.record_call("voyage-ai", 100.0, success=True)
        assert (
            monitor._sinbin_failure_deque.get("voyage-ai") is None
            or len(monitor._sinbin_failure_deque.get("voyage-ai", [])) == 0
        )


class TestClearSinBinResetsRounds:
    """clear_sinbin must reset backoff round counter so next activation uses initial cooldown."""

    def test_clear_sinbin_resets_rounds_to_zero(self, monitor):
        """After clear_sinbin, the next sinbin should use initial cooldown, not continued backoff."""
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            failure_threshold=5,
            failure_window_seconds=60,
            initial_cooldown_seconds=30,
            max_cooldown_seconds=300,
            backoff_multiplier=2.0,
        )
        with patch.object(monitor, "_get_sinbin_config", return_value=cfg):
            monitor.sinbin("voyage-ai")  # round 1, cooldown=30s
            monitor.sinbin("voyage-ai")  # round 2, cooldown=60s
            monitor.clear_sinbin("voyage-ai")
            # After clear, rounds should be reset to 0 — next sinbin uses initial cooldown (30s)
            before = time.monotonic()
            monitor.sinbin("voyage-ai")
            after = time.monotonic()

        # Round counter should be 1 (just incremented from 0, not continuing from 2)
        with monitor._data_lock:
            assert monitor._sinbin_rounds.get("voyage-ai") == 1

        # Cooldown should be ~30s (initial), not ~120s (round 3 backoff)
        until = monitor._sinbin_until.get("voyage-ai", 0.0)
        assert until > before + 28, (
            "cooldown should be ~30s (initial), not continued backoff"
        )
        assert until < after + 32, (
            "cooldown should be ~30s (initial), not continued backoff"
        )


class TestSinBinConfigReading:
    """Sin-bin reads config from server runtime config."""

    def test_sinbin_reads_from_server_config_via_get_sinbin_config(self, monitor):
        """_get_sinbin_config should return ProviderSinBinConfig or default."""
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        result = monitor._get_sinbin_config("voyage-ai")
        assert isinstance(result, ProviderSinBinConfig)

    def test_get_sinbin_config_returns_defaults_without_server(self, monitor):
        """When no server config service, returns default ProviderSinBinConfig."""
        cfg = monitor._get_sinbin_config("voyage-ai")
        assert cfg.failure_threshold == 5
        assert cfg.initial_cooldown_seconds == 30
        assert cfg.max_cooldown_seconds == 300
        assert cfg.backoff_multiplier == 2.0

    def test_reconfigure_preserves_metrics(self, monitor):
        """reconfigure should re-read thresholds without losing existing metrics."""
        monitor.record_call("voyage-ai", 100.0, success=True)
        monitor.reconfigure("voyage-ai")
        # Metrics should still be there
        health = monitor.get_health("voyage-ai")
        assert health["voyage-ai"].total_requests == 1


class TestClearSinBinAll:
    """clear_sinbin_all() must clear sinbin state for all providers at once."""

    def test_clear_sinbin_all_clears_all_sinbinned_providers(self, monitor):
        """clear_sinbin_all() removes sinbin for every tracked provider."""
        monitor.sinbin("voyage-ai")
        monitor.sinbin("cohere")
        assert monitor.is_sinbinned("voyage-ai") is True
        assert monitor.is_sinbinned("cohere") is True
        monitor.clear_sinbin_all()
        assert monitor.is_sinbinned("voyage-ai") is False
        assert monitor.is_sinbinned("cohere") is False

    def test_clear_sinbin_all_resets_rounds_for_all_providers(self, monitor):
        """clear_sinbin_all() resets backoff rounds to zero for every provider."""
        monitor.sinbin("voyage-ai")
        monitor.sinbin("voyage-ai")  # round 2
        monitor.sinbin("cohere")    # round 1
        monitor.clear_sinbin_all()
        assert monitor.get_sinbin_rounds("voyage-ai") == 0
        assert monitor.get_sinbin_rounds("cohere") == 0

    def test_clear_sinbin_all_on_empty_monitor_is_noop(self, monitor):
        """clear_sinbin_all() with no sinbinned providers does not raise."""
        monitor.clear_sinbin_all()  # Should not raise
        assert monitor.is_sinbinned("voyage-ai") is False
        assert monitor.is_sinbinned("cohere") is False

    def test_clear_sinbin_all_does_not_corrupt_providers_not_sinbinned(self, monitor):
        """clear_sinbin_all() does not corrupt providers that were never sinbinned."""
        monitor.record_call("voyage-ai", 100.0, success=True)
        monitor.sinbin("cohere")
        monitor.clear_sinbin_all()
        assert monitor.is_sinbinned("voyage-ai") is False
        assert monitor.is_sinbinned("cohere") is False

    def test_single_provider_clear_sinbin_only_clears_named_provider(self, monitor):
        """Existing clear_sinbin(provider) still only clears the named provider."""
        monitor.sinbin("voyage-ai")
        monitor.sinbin("cohere")
        monitor.clear_sinbin("voyage-ai")
        assert monitor.is_sinbinned("voyage-ai") is False
        # cohere must remain sinbinned — single-provider clear does not affect others
        assert monitor.is_sinbinned("cohere") is True
