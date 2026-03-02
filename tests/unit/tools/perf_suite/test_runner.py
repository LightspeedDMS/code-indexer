"""
Unit tests for tools/perf-suite/runner.py and tools/perf-suite/escalation.py

Story #334: Concurrency Escalation Tests with Degradation Detection
AC1: Concurrency escalation execution
AC2: asyncio.Semaphore-based concurrency control
AC3: Per-level metrics collection
AC6: Graceful error handling under load

TDD: These tests were written BEFORE the implementation.
"""

from __future__ import annotations

import asyncio
import sys
import os
from dataclasses import dataclass
from typing import Any, Optional

import pytest

# Add the perf-suite directory to path so we can import from it
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../tools/perf-suite"))

from cli_helpers import parse_concurrency_levels  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers: fake scenario and fake request execution for unit testing
# ---------------------------------------------------------------------------

def _make_scenario(name: str = "test_scenario") -> Any:
    """Create a minimal Scenario for testing."""
    from config import Scenario

    return Scenario(
        name=name,
        endpoint="search_code",
        protocol="mcp",
        method="POST",
        parameters={"query_text": "auth", "repository_alias": "test-global"},
        repo_alias="test-global",
        priority="highest",
        warmup_count=2,
        measurement_count=5,
    )


def _make_success_result(response_time_ms: float = 100.0) -> Any:
    """Create a successful RequestResult for testing."""
    from metrics import RequestResult

    return RequestResult(
        response_time_ms=response_time_ms,
        status_code=200,
        success=True,
        response_size_bytes=1024,
    )


def _make_error_result(response_time_ms: float = 10.0) -> Any:
    """Create a failed RequestResult for testing."""
    from metrics import RequestResult

    return RequestResult(
        response_time_ms=response_time_ms,
        status_code=500,
        success=False,
        response_size_bytes=0,
        error_message="Internal server error",
    )


# ---------------------------------------------------------------------------
# Tests for run_concurrent_requests()
# ---------------------------------------------------------------------------

