"""
CLI helper functions for the CIDX performance test harness.

Story #334: Concurrency Escalation Tests with Degradation Detection
AC1: Extracted from run_perf_suite.py to keep each module under 200 lines (Anti-File-Bloat).

Contains: argument parsing helpers, scenario loading, pre-flight check, progress callback.
"""

from __future__ import annotations

import sys
from typing import Optional

from config import Scenario, load_scenarios_from_dir
from preflight import validate_repos_exist

# Default concurrency levels per Story #334 AC1
DEFAULT_CONCURRENCY_LEVELS = [1, 2, 5, 10, 20, 50]


def parse_concurrency_levels(levels_str: Optional[str]) -> list[int]:
    """Parse --concurrency-levels string to a sorted list of ints."""
    if levels_str is None:
        return DEFAULT_CONCURRENCY_LEVELS
    try:
        levels = [int(x.strip()) for x in levels_str.split(",") if x.strip()]
    except ValueError as exc:
        print(f"ERROR: Invalid --concurrency-levels value: {exc}", file=sys.stderr)
        sys.exit(1)
    if not levels:
        print(
            "ERROR: --concurrency-levels must contain at least one value.",
            file=sys.stderr,
        )
        sys.exit(1)
    invalid = [level for level in levels if level < 1]
    if invalid:
        print(
            f"ERROR: Concurrency levels must be >= 1, got: {invalid}", file=sys.stderr
        )
        sys.exit(1)
    return sorted(levels)


def progress_callback(scenario_name: str, current: int, total: int) -> None:
    """Print simple progress to stdout."""
    if current < 0:
        print(f"  [warmup] {scenario_name}: warming up...", flush=True)
    else:
        print(f"  [measure] {scenario_name}: {current}/{total}", flush=True)


def load_scenarios(scenarios_dir: str) -> list[Scenario]:
    """Load scenarios from directory, exiting on failure."""
    try:
        scenarios = load_scenarios_from_dir(scenarios_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: Failed to load scenarios: {exc}", file=sys.stderr)
        sys.exit(1)

    if not scenarios:
        print("ERROR: No scenarios found in scenarios directory.", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(scenarios)} scenarios.")
    return scenarios


async def run_preflight_check(
    server_url: str,
    username: str,
    password: str,
    scenarios: list[Scenario],
) -> None:
    """Validate repo aliases exist on the server, exiting on missing repos."""
    required_aliases = list({s.repo_alias for s in scenarios})
    print(f"Pre-flight check: validating {len(required_aliases)} repo aliases...")
    try:
        missing = await validate_repos_exist(
            server_url=server_url,
            username=username,
            password=password,
            repo_aliases=required_aliases,
        )
    except RuntimeError as exc:
        print(f"ERROR: Pre-flight authentication failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if missing:
        print(
            "ERROR: The following repository aliases are not found on the server:",
            file=sys.stderr,
        )
        for alias in sorted(missing):
            print(f"  - {alias}", file=sys.stderr)
        print(
            "\nRegister missing repos before running the perf suite.", file=sys.stderr
        )
        sys.exit(1)

    print("Pre-flight check passed.")
