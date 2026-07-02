"""Tests for temporal circuit breaker health tracking (Story #635).

Covers:
- make_temporal_health_key() key construction and prefix stripping
- is_temporal_provider_healthy() default-true when monitor unavailable
- filter_healthy_temporal_providers() health-gating logic
- record_temporal_success() / record_temporal_failure() no-crash contracts
- TEMPORAL_HEALTH_PREFIX constant value
"""

from unittest.mock import patch

from code_indexer.services.temporal.temporal_health import (
    TEMPORAL_HEALTH_PREFIX,
    make_temporal_health_key,
    is_temporal_provider_healthy,
    filter_healthy_temporal_providers,
    record_temporal_success,
    record_temporal_failure,
)
from code_indexer.services.provider_health_monitor import ProviderHealthMonitor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_health_monitor():
    ProviderHealthMonitor.reset_instance()


# ---------------------------------------------------------------------------
# make_temporal_health_key
# ---------------------------------------------------------------------------


def test_make_temporal_health_key_from_model():
    """Plain model name gets prefixed with 'temporal:'."""
    key = make_temporal_health_key("voyage-code-3")
    assert key == "temporal:voyage-code-3"


def test_make_temporal_health_key_strips_collection_prefix():
    """Collection name with 'code-indexer-temporal-' prefix is stripped."""
    key = make_temporal_health_key("code-indexer-temporal-voyage_code_3")
    assert key == "temporal:voyage_code_3"


# ---------------------------------------------------------------------------
# TEMPORAL_HEALTH_PREFIX constant
# ---------------------------------------------------------------------------


def test_temporal_health_prefix_constant():
    """TEMPORAL_HEALTH_PREFIX must equal 'temporal:'."""
    assert TEMPORAL_HEALTH_PREFIX == "temporal:"


# ---------------------------------------------------------------------------
# is_temporal_provider_healthy
# ---------------------------------------------------------------------------


def test_is_temporal_provider_healthy_default_true():
    """Returns True when health monitor raises an exception (best-effort)."""
    with patch(
        "code_indexer.services.provider_health_monitor.ProviderHealthMonitor"
    ) as mock_cls:
        mock_cls.get_instance.side_effect = Exception("monitor unavailable")
        result = is_temporal_provider_healthy("voyage-code-3")
    assert result is True


def test_is_temporal_provider_healthy_returns_true_when_healthy(tmp_path):
    """Returns True when monitor reports status 'healthy'."""
    _reset_health_monitor()
    ProviderHealthMonitor.get_instance()  # ensure initialized
    # No calls recorded → empty status → 'healthy'
    result = is_temporal_provider_healthy("voyage-code-3")
    assert result is True
    _reset_health_monitor()


def test_is_temporal_provider_healthy_returns_false_when_down(tmp_path):
    """Returns False when monitor reports status 'down' (circuit open)."""
    _reset_health_monitor()
    monitor = ProviderHealthMonitor.get_instance()
    key = make_temporal_health_key("voyage-code-3")
    # Record enough consecutive failures to trip the circuit breaker
    # DEFAULT_DOWN_CONSECUTIVE_FAILURES = 5
    for _ in range(5):
        monitor.record_call(key, latency_ms=100.0, success=False)
    result = is_temporal_provider_healthy("voyage-code-3")
    assert result is False
    _reset_health_monitor()


# ---------------------------------------------------------------------------
# filter_healthy_temporal_providers
# ---------------------------------------------------------------------------


def test_filter_healthy_all_healthy():
    """All collections pass through when all providers are healthy."""
    collections = [("coll-a", "hint-a"), ("coll-b", "hint-b")]
    with patch(
        "code_indexer.services.temporal.temporal_health.is_temporal_provider_healthy",
        return_value=True,
    ):
        healthy, skipped = filter_healthy_temporal_providers(collections)
    assert healthy == collections
    assert skipped == []


def test_filter_healthy_one_unhealthy():
    """Unhealthy collection is filtered out; healthy collection passes through."""
    collections = [("coll-healthy", "h1"), ("coll-sick", "h2")]

    def _is_healthy(name):
        return name != "coll-sick"

    with patch(
        "code_indexer.services.temporal.temporal_health.is_temporal_provider_healthy",
        side_effect=_is_healthy,
    ):
        healthy, skipped = filter_healthy_temporal_providers(collections)

    assert healthy == [("coll-healthy", "h1")]
    assert skipped == [("coll-sick", "h2")]


def test_filter_healthy_all_unhealthy_attempts_anyway():
    """When ALL providers are unhealthy, returns all in healthy list with empty skipped.

    This prevents a total blackout: if all circuit breakers are open,
    attempt the query anyway rather than returning nothing.
    """
    collections = [("coll-a", "h1"), ("coll-b", "h2")]
    with patch(
        "code_indexer.services.temporal.temporal_health.is_temporal_provider_healthy",
        return_value=False,
    ):
        healthy, skipped = filter_healthy_temporal_providers(collections)

    assert healthy == collections
    assert skipped == []


# ---------------------------------------------------------------------------
# record_temporal_success / record_temporal_failure — no-crash contract
# ---------------------------------------------------------------------------


def test_record_temporal_success_no_crash():
    """record_temporal_success must not raise even if monitor is unavailable."""
    with patch(
        "code_indexer.services.provider_health_monitor.ProviderHealthMonitor"
    ) as mock_cls:
        mock_cls.get_instance.side_effect = Exception("unavailable")
        # Must not raise
        record_temporal_success("voyage-code-3", latency_ms=42.0)


def test_record_temporal_failure_no_crash():
    """record_temporal_failure must not raise even if monitor is unavailable."""
    with patch(
        "code_indexer.services.provider_health_monitor.ProviderHealthMonitor"
    ) as mock_cls:
        mock_cls.get_instance.side_effect = Exception("unavailable")
        # Must not raise
        record_temporal_failure("voyage-code-3", latency_ms=99.0)


def test_record_temporal_success_records_to_monitor():
    """record_temporal_success calls record_call with success=True."""
    _reset_health_monitor()
    record_temporal_success("voyage-code-3", latency_ms=55.0)
    key = make_temporal_health_key("voyage-code-3")
    health = ProviderHealthMonitor.get_instance().get_health(key)
    assert health[key].successful_requests == 1
    assert health[key].failed_requests == 0
    _reset_health_monitor()


def test_record_temporal_failure_records_to_monitor():
    """record_temporal_failure calls record_call with success=False."""
    _reset_health_monitor()
    record_temporal_failure("voyage-code-3", latency_ms=200.0)
    key = make_temporal_health_key("voyage-code-3")
    health = ProviderHealthMonitor.get_instance().get_health(key)
    assert health[key].failed_requests == 1
    assert health[key].successful_requests == 0
    _reset_health_monitor()
