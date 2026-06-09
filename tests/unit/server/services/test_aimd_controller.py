"""Unit tests for AimdController (Story #1079 Phase B).

AimdController implements per-lane adaptive K: additive-increase on sustained
success, multiplicative-decrease on a 429. It drives a ResizableLimiter via
set_limit(K). A post-decrease cooldown prevents immediate re-grow.

Determinism: the controller accepts an injectable ``time_fn`` so cooldown
behaviour is tested without real sleeping (NEVER time.sleep in production).
"""

from typing import List

from code_indexer.server.services.aimd_controller import (
    COOLDOWN_SECONDS,
    SUCCESS_THRESHOLD,
    AimdController,
)
from code_indexer.server.services.resizable_limiter import (
    K_MAX,
    K_MIN,
    ResizableLimiter,
)


class _FakeClock:
    """Controllable monotonic clock for deterministic cooldown tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make(limiter_initial: int = K_MIN):
    clock = _FakeClock()
    limiter = ResizableLimiter(initial=limiter_initial)
    aimd = AimdController(limiter=limiter, time_fn=clock)
    return aimd, limiter, clock


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


class TestAimdInitial:
    def test_starts_at_k_min(self):
        aimd, _, _ = _make()
        assert aimd.k == K_MIN  # 8


# ---------------------------------------------------------------------------
# Additive increase
# ---------------------------------------------------------------------------


class TestAimdIncrease:
    def test_success_below_threshold_does_not_grow(self):
        aimd, limiter, _ = _make()
        for _ in range(SUCCESS_THRESHOLD - 1):
            aimd.record(success=True)
        assert aimd.k == K_MIN
        assert limiter.limit == K_MIN

    def test_threshold_successes_grow_by_one(self):
        aimd, limiter, _ = _make()
        for _ in range(SUCCESS_THRESHOLD):
            aimd.record(success=True)
        assert aimd.k == K_MIN + 1
        assert limiter.limit == K_MIN + 1

    def test_grows_to_ceiling_never_beyond(self):
        aimd, limiter, _ = _make()
        # Drive far more successes than needed to reach K_MAX
        for _ in range(SUCCESS_THRESHOLD * (K_MAX - K_MIN) + SUCCESS_THRESHOLD * 5):
            aimd.record(success=True)
        assert aimd.k == K_MAX  # 32
        assert limiter.limit == K_MAX

    def test_record_drives_limiter_set_limit_calls(self):
        """record(success) past threshold calls limiter.set_limit(K)."""
        captured: List[int] = []
        clock = _FakeClock()
        limiter = ResizableLimiter(initial=K_MIN)
        orig_set = limiter.set_limit

        def spy(new_limit: int) -> None:
            captured.append(new_limit)
            orig_set(new_limit)

        limiter.set_limit = spy  # type: ignore[method-assign]
        aimd = AimdController(limiter=limiter, time_fn=clock)
        for _ in range(SUCCESS_THRESHOLD):
            aimd.record(success=True)
        assert captured == [K_MIN + 1]


# ---------------------------------------------------------------------------
# Multiplicative decrease
# ---------------------------------------------------------------------------


class TestAimdDecrease:
    def test_429_halves_k(self):
        aimd, limiter, _ = _make(limiter_initial=K_MAX)
        aimd._k = K_MAX  # start high to observe the halving
        aimd.record(success=False)  # 429
        assert aimd.k == K_MAX // 2  # 16
        assert limiter.limit == K_MAX // 2

    def test_429_never_below_floor(self):
        aimd, limiter, _ = _make()
        assert aimd.k == K_MIN
        aimd.record(success=False)  # 429 at floor
        assert aimd.k == K_MIN  # stays at floor, never below
        assert limiter.limit == K_MIN

    def test_multiple_429_drive_32_to_16_to_8_to_8(self):
        aimd, limiter, clock = _make(limiter_initial=K_MAX)
        aimd._k = K_MAX
        aimd.record(success=False)
        assert aimd.k == 16
        # advance past cooldown so a subsequent 429 is purely a decrease test
        clock.advance(COOLDOWN_SECONDS + 1)
        aimd.record(success=False)
        assert aimd.k == 8
        clock.advance(COOLDOWN_SECONDS + 1)
        aimd.record(success=False)
        assert aimd.k == 8  # floor, never below
        assert limiter.limit == 8


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


class TestAimdCooldown:
    def test_cooldown_blocks_immediate_regrow_after_cut(self):
        aimd, limiter, clock = _make(limiter_initial=K_MAX)
        aimd._k = 20
        aimd.record(success=False)  # cut -> 10, cooldown starts
        assert aimd.k == 10

        # Successes during cooldown must NOT grow K
        for _ in range(SUCCESS_THRESHOLD * 3):
            aimd.record(success=True)
        assert aimd.k == 10, "cooldown must block re-grow immediately after a cut"

    def test_regrow_resumes_after_cooldown_expires(self):
        aimd, limiter, clock = _make(limiter_initial=K_MAX)
        aimd._k = 20
        aimd.record(success=False)  # cut -> 10
        assert aimd.k == 10

        # Advance past the cooldown window
        clock.advance(COOLDOWN_SECONDS + 0.001)
        for _ in range(SUCCESS_THRESHOLD):
            aimd.record(success=True)
        assert aimd.k == 11, "after cooldown expires, additive increase resumes"

    def test_success_during_cooldown_does_not_count_toward_run(self):
        """Successes that arrive during cooldown are dropped, not banked."""
        aimd, limiter, clock = _make(limiter_initial=K_MAX)
        aimd._k = 20
        aimd.record(success=False)  # cut -> 10, cooldown
        # SUCCESS_THRESHOLD-1 successes during cooldown (would-be banked)
        for _ in range(SUCCESS_THRESHOLD - 1):
            aimd.record(success=True)
        # Cooldown expires; a single success must NOT immediately tip a grow
        clock.advance(COOLDOWN_SECONDS + 0.001)
        aimd.record(success=True)
        assert aimd.k == 10, "in-cooldown successes must not be banked"
        # Now a full fresh run grows
        for _ in range(SUCCESS_THRESHOLD - 1):
            aimd.record(success=True)
        assert aimd.k == 11


# ---------------------------------------------------------------------------
# Configurable floor/ceiling (Story #1079 anti-orphan: coalesce_k_min/k_max)
# ---------------------------------------------------------------------------


class TestAimdConfigurableBounds:
    def test_default_ceiling_is_k_max(self):
        """No k_max arg -> additive increase caps at the module default K_MAX (32)."""
        aimd, limiter, _ = _make()
        for _ in range(SUCCESS_THRESHOLD * (K_MAX - K_MIN) + SUCCESS_THRESHOLD * 5):
            aimd.record(success=True)
        assert aimd.k == K_MAX
        assert limiter.limit == K_MAX

    def test_custom_ceiling_grows_above_k_max(self):
        """k_max=64 (with a matching limiter) lets K grow past 32 up to 64."""
        clock = _FakeClock()
        limiter = ResizableLimiter(initial=8, k_min=8, k_max=64)
        aimd = AimdController(limiter=limiter, time_fn=clock, k_min=8, k_max=64)
        # Drive well past the would-be K_MAX (32) ceiling toward 64.
        for _ in range(SUCCESS_THRESHOLD * (64 - 8) + SUCCESS_THRESHOLD * 5):
            aimd.record(success=True)
        assert aimd.k == 64, "custom ceiling must let K grow above the default K_MAX"
        assert limiter.limit == 64

    def test_custom_floor_caps_multiplicative_decrease(self):
        """A custom k_min raises the decrease floor above K_MIN."""
        clock = _FakeClock()
        limiter = ResizableLimiter(initial=40, k_min=10, k_max=64)
        aimd = AimdController(limiter=limiter, time_fn=clock, k_min=10, k_max=64)
        aimd._k = 12
        aimd.record(success=False)  # 12 // 2 = 6, but floor is 10
        assert aimd.k == 10, "decrease must not drop below the configured floor"
        assert limiter.limit == 10


# ---------------------------------------------------------------------------
# Observability: structured WARNING on multiplicative decrease (Phase E)
# ---------------------------------------------------------------------------

_AIMD_LOGGER = "code_indexer.server.services.aimd_controller"


class TestDecreaseLogging:
    def test_decrease_emits_structured_warning(self, caplog):
        """A 429-driven decrease logs a WARNING with structured old_k/new_k."""
        import logging

        aimd, _limiter, _clock = _make(limiter_initial=16)
        with caplog.at_level(logging.WARNING, logger=_AIMD_LOGGER):
            aimd.record(success=False)  # 16 -> 8
        assert aimd.k == 8
        decrease_records = [
            r
            for r in caplog.records
            if r.name == _AIMD_LOGGER and r.levelno == logging.WARNING
        ]
        assert decrease_records, "decrease must emit a WARNING on the aimd logger"
        record = decrease_records[0]
        assert record.old_k == 16
        assert record.new_k == 8

    def test_no_log_when_already_at_floor(self, caplog):
        """At the K_MIN floor a 429 leaves K unchanged and emits no decrease log."""
        import logging

        aimd, _limiter, _clock = _make(limiter_initial=K_MIN)
        with caplog.at_level(logging.WARNING, logger=_AIMD_LOGGER):
            aimd.record(success=False)  # already at floor, K stays K_MIN
        assert aimd.k == K_MIN
        assert not [
            r
            for r in caplog.records
            if r.name == _AIMD_LOGGER and r.levelno == logging.WARNING
        ], "no decrease log when K is unchanged at the floor"
