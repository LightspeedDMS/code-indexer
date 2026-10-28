import threading
from typing import Callable, List, Optional

import pytest

from perf_suite import client, measure


def test_percentile_nearest_rank():
    values = [float(v) for v in range(1, 101)]
    assert measure.percentile(values, 50) == 50.0
    assert measure.percentile(values, 95) == 95.0
    assert measure.percentile(values, 99) == 99.0
    assert measure.percentile([7.0], 99) == 7.0


def test_median_and_mad():
    assert measure.median([3.0, 1.0, 2.0]) == 2.0
    assert measure.median([1.0, 2.0, 3.0, 4.0]) == 2.5
    assert measure.mad([1.0, 2.0, 3.0, 4.0, 100.0]) == 1.0


class _Calls:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.count = 0

    def ok(self) -> Optional[str]:
        with self.lock:
            self.count += 1
        return None


def test_closed_loop_counts_errors_and_wrong_results_separately():
    calls = _Calls()

    def wrong() -> Optional[str]:
        calls.ok()
        return "repo fts 'x': expected a.py; top ['b.py']"

    def failing() -> Optional[str]:
        calls.ok()
        raise client.RequestFailedError("HTTP 500")

    items: List[Callable[[], Optional[str]]] = [calls.ok, wrong, failing, calls.ok]
    stats = measure.run_closed_loop(items, concurrency=2)
    assert stats.requests == 8
    assert calls.count == 8
    assert stats.errors == 2
    assert stats.wrong == 2
    assert stats.served == 6
    assert stats.rps is not None and stats.rps > 0
    assert stats.p50_ms is not None and stats.p95_ms is not None
    assert stats.p99_ms is not None
    assert stats.p50_ms <= stats.p95_ms <= stats.p99_ms
    assert stats.error_samples == ["HTTP 500"]
    assert stats.wrong_samples == ["repo fts 'x': expected a.py; top ['b.py']"]


def _misses(count: int) -> List[Callable[[], Optional[str]]]:
    def miss(index: int) -> Callable[[], Optional[str]]:
        def run() -> Optional[str]:
            return f"query-{index} missed"

        return run

    return [miss(i) for i in range(count)]


def test_wrong_samples_are_bounded_per_run_and_per_scenario():
    stats = measure.run_closed_loop(_misses(7), concurrency=1)
    assert stats.wrong == 7
    assert len(stats.wrong_samples) == measure.MAX_WRONG_SAMPLES == 5
    result = measure.run_scenario(_misses(7), concurrency=1, repetitions=2)
    assert len(result["wrong_samples"]) == 5
    assert all(s.startswith("query-") for s in result["wrong_samples"])


def test_closed_loop_issues_at_least_four_requests_per_worker():
    calls = _Calls()
    stats = measure.run_closed_loop([calls.ok], concurrency=3)
    assert stats.requests == 12
    assert calls.count == 12


def test_unexpected_shape_aborts_the_scenario():
    def bad_shape() -> Optional[str]:
        raise client.ShapeError("search_code result has no results list")

    with pytest.raises(measure.ScenarioAbortedError, match="no results list"):
        measure.run_closed_loop([bad_shape], concurrency=1)


def test_failed_job_poll_aborts_the_scenario():
    def poll_fails() -> Optional[str]:
        raise client.PollFailedError("GET /api/jobs/{job_id}: ConnectError")

    result = measure.run_scenario([poll_fails], concurrency=1, repetitions=1)
    assert result["aborted"] == "GET /api/jobs/{job_id}: ConnectError"


def test_run_scenario_excludes_warmup_from_timing_but_records_its_outcomes():
    calls = _Calls()
    result = measure.run_scenario([calls.ok, calls.ok], concurrency=1, repetitions=3)
    assert calls.count == 2 + 3 * 4
    assert len(result["warmup_ms"]) == 2
    assert len(result["repetitions"]) == 3
    assert result["median"]["requests"] == 4
    assert result["mad"]["errors"] == 0.0
    assert result["aborted"] is None
    assert result["warmup_wrong"] == 0 and result["warmup_errors"] == 0


def test_warmup_only_miss_is_recorded_with_its_sample():
    state = {"first": True}

    def miss_once() -> Optional[str]:
        if state["first"]:
            state["first"] = False
            return "cold miss on first call"
        return None

    result = measure.run_scenario([miss_once], concurrency=1, repetitions=1)
    assert result["warmup_wrong"] == 1
    assert result["median"]["wrong"] == 0
    assert result["wrong_samples"] == ["cold miss on first call"]


def test_run_scenario_records_abort_message():
    def bad_shape() -> Optional[str]:
        raise client.ShapeError("missing fts_results")

    result = measure.run_scenario([bad_shape], concurrency=1, repetitions=2)
    assert result["aborted"] == "missing fts_results"
    assert result["repetitions"] == []