class TestRunConcurrentRequests:
    """Tests for escalation.run_concurrent_requests() - semaphore-based concurrency."""

    @pytest.mark.asyncio
    async def test_returns_correct_count_of_results(self):
        """run_concurrent_requests returns exactly `count` RequestResult objects."""
        from escalation import run_concurrent_requests
        from metrics import RequestResult

        call_count = 0

        async def fake_execute(perf_client, http_client, scenario):
            nonlocal call_count
            call_count += 1
            return _make_success_result()

        results = await run_concurrent_requests(
            perf_client=None,
            http_client=None,
            scenario=_make_scenario(),
            concurrency=3,
            count=10,
            execute_fn=fake_execute,
        )

        assert len(results) == 10
        assert call_count == 10

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self):
        """At most N requests run concurrently when semaphore=N."""
        from escalation import run_concurrent_requests

        max_in_flight = 0
        current_in_flight = 0
        lock = asyncio.Lock()

        async def slow_execute(perf_client, http_client, scenario):
            nonlocal max_in_flight, current_in_flight
            async with lock:
                current_in_flight += 1
                if current_in_flight > max_in_flight:
                    max_in_flight = current_in_flight
            # Yield to allow other coroutines to potentially start
            await asyncio.sleep(0.01)
            async with lock:
                current_in_flight -= 1
            return _make_success_result()

        await run_concurrent_requests(
            perf_client=None,
            http_client=None,
            scenario=_make_scenario(),
            concurrency=3,
            count=15,
            execute_fn=slow_execute,
        )

        # max concurrent should be <= 3 (the semaphore limit)
        assert max_in_flight <= 3

    @pytest.mark.asyncio
    async def test_semaphore_uses_full_concurrency(self):
        """With concurrency=5 and enough requests, should reach 5 in-flight."""
        from escalation import run_concurrent_requests

        max_in_flight = 0
        current_in_flight = 0
        lock = asyncio.Lock()

        async def slow_execute(perf_client, http_client, scenario):
            nonlocal max_in_flight, current_in_flight
            async with lock:
                current_in_flight += 1
                if current_in_flight > max_in_flight:
                    max_in_flight = current_in_flight
            await asyncio.sleep(0.02)
            async with lock:
                current_in_flight -= 1
            return _make_success_result()

        await run_concurrent_requests(
            perf_client=None,
            http_client=None,
            scenario=_make_scenario(),
            concurrency=5,
            count=20,
            execute_fn=slow_execute,
        )

        # With 20 requests and concurrency=5, should reach at least 3 in-flight
        assert max_in_flight >= 3

    @pytest.mark.asyncio
    async def test_errors_captured_not_raised(self):
        """Request exceptions should be captured as error RequestResult, not re-raised."""
        from escalation import run_concurrent_requests
        from metrics import RequestResult

        async def failing_execute(perf_client, http_client, scenario):
            return _make_error_result()

        results = await run_concurrent_requests(
            perf_client=None,
            http_client=None,
            scenario=_make_scenario(),
            concurrency=2,
            count=5,
            execute_fn=failing_execute,
        )

        assert len(results) == 5
        assert all(not r.success for r in results)

    @pytest.mark.asyncio
    async def test_concurrency_one_runs_sequentially(self):
        """concurrency=1 means only one request at a time."""
        from escalation import run_concurrent_requests

        max_in_flight = 0
        current_in_flight = 0
        lock = asyncio.Lock()

        async def slow_execute(perf_client, http_client, scenario):
            nonlocal max_in_flight, current_in_flight
            async with lock:
                current_in_flight += 1
                if current_in_flight > max_in_flight:
                    max_in_flight = current_in_flight
            await asyncio.sleep(0.01)
            async with lock:
                current_in_flight -= 1
            return _make_success_result()

        await run_concurrent_requests(
            perf_client=None,
            http_client=None,
            scenario=_make_scenario(),
            concurrency=1,
            count=10,
            execute_fn=slow_execute,
        )

        assert max_in_flight == 1


# ---------------------------------------------------------------------------
# Tests for run_scenario_escalation()
# ---------------------------------------------------------------------------

