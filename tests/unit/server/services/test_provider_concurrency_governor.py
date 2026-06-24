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


class TestCurrentK:
    def test_current_k_returns_per_lane_aimd_k(self):
        """current_k exposes each lane's live AIMD K for observability."""
        g = _make_governor(k=12)
        ck = g.current_k
        assert set(ck.keys()) == set(LANES)
        for lane in LANES:
            assert ck[lane] == g.aimd(lane).k == 12

    def test_current_k_reflects_aimd_decrease(self):
        """A 429-driven multiplicative decrease is visible via current_k."""
        g = _make_governor(k=16)
        g.aimd("voyage:embed").record(success=False)  # K -> 8 (floor)
        assert g.current_k["voyage:embed"] == 8
        # Other lanes unaffected (lane independence).
        assert g.current_k["cohere:embed"] == 16


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


# ---------------------------------------------------------------------------
# coalesce_k_min / coalesce_k_max config seeds (Story #1079 anti-orphan fix)
# ---------------------------------------------------------------------------


class _FakeConfig:
    """Minimal stand-in for ServerConfig exposing the fields the governor reads."""

    def __init__(self, *, k_min=8, k_max=32, max_concurrency=8):
        self.coalesce_k_min = k_min
        self.coalesce_k_max = k_max
        self.query_provider_max_concurrency = max_concurrency


class _FakeConfigService:
    def __init__(self, cfg):
        self._cfg = cfg

    def get_config(self):
        return self._cfg


def _patch_config(cfg):
    """Patch get_config_service to return a service yielding ``cfg``."""
    return patch(
        "code_indexer.server.services.config_service.get_config_service",
        return_value=_FakeConfigService(cfg),
    )


class TestCoalesceKBoundsSeed:
    def test_governor_seeds_aimd_ceiling_from_config(self):
        """coalesce_k_max=64 lets a lane's AIMD grow above the default 32 up to 64."""
        from code_indexer.server.services.aimd_controller import SUCCESS_THRESHOLD

        cfg = _FakeConfig(k_min=8, k_max=64, max_concurrency=8)
        with _patch_config(cfg):
            g = ProviderConcurrencyGovernor()  # seed from config
        aimd = g.aimd("voyage:embed")
        # Drive far more successes than needed to reach the configured ceiling.
        for _ in range(SUCCESS_THRESHOLD * (64 - 8) + SUCCESS_THRESHOLD * 5):
            aimd.record(success=True)
        assert aimd.k == 64, "AIMD must grow up to the config-seeded k_max=64"

    def test_governor_seeds_limiter_clamp_from_config(self):
        """The per-lane limiter clamp bound becomes [k_min, k_max] from config."""
        cfg = _FakeConfig(k_min=8, k_max=64, max_concurrency=8)
        with _patch_config(cfg):
            g = ProviderConcurrencyGovernor()
        limiter = g._limiters["voyage:embed"]
        limiter.set_limit(64)
        assert limiter.limit == 64, "limiter clamp ceiling must be the config k_max=64"
        limiter.set_limit(999)
        assert limiter.limit == 64

    def test_initial_seed_clamps_into_config_bounds(self):
        """The initial K seed clamps into [k_min, k_max], not [8, 32]."""
        # max_concurrency=50 is above default K_MAX (32) but within [8, 64].
        cfg = _FakeConfig(k_min=8, k_max=64, max_concurrency=50)
        with _patch_config(cfg):
            g = ProviderConcurrencyGovernor()
        for lane in LANES:
            assert g.aimd(lane).k == 50, (
                f"lane {lane} initial K must clamp into the configured [8, 64]"
            )

    def test_invalid_k_min_greater_than_k_max_falls_back_to_defaults(self, caplog):
        """k_min > k_max is invalid -> fall back to 8/32 with a logged WARNING."""
        import logging

        cfg = _FakeConfig(k_min=40, k_max=10, max_concurrency=8)
        logger_name = "code_indexer.server.services.provider_concurrency_governor"
        with _patch_config(cfg):
            with caplog.at_level(logging.WARNING, logger=logger_name):
                g = ProviderConcurrencyGovernor()
        # Defaults applied: floor 8, ceiling 32.
        limiter = g._limiters["voyage:embed"]
        limiter.set_limit(999)
        assert limiter.limit == 32, "invalid config must fall back to default K_MAX=32"
        assert any(
            r.name == logger_name and r.levelno == logging.WARNING
            for r in caplog.records
        ), "invalid K bounds must emit a WARNING"

    def test_invalid_k_min_below_floor_falls_back(self):
        """k_min < 8 is invalid -> fall back to default 8/32."""
        cfg = _FakeConfig(k_min=2, k_max=64, max_concurrency=8)
        with _patch_config(cfg):
            g = ProviderConcurrencyGovernor()
        limiter = g._limiters["voyage:embed"]
        limiter.set_limit(999)
        assert limiter.limit == 32, "k_min<8 invalid -> default ceiling 32"
        limiter.set_limit(0)
        assert limiter.limit == 8, "k_min<8 invalid -> default floor 8"

    def test_missing_fields_fall_back_to_defaults(self):
        """Config without the coalesce_k_* fields -> default 8/32, no crash."""

        class _BareConfig:
            query_provider_max_concurrency = 8

        with _patch_config(_BareConfig()):
            g = ProviderConcurrencyGovernor()
        limiter = g._limiters["voyage:embed"]
        limiter.set_limit(999)
        assert limiter.limit == 32

    def test_explicit_max_concurrency_preserves_default_bounds(self):
        """Direct construction (tests) with max_concurrency keeps default [8, 32]."""
        g = ProviderConcurrencyGovernor(max_concurrency=20)
        limiter = g._limiters["voyage:embed"]
        limiter.set_limit(999)
        assert limiter.limit == 32, "explicit construction must keep default K_MAX=32"


