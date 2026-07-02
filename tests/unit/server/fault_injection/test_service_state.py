"""
Tests for FaultInjectionService counters, history ring buffer, reset,
seed control, injectable RNG, and concurrent mutation safety.

Story #746 — Scenarios 14, 16, 23, 29, 33, 34.

TDD: tests written BEFORE production code.
"""

import queue
import random
import threading
import time

import pytest

from code_indexer.server.fault_injection.fault_profile import FaultProfile
from code_indexer.server.fault_injection.fault_injection_service import (
    FaultInjectionService,
    select_outcome,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SEED_MAIN = 42
SEED_ALT = 99
TARGET = "provider-a.test"
URL = f"https://{TARGET}/v1/embed"
FAULT_TYPE = "http_error"
DEFAULT_ERROR_CODES = (429,)
DEFAULT_ERROR_RATE = 1.0
HALF_RATE = 0.5

COUNTER_INCREMENTS = 5
COUNTER_THREAD_COUNT = 500
COUNTER_THREAD_JOIN_SEC = 5.0

HISTORY_CAPACITY = 100
HISTORY_OVERFLOW = 50

SEED_SEQUENCE_LEN = 30
RNG_SEQUENCE_LEN = 30

CONCURRENCY_MATCH_WORKERS = 100  # 100 workers × 10 calls each = 1000 matches
CONCURRENCY_CALLS_PER_WORKER = 10
CONCURRENCY_MUTATE_WORKERS = 50
CONCURRENCY_TIMEOUT_SEC = 10


def _make_service(enabled: bool = True) -> FaultInjectionService:
    return FaultInjectionService(enabled=enabled, rng=random.Random(SEED_MAIN))


def _profile(
    target: str = TARGET,
    error_rate: float = DEFAULT_ERROR_RATE,
    **kwargs,
) -> FaultProfile:
    return FaultProfile(
        target=target,
        error_rate=error_rate,
        error_codes=list(DEFAULT_ERROR_CODES),
        **kwargs,
    )


@pytest.fixture()
def svc() -> FaultInjectionService:
    return _make_service()


# ===========================================================================
# Counters (Scenario 33)
# ===========================================================================


def test_counter_increments_on_record(svc):
    svc.record_injection(TARGET, FAULT_TYPE, "corr-001")
    assert svc.get_counters().get((TARGET, FAULT_TYPE), 0) == 1


def test_counter_increments_multiple_times(svc):
    for _ in range(COUNTER_INCREMENTS):
        svc.record_injection(TARGET, FAULT_TYPE, "corr-x")
    assert svc.get_counters()[(TARGET, FAULT_TYPE)] == COUNTER_INCREMENTS


def test_reset_clears_counters(svc):
    svc.record_injection(TARGET, FAULT_TYPE, "corr-y")
    svc.reset()
    assert svc.get_counters() == {}


def test_concurrent_increments_are_atomic(svc):
    """500 concurrent increments must produce exactly 500 (Scenario 33)."""
    threads = [
        threading.Thread(target=lambda: svc.record_injection(TARGET, FAULT_TYPE, "c"))
        for _ in range(COUNTER_THREAD_COUNT)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=COUNTER_THREAD_JOIN_SEC)
    assert svc.get_counters()[(TARGET, FAULT_TYPE)] == COUNTER_THREAD_COUNT


# ===========================================================================
# History ring buffer (Scenario 34)
# ===========================================================================


def test_empty_history_on_init(svc):
    assert svc.get_history() == []


def test_events_appear_in_history(svc):
    svc.record_injection(TARGET, FAULT_TYPE, "corr-1")
    history = svc.get_history()
    assert len(history) == 1
    assert history[0].target == TARGET
    assert history[0].fault_type == FAULT_TYPE


def test_buffer_bounded_at_capacity(svc):
    for i in range(HISTORY_CAPACITY + HISTORY_OVERFLOW):
        svc.record_injection(TARGET, FAULT_TYPE, f"corr-{i}")
    assert len(svc.get_history()) == HISTORY_CAPACITY


def test_oldest_evicted_first(svc):
    """After capacity+overflow events, only the HISTORY_CAPACITY most recent remain."""
    total = HISTORY_CAPACITY + HISTORY_OVERFLOW
    for i in range(total):
        svc.record_injection(TARGET, FAULT_TYPE, f"corr-{i}")
    history = svc.get_history()
    assert history[0].correlation_id == f"corr-{HISTORY_OVERFLOW}"
    assert history[-1].correlation_id == f"corr-{total - 1}"


def test_reset_clears_history(svc):
    svc.record_injection(TARGET, FAULT_TYPE, "corr-z")
    svc.reset()
    assert svc.get_history() == []


# ===========================================================================
# Reset clears all state (Scenario 16)
# ===========================================================================


def test_reset_clears_profiles_counters_and_history(svc):
    svc.register_profile(TARGET, _profile())
    svc.record_injection(TARGET, FAULT_TYPE, "c1")
    svc.reset()
    assert svc.get_all_profiles() == {}
    assert svc.get_counters() == {}
    assert svc.get_history() == []


# ===========================================================================
# Seed control (Scenario 14)
# ===========================================================================


def test_seed_resets_rng_for_reproducibility(svc):
    """Two runs seeded identically produce identical outcome sequences."""
    profile = _profile(error_rate=HALF_RATE)
    svc.register_profile(TARGET, profile)

    svc.set_seed(SEED_ALT)
    seq_a = [select_outcome(profile, svc.rng) for _ in range(SEED_SEQUENCE_LEN)]

    svc.set_seed(SEED_ALT)
    seq_b = [select_outcome(profile, svc.rng) for _ in range(SEED_SEQUENCE_LEN)]

    assert seq_a == seq_b


# ===========================================================================
# Injectable RNG (Scenario 23)
# ===========================================================================


def test_injectable_rng_produces_deterministic_behaviour():
    """Two services with same seed give reproducible select_outcome sequences."""
    profile = _profile(error_rate=HALF_RATE)

    svc_a = FaultInjectionService(enabled=True, rng=random.Random(SEED_MAIN))
    svc_b = FaultInjectionService(enabled=True, rng=random.Random(SEED_MAIN))

    seq_a = [select_outcome(profile, svc_a.rng) for _ in range(RNG_SEQUENCE_LEN)]
    seq_b = [select_outcome(profile, svc_b.rng) for _ in range(RNG_SEQUENCE_LEN)]
    assert seq_a == seq_b


# ===========================================================================
# Concurrent mutations under load (Scenario 29)
# ===========================================================================


def test_concurrent_mutations_do_not_crash_or_deadlock():
    """1000 match calls + 50 PUT/DELETE concurrent mutations must not crash."""
    svc = _make_service()
    svc.register_profile(TARGET, _profile())

    error_queue: queue.Queue = queue.Queue()

    def _match_worker():
        for _ in range(CONCURRENCY_CALLS_PER_WORKER):
            try:
                svc.match_profile_snapshot(URL)
            except Exception as exc:  # noqa: BLE001
                error_queue.put(exc)

    def _mutate_worker():
        try:
            svc.register_profile(TARGET, _profile(error_rate=HALF_RATE))
            svc.remove_profile(TARGET)
            svc.register_profile(TARGET, _profile())
        except Exception as exc:  # noqa: BLE001
            error_queue.put(exc)

    threads = []
    for _ in range(CONCURRENCY_MATCH_WORKERS):
        threads.append(threading.Thread(target=_match_worker))
    for _ in range(CONCURRENCY_MUTATE_WORKERS):
        threads.append(threading.Thread(target=_mutate_worker))

    deadline = time.monotonic() + CONCURRENCY_TIMEOUT_SEC
    for t in threads:
        t.start()
    for t in threads:
        remaining = max(0.0, deadline - time.monotonic())
        t.join(timeout=remaining)

    collected_errors = []
    while not error_queue.empty():
        collected_errors.append(error_queue.get_nowait())

    assert not collected_errors, f"Concurrent mutations raised: {collected_errors}"
    assert all(not t.is_alive() for t in threads), (
        "Deadlock detected — threads still alive"
    )


# ===========================================================================
# Logs DB integration (Scenario 32)
# ===========================================================================


def test_record_injection_logs_to_fault_injection_logger(caplog):
    """Scenario 32: record_injection emits exactly one INFO log to 'fault_injection' logger
    with structured extra fields target, fault_type, and correlation_id on the record.
    """
    import logging as _logging

    svc = _make_service()
    corr_id = "corr-log-test"
    with caplog.at_level(_logging.INFO, logger="fault_injection"):
        svc.record_injection(TARGET, FAULT_TYPE, corr_id)

    records = [r for r in caplog.records if r.name == "fault_injection"]
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == _logging.INFO
    assert rec.target == TARGET
    assert rec.fault_type == FAULT_TYPE
    assert rec.correlation_id == corr_id
