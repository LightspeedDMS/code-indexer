"""
Async load runner for the CIDX performance test harness.

Story #333: Performance Test Harness with Single-User Baselines
AC4: Warm-Up and Measurement Phase - 3 warmup + 20 measurement requests at concurrency=1.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

import httpx

from client import PerfClient
from config import Scenario
from metrics import MetricsResult, RequestResult, aggregate_metrics


async def _execute_single_request(
    perf_client: PerfClient,
    http_client: httpx.AsyncClient,
    scenario: Scenario,
) -> RequestResult:
    """Execute one request for a scenario (MCP or REST)."""
    if scenario.protocol == "mcp":
        return await perf_client.execute_mcp(
            client=http_client,
            tool_name=scenario.endpoint,
            arguments=scenario.parameters,
        )
    return await perf_client.execute_rest(
        client=http_client,
        endpoint=scenario.endpoint,
        parameters=scenario.parameters,
    )


async def run_scenario(
    perf_client: PerfClient,
    http_client: httpx.AsyncClient,
    scenario: Scenario,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
) -> MetricsResult:
    """
    Execute a single scenario with warm-up and measurement phases.

    Warm-up requests are discarded. Measurement requests are collected and
    aggregated into a MetricsResult.

    Args:
        perf_client: Authenticated PerfClient instance.
        http_client: httpx.AsyncClient to use for requests.
        scenario: Scenario definition to execute.
        progress_callback: Optional callback(scenario_name, current, total) for progress.

    Returns:
        MetricsResult with aggregated metrics for the scenario.
    """
    # Warm-up phase: execute and discard results
    for i in range(scenario.warmup_count):
        await _execute_single_request(perf_client, http_client, scenario)
        if progress_callback:
            progress_callback(scenario.name, -(scenario.warmup_count - i), scenario.measurement_count)

    # Measurement phase: collect results
    measurement_results: list[RequestResult] = []
    measurement_start = time.monotonic()

    for i in range(scenario.measurement_count):
        result = await _execute_single_request(perf_client, http_client, scenario)
        measurement_results.append(result)
        if progress_callback:
            progress_callback(scenario.name, i + 1, scenario.measurement_count)

    total_elapsed = time.monotonic() - measurement_start

    return aggregate_metrics(
        scenario_name=scenario.name,
        results=measurement_results,
        total_elapsed_seconds=total_elapsed,
    )


async def run_all_scenarios(
    server_url: str,
    username: str,
    password: str,
    scenarios: list[Scenario],
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
) -> list[MetricsResult]:
    """
    Run all scenarios sequentially (concurrency=1) for single-user baselines.

    Authenticates once at the start, then executes each scenario in order.
    Token refresh is handled transparently by PerfClient.

    Args:
        server_url: Base URL of the CIDX server.
        username: Login username.
        password: Login password.
        scenarios: List of Scenario definitions to execute.
        progress_callback: Optional callback for per-request progress reporting.

    Returns:
        List of MetricsResult in the same order as the input scenarios.
    """
    perf_client = PerfClient(server_url=server_url, username=username, password=password)
    results: list[MetricsResult] = []

    async with httpx.AsyncClient(timeout=60.0) as http_client:
        await perf_client.authenticate(http_client)

        for scenario in scenarios:
            metrics = await run_scenario(
                perf_client=perf_client,
                http_client=http_client,
                scenario=scenario,
                progress_callback=progress_callback,
            )
            results.append(metrics)

    return results
