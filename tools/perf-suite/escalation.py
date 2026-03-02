"""
Concurrency escalation runner for the CIDX performance test harness.

Story #334: Concurrency Escalation Tests with Degradation Detection
AC1: Concurrency escalation execution (ascending levels, warmup + measurement per level)
AC2: asyncio.Semaphore-based concurrency control (no threading)
AC3: Per-level metrics collection (independent MetricsResult per level)
AC6: Graceful error handling under load (errors captured, never raised)

This module is extracted from runner.py to keep each file under 200 lines.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import httpx

from config import Scenario
from metrics import EscalationResult, MetricsResult, RequestResult, aggregate_metrics, detect_inflection

if TYPE_CHECKING:
    from client import PerfClient

# Type alias for the injectable request execution function (enables unit testing without a server)
ExecuteFn = Callable[..., Awaitable[RequestResult]]


async def run_concurrent_requests(
    perf_client: PerfClient,
    http_client: httpx.AsyncClient,
    scenario: Scenario,
    concurrency: int,
    count: int,
    execute_fn: Optional[ExecuteFn] = None,
) -> list[RequestResult]:
    """
    Execute `count` requests with at most `concurrency` in-flight simultaneously.

    Uses asyncio.Semaphore to limit concurrent requests. All `count` tasks are
    launched via asyncio.gather(), with the semaphore controlling in-flight count.

    Request timing is measured inside the semaphore-guarded block (per-request).
    Errors are captured as RequestResult(success=False), never raised.

    Args:
        perf_client: PerfClient instance (or None for testing).
        http_client: httpx.AsyncClient instance (or None for testing).
        scenario: Scenario definition being executed.
        concurrency: Maximum number of requests in-flight at once.
        count: Total number of requests to execute.
        execute_fn: Optional injectable execution function (for testing). Defaults to
                    the standard _execute_single_request from runner module.

    Returns:
        List of `count` RequestResult objects.
    """
    if execute_fn is None:
        from runner import _execute_single_request
        execute_fn = _execute_single_request

    semaphore = asyncio.Semaphore(concurrency)

    async def guarded_request() -> RequestResult:
        async with semaphore:
            return await execute_fn(perf_client, http_client, scenario)

    tasks = [guarded_request() for _ in range(count)]
    return list(await asyncio.gather(*tasks))


async def run_scenario_escalation(
    perf_client: PerfClient,
    http_client: httpx.AsyncClient,
    scenario: Scenario,
    levels: list[int],
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    execute_fn: Optional[ExecuteFn] = None,
) -> EscalationResult:
    """
    Run a scenario at each concurrency level, collecting independent per-level metrics.

    For each level (in ascending order):
      1. Warmup: execute scenario.warmup_count requests (discarded).
      2. Measurement: execute scenario.measurement_count requests concurrently,
         record wall-clock elapsed time.
      3. Aggregate metrics independently for this level.

    After all levels complete, detect the inflection point across levels.

    Args:
        perf_client: PerfClient instance (or None for testing).
        http_client: httpx.AsyncClient instance (or None for testing).
        scenario: Scenario definition being executed.
        levels: List of concurrency levels to test (run in ascending order).
        progress_callback: Optional callback(scenario_name, current, total) for progress.
        execute_fn: Optional injectable execution function (for testing).

    Returns:
        EscalationResult containing per-level MetricsResult and inflection analysis.
    """
    level_metrics: dict[int, MetricsResult] = {}

    for level in sorted(levels):
        # Warmup phase at this concurrency level (results discarded)
        if progress_callback:
            progress_callback(scenario.name, -(scenario.warmup_count), scenario.measurement_count)
        await run_concurrent_requests(
            perf_client=perf_client,
            http_client=http_client,
            scenario=scenario,
            concurrency=level,
            count=scenario.warmup_count,
            execute_fn=execute_fn,
        )

        # Measurement phase: wall-clock time wraps the entire gather
        measurement_start = time.monotonic()
        results = await run_concurrent_requests(
            perf_client=perf_client,
            http_client=http_client,
            scenario=scenario,
            concurrency=level,
            count=scenario.measurement_count,
            execute_fn=execute_fn,
        )
        elapsed = time.monotonic() - measurement_start

        level_metrics[level] = aggregate_metrics(
            scenario_name=scenario.name,
            results=results,
            total_elapsed_seconds=elapsed,
        )

        if progress_callback:
            progress_callback(scenario.name, scenario.measurement_count, scenario.measurement_count)

    inflection = detect_inflection(level_metrics)

    return EscalationResult(
        scenario=scenario,
        level_metrics=level_metrics,
        inflection=inflection,
    )