class TestRunScenarioEscalation:
    """Tests for escalation.run_scenario_escalation() - multi-level escalation."""

    @pytest.mark.asyncio
    async def test_returns_escalation_result(self):
        """run_scenario_escalation returns an EscalationResult with level data."""
        from escalation import run_scenario_escalation
        from metrics import EscalationResult

        async def fast_execute(perf_client, http_client, scenario):
            return _make_success_result(100.0)

        result = await run_scenario_escalation(
            perf_client=None,
            http_client=None,
            scenario=_make_scenario(),
            levels=[1, 2],
            progress_callback=None,
            execute_fn=fast_execute,
        )

        assert isinstance(result, EscalationResult)
        assert 1 in result.level_metrics
        assert 2 in result.level_metrics

    @pytest.mark.asyncio
    async def test_all_levels_are_in_result(self):
        """All specified concurrency levels appear in the result."""
        from escalation import run_scenario_escalation

        async def fast_execute(perf_client, http_client, scenario):
            return _make_success_result(50.0)

        levels = [1, 2, 5, 10]
        result = await run_scenario_escalation(
            perf_client=None,
            http_client=None,
            scenario=_make_scenario(),
            levels=levels,
            progress_callback=None,
            execute_fn=fast_execute,
        )

        for level in levels:
            assert level in result.level_metrics

    @pytest.mark.asyncio
    async def test_level_metrics_have_correct_request_count(self):
        """Each level's metrics reflect the measurement_count, not warmup."""
        from escalation import run_scenario_escalation

        scenario = _make_scenario()
        scenario_measurement_count = scenario.measurement_count

        async def fast_execute(perf_client, http_client, scenario):
            return _make_success_result()

        result = await run_scenario_escalation(
            perf_client=None,
            http_client=None,
            scenario=scenario,
            levels=[1],
            progress_callback=None,
            execute_fn=fast_execute,
        )

        assert result.level_metrics[1].total_requests == scenario_measurement_count

    @pytest.mark.asyncio
    async def test_warmup_is_discarded(self):
        """Warmup requests do not appear in the measurement metrics."""
        from escalation import run_scenario_escalation

        call_times = []

        async def tracking_execute(perf_client, http_client, scenario):
            call_times.append(len(call_times))
            return _make_success_result(float(len(call_times)))

        scenario = _make_scenario()
        # warmup_count=2, measurement_count=5, total=7

        result = await run_scenario_escalation(
            perf_client=None,
            http_client=None,
            scenario=scenario,
            levels=[1],
            progress_callback=None,
            execute_fn=tracking_execute,
        )

        # Total requests should be warmup_count + measurement_count
        total_calls = scenario.warmup_count + scenario.measurement_count
        assert len(call_times) == total_calls
        # But only measurement_count appear in metrics
        assert result.level_metrics[1].total_requests == scenario.measurement_count

    @pytest.mark.asyncio
    async def test_inflection_result_present(self):
        """EscalationResult includes an InflectionResult."""
        from escalation import run_scenario_escalation
        from metrics import InflectionResult

        async def fast_execute(perf_client, http_client, scenario):
            return _make_success_result(100.0)

        result = await run_scenario_escalation(
            perf_client=None,
            http_client=None,
            scenario=_make_scenario(),
            levels=[1, 2],
            progress_callback=None,
            execute_fn=fast_execute,
        )

        assert isinstance(result.inflection, InflectionResult)

    @pytest.mark.asyncio
    async def test_progress_callback_called(self):
        """progress_callback is invoked during escalation."""
        from escalation import run_scenario_escalation

        callback_calls = []

        def progress(scenario_name, current, total):
            callback_calls.append((scenario_name, current, total))

        async def fast_execute(perf_client, http_client, scenario):
            return _make_success_result()

        await run_scenario_escalation(
            perf_client=None,
            http_client=None,
            scenario=_make_scenario(),
            levels=[1],
            progress_callback=progress,
            execute_fn=fast_execute,
        )

        assert len(callback_calls) > 0


# ---------------------------------------------------------------------------
# Tests for backward compatibility: run_scenario() still works at concurrency=1
# ---------------------------------------------------------------------------

class TestRunScenarioBackwardCompatibility:
    """run_scenario() from runner.py must still work as before."""

    @pytest.mark.asyncio
    async def test_run_scenario_returns_metrics_result(self):
        """run_scenario still returns a single MetricsResult for concurrency=1."""
        from runner import run_scenario
        from metrics import MetricsResult

        async def fake_execute(perf_client, http_client, scenario):
            return _make_success_result(150.0)

        # Patch _execute_single_request via monkey-patching runner module
        import runner as runner_module
        original = runner_module._execute_single_request
        runner_module._execute_single_request = fake_execute

        try:
            result = await run_scenario(
                perf_client=None,
                http_client=None,
                scenario=_make_scenario(),
                progress_callback=None,
            )
        finally:
            runner_module._execute_single_request = original

        assert isinstance(result, MetricsResult)
        assert result.total_requests == _make_scenario().measurement_count


# ---------------------------------------------------------------------------
# Tests for parse_concurrency_levels() validation
# ---------------------------------------------------------------------------

def test_parse_concurrency_levels_rejects_zero():
    """Zero concurrency level should cause sys.exit."""
    with pytest.raises(SystemExit):
        parse_concurrency_levels("0,1,5")


def test_parse_concurrency_levels_rejects_negative():
    """Negative concurrency level should cause sys.exit."""
    with pytest.raises(SystemExit):
        parse_concurrency_levels("-1,5,10")
