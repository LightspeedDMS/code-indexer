"""
Main CLI entry point for the CIDX performance test harness.

Story #333: Performance Test Harness with Single-User Baselines
AC1: CLI Entry Point and Configuration

Story #334: Concurrency Escalation Tests with Degradation Detection
AC1: --concurrency-levels CLI argument (default: 1,2,5,10,20,50)

Usage:
    python run_perf_suite.py \\
        --server-url http://localhost:8000 \\
        --username admin \\
        --password admin \\
        --output-dir /tmp/perf-results \\
        --concurrency-levels 1,2,5,10,20,50
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from cli_helpers import (
    DEFAULT_CONCURRENCY_LEVELS,
    load_scenarios,
    parse_concurrency_levels,
    progress_callback,
    run_preflight_check,
)
from client import PerfClient
from escalation import run_scenario_escalation
from metrics import EscalationResult
from output import print_escalation_summary, write_escalation_results

# Scenarios directory relative to this script
SCENARIOS_DIR = os.path.join(os.path.dirname(__file__), "scenarios")


def parse_args() -> argparse.Namespace:
    """Parse and validate CLI arguments."""
    parser = argparse.ArgumentParser(
        description="CIDX Performance Test Harness - Concurrency Escalation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--server-url",
        required=True,
        help="Base URL of the CIDX server (e.g., http://localhost:8000)",
    )
    parser.add_argument(
        "--username",
        required=True,
        help="CIDX server username for authentication",
    )
    parser.add_argument(
        "--password",
        required=True,
        help="CIDX server password for authentication",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory to write raw_metrics.json output",
    )
    parser.add_argument(
        "--scenarios-dir",
        default=SCENARIOS_DIR,
        help=f"Directory containing scenario JSON files (default: {SCENARIOS_DIR})",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        default=False,
        help="Skip repository pre-flight validation",
    )
    parser.add_argument(
        "--concurrency-levels",
        default=None,
        help=(
            "Comma-separated list of concurrency levels to test "
            f"(default: {','.join(str(level) for level in DEFAULT_CONCURRENCY_LEVELS)})"
        ),
    )
    return parser.parse_args()


async def _main(args: argparse.Namespace) -> int:
    """Async main entry point. Returns exit code."""
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    scenarios = load_scenarios(args.scenarios_dir)
    concurrency_levels = parse_concurrency_levels(args.concurrency_levels)

    if not args.skip_preflight:
        await run_preflight_check(
            server_url=args.server_url,
            username=args.username,
            password=args.password,
            scenarios=scenarios,
        )

    print(
        f"\nRunning {len(scenarios)} scenarios against {args.server_url} "
        f"at concurrency levels {concurrency_levels}..."
    )
    started_at = datetime.now(timezone.utc)

    perf_client = PerfClient(
        server_url=args.server_url,
        username=args.username,
        password=args.password,
    )
    escalation_results: list[EscalationResult] = []

    try:
        async with httpx.AsyncClient(timeout=60.0) as http_client:
            await perf_client.authenticate(http_client)

            for scenario in scenarios:
                print(f"\n  Scenario: {scenario.name}")
                result = await run_scenario_escalation(
                    perf_client=perf_client,
                    http_client=http_client,
                    scenario=scenario,
                    levels=concurrency_levels,
                    progress_callback=progress_callback,
                )
                escalation_results.append(result)

    except RuntimeError as exc:
        print(f"ERROR: Suite execution failed: {exc}", file=sys.stderr)
        return 1

    finished_at = datetime.now(timezone.utc)
    output_file = write_escalation_results(
        output_dir=output_dir,
        results=escalation_results,
        started_at=started_at,
        finished_at=finished_at,
        server_url=args.server_url,
        concurrency_levels=concurrency_levels,
    )

    print(f"\nResults written to: {output_file}")
    print(f"Total scenarios: {len(escalation_results)}")
    print_escalation_summary(escalation_results)

    return 0


def main() -> None:
    """Synchronous CLI entry point."""
    args = parse_args()
    exit_code = asyncio.run(_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
