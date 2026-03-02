"""
Output formatting and file writing for the CIDX performance test harness.

Story #333: Performance Test Harness with Single-User Baselines
AC7: Raw JSON Metrics Output - writes raw_metrics.json with ISO 8601 timestamps.

Story #334: Concurrency Escalation Tests with Degradation Detection
AC5: Cross-repository comparison support via per-scenario dict with endpoint/repo_alias.
AC7: Escalation results in JSON output - write_escalation_results() with new schema.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from metrics import EscalationResult, MetricsResult

# Summary table column widths
COL_SCENARIO = 50
COL_METRIC = 8
SEPARATOR_WIDTH = COL_SCENARIO + (COL_METRIC * 4) + 6


def sanitize_server_url(server_url: str) -> str:
    """Replace server hostname with a placeholder for output sanitization."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(server_url)
        sanitized = parsed._replace(netloc="<server>")
        return sanitized.geturl()
    except Exception:
        # URL sanitization is best-effort for display only; failures are non-critical
        return "<server>"


def write_results(
    output_dir: Path,
    results: list[MetricsResult],
    started_at: datetime,
    finished_at: datetime,
    server_url: str,
) -> Path:
    """
    Serialize results to raw_metrics.json in the output directory.

    Output includes sanitized server URL and ISO 8601 timestamps.

    Returns:
        Path to the written output file.
    """
    output = {
        "metadata": {
            "server_url": sanitize_server_url(server_url),
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "total_scenarios": len(results),
            "concurrency": 1,
        },
        "scenarios": [r.to_dict() for r in results],
    }

    output_file = output_dir / "raw_metrics.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    return output_file


def print_summary(results: list[MetricsResult]) -> None:
    """Print a human-readable summary table to stdout."""
    print(
        f"\n{'Scenario':<{COL_SCENARIO}} {'p50':>{COL_METRIC}} "
        f"{'p95':>{COL_METRIC}} {'p99':>{COL_METRIC}} {'Errors':>{COL_METRIC}}"
    )
    print("-" * SEPARATOR_WIDTH)
    for r in results:
        print(
            f"{r.scenario_name:<{COL_SCENARIO}} {r.p50_ms:>{COL_METRIC - 1}.0f}ms "
            f"{r.p95_ms:>{COL_METRIC - 1}.0f}ms {r.p99_ms:>{COL_METRIC - 1}.0f}ms "
            f"{r.error_rate_pct:>{COL_METRIC - 1}.1f}%"
        )


def write_escalation_results(
    output_dir: Path,
    results: list[EscalationResult],
    started_at: datetime,
    finished_at: datetime,
    server_url: str,
    concurrency_levels: list[int],
) -> Path:
    """
    Serialize escalation results to raw_metrics.json in the output directory.

    Output schema uses a dict for scenarios (keyed by name) with per-level data.
    Compatible with Story #335 report generator. File written atomically (write
    to temp name, then rename).

    Args:
        output_dir: Directory to write raw_metrics.json.
        results: List of EscalationResult from run_scenario_escalation().
        started_at: Suite start timestamp.
        finished_at: Suite finish timestamp.
        server_url: Server URL (hostname sanitized in output).
        concurrency_levels: The concurrency levels that were configured for the run.

    Returns:
        Path to the written output file.
    """
    scenarios_dict = {r.scenario.name: r.to_dict() for r in results}

    output = {
        "metadata": {
            "server_url": sanitize_server_url(server_url),
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "total_scenarios": len(results),
            "concurrency_levels": concurrency_levels,
        },
        "scenarios": scenarios_dict,
    }

    output_file = output_dir / "raw_metrics.json"
    tmp_file = output_dir / "raw_metrics.json.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    tmp_file.rename(output_file)

    return output_file


# Escalation summary column widths
COL_ESCALATION_SCENARIO = 45
COL_LEVEL = 6
COL_ESCALATION_METRIC = 8
ESCALATION_SEPARATOR_WIDTH = COL_ESCALATION_SCENARIO + COL_LEVEL + COL_ESCALATION_METRIC * 3 + 20


def print_escalation_summary(results: list[EscalationResult]) -> None:
    """Print a human-readable escalation summary table to stdout."""
    print(
        f"\n{'Scenario':<{COL_ESCALATION_SCENARIO}} {'Level':>{COL_LEVEL}} "
        f"{'p50':>{COL_ESCALATION_METRIC}} {'p95':>{COL_ESCALATION_METRIC}} "
        f"{'Errors':>{COL_ESCALATION_METRIC}}  Inflection"
    )
    print("-" * ESCALATION_SEPARATOR_WIDTH)

    for escalation in results:
        name = escalation.scenario.name
        inflection = escalation.inflection
        inflection_str = (
            f"at level {inflection.inflection_level}"
            if inflection.inflection_level is not None
            else "stable"
        )
        if inflection.baseline_unreliable:
            inflection_str += " (baseline unreliable)"

        sorted_levels = sorted(escalation.level_metrics.keys())
        for level in sorted_levels:
            m = escalation.level_metrics[level]
            level_inflection = (
                inflection_str if level == sorted_levels[-1] else ""
            )
            print(
                f"{name:<{COL_ESCALATION_SCENARIO}} {level:>{COL_LEVEL}} "
                f"{m.p50_ms:>{COL_ESCALATION_METRIC - 1}.0f}ms "
                f"{m.p95_ms:>{COL_ESCALATION_METRIC - 1}.0f}ms "
                f"{m.error_rate_pct:>{COL_ESCALATION_METRIC - 1}.1f}%  {level_inflection}"
            )
            name = ""  # Only print name on first level row