# ---------------------------------------------------------------------------
# Story #1165 — Per-Worker Embedding Governor Concurrency Scaling
#
# query_provider_max_concurrency is the PER-NODE total provider-concurrency
# budget. The governor divides it by config.workers at construction
# (auto-seed path only) so combined embedding pressure across all workers
# on a node stays within the configured budget.
# ---------------------------------------------------------------------------


class _FakeConfigWithWorkers:
    """ServerConfig stub exposing the fields the governor reads for #1165."""

    def __init__(
        self,
        *,
        k_min: int = 8,
        k_max: int = 32,
        max_concurrency: int = 8,
        workers: int = 1,
    ):
        self.coalesce_k_min = k_min
        self.coalesce_k_max = k_max
        self.query_provider_max_concurrency = max_concurrency
        self.workers = workers


def _patch_config_workers(cfg):
    """Patch get_config_service AND the applied-worker-count resolver.

    Story #1197 rerouted _read_config_workers() through the file-based
    applied_worker_count resolver instead of get_config_service().workers.
    Both patches are required so TestWorkerConcurrencyScaling tests inject
    the desired worker count into the governor.
    """
    workers = getattr(cfg, "workers", 1)
    # Ensure max(1, workers) discipline matches the real resolver behaviour.
    applied_workers = max(1, workers) if isinstance(workers, int) else 1
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=_FakeConfigService(cfg),
        )
    )
    stack.enter_context(
        patch(
            # The governor imports via `from ... import get_applied_worker_count`
            # inside a try block (local import), so we must patch the name in
            # the source module, not in the governor's namespace.
            "code_indexer.server.services.applied_worker_count.get_applied_worker_count",
            return_value=applied_workers,
        )
    )
    return stack


class TestWorkerConcurrencyScaling:
    """Story #1165: per-worker seed = per_node_seed // workers (floor k_min)."""

    def setup_method(self):
        ProviderConcurrencyGovernor.reset_instance()

    def teardown_method(self):
        ProviderConcurrencyGovernor.reset_instance()

    def test_ac1_four_workers_divides_seed(self):
        """AC1: workers=4, max_concurrency=32 -> each worker gets seed 8 (32//4)."""
        cfg = _FakeConfigWithWorkers(k_min=8, k_max=32, max_concurrency=32, workers=4)
        with _patch_config_workers(cfg):
            g = ProviderConcurrencyGovernor()  # auto-seed path (no explicit arg)
        for lane in LANES:
            assert g.current_k[lane] == 8, (
                f"AC1 failed for lane {lane}: expected 8 (32//4), got {g.current_k[lane]}"
            )

    def test_ac2_one_worker_unchanged(self):
        """AC2: workers=1, max_concurrency=32 -> seed 32 (identical to today)."""
        cfg = _FakeConfigWithWorkers(k_min=8, k_max=32, max_concurrency=32, workers=1)
        with _patch_config_workers(cfg):
            g = ProviderConcurrencyGovernor()
        for lane in LANES:
            assert g.current_k[lane] == 32, (
                f"AC2 failed for lane {lane}: workers=1 must be identical to today, "
                f"expected 32, got {g.current_k[lane]}"
            )

    def test_ac3_over_division_floor_is_k_min(self):
        """AC3: workers=4, max_concurrency=8 -> 8//4=2, floored to k_min=8."""
        cfg = _FakeConfigWithWorkers(k_min=8, k_max=32, max_concurrency=8, workers=4)
        with _patch_config_workers(cfg):
            g = ProviderConcurrencyGovernor()
        for lane in LANES:
            assert g.current_k[lane] == 8, (
                f"AC3 failed for lane {lane}: 8//4=2 must floor to k_min=8, "
                f"got {g.current_k[lane]}"
            )

    def test_misconfig_workers_zero_treated_as_one(self):
        """workers=0 -> _read_config_workers returns 1 -> full per-node seed."""
        cfg = _FakeConfigWithWorkers(k_min=8, k_max=32, max_concurrency=32, workers=0)
        with _patch_config_workers(cfg):
            g = ProviderConcurrencyGovernor()
        # workers=0 invalid -> treated as 1 -> seed = 32//1 clamped to [8,32]=32
        for lane in LANES:
            assert g.current_k[lane] == 32, (
                f"misconfig workers=0 failed for lane {lane}: expected 32, "
                f"got {g.current_k[lane]}"
            )

    def test_misconfig_workers_negative_treated_as_one(self):
        """workers=-3 -> _read_config_workers returns 1 -> full per-node seed."""
        cfg = _FakeConfigWithWorkers(k_min=8, k_max=32, max_concurrency=32, workers=-3)
        with _patch_config_workers(cfg):
            g = ProviderConcurrencyGovernor()
        for lane in LANES:
            assert g.current_k[lane] == 32, (
                f"misconfig workers=-3 failed for lane {lane}: expected 32, "
                f"got {g.current_k[lane]}"
            )

    def test_explicit_construction_not_divided_by_workers(self):
        """Explicit max_concurrency arg bypasses worker-division (tests unaffected)."""
        cfg = _FakeConfigWithWorkers(k_min=8, k_max=32, max_concurrency=32, workers=4)
        with _patch_config_workers(cfg):
            # Explicit construction: max_concurrency=20 is NOT divided by workers.
            g = ProviderConcurrencyGovernor(max_concurrency=20)
        for lane in LANES:
            assert g.current_k[lane] == 20, (
                f"explicit construction must not be divided by workers: "
                f"expected 20, got {g.current_k[lane]}"
            )

    def test_read_config_workers_fallback_when_config_raises(self):
        """_read_config_workers returns 1 (no division) when config raises."""
        with patch(
            "code_indexer.server.services.config_service.get_config_service",
            side_effect=RuntimeError("config not initialized"),
        ):
            g = ProviderConcurrencyGovernor()  # auto-seed path
        # Both reads fail: concurrency=K_MIN=8, workers=1; seed=8//1=8 clamped=8.
        for lane in LANES:
            assert g.current_k[lane] == 8, (
                f"fallback failed for lane {lane}: expected K_MIN=8, "
                f"got {g.current_k[lane]}"
            )

    def test_read_config_workers_fallback_when_field_absent(self):
        """_read_config_workers returns 1 when the 'workers' field is absent."""

        class _ConfigNoWorkers:
            query_provider_max_concurrency = 32
            coalesce_k_min = 8
            coalesce_k_max = 32
            # no 'workers' attribute

        with _patch_config_workers(_ConfigNoWorkers()):
            g = ProviderConcurrencyGovernor()
        # workers absent -> _read_config_workers returns 1 -> seed=32//1=32
        for lane in LANES:
            assert g.current_k[lane] == 32, (
                f"absent workers field failed for lane {lane}: expected 32, "
                f"got {g.current_k[lane]}"
            )
