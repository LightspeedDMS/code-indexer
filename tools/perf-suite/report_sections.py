"""
Section renderers for the CIDX performance report.

Story #335: Performance Report with Hardware Profile
AC2: Endpoint metrics tables
AC3: ASCII bar charts for degradation profiles
AC4: Cross-repository comparison tables
AC5: Executive summary with key findings

Each public function returns a Markdown string for its section.
"""

from __future__ import annotations

from typing import Any, Optional

# Re-export chart/table renderers that have been extracted to report_charts.
# These re-exports preserve backward-compatible imports for existing consumers.
from report_charts import (  # noqa: F401
    render_ascii_chart,
    render_cross_repo_table,
    render_metrics_table,
)

# Error rate threshold for "error-prone" classification in executive summary
_ERROR_PRONE_PCT = 10.0

# How many entries to show in top-N rankings in executive summary
_TOP_N_RANKINGS = 3


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _collect_scenario_stats(
    scenarios: dict[str, Any],
) -> tuple[list[tuple[str, float]], list[tuple[str, Optional[int]]], list[str]]:
    """
    Collect baseline p50, inflection, and error-prone stats from all scenarios.

    Returns:
        baselines: List of (scenario_name, baseline_p50_ms).
        inflections: List of (scenario_name, inflection_level or None).
        error_prone: List of human-readable strings for scenarios exceeding error threshold.
    """
    baselines: list[tuple[str, float]] = []
    inflections: list[tuple[str, Optional[int]]] = []
    error_prone: list[str] = []

    for name, data in scenarios.items():
        levels = data.get("levels", {})
        if not levels:
            continue

        sorted_keys = sorted(int(k) for k in levels.keys())
        baseline_key = str(sorted_keys[0])
        highest_key = str(sorted_keys[-1])

        baseline_p50 = levels[baseline_key].get("p50_ms", 0.0)
        baselines.append((name, baseline_p50))

        inflection_level = (data.get("inflection") or {}).get("inflection_level")
        inflections.append((name, inflection_level))

        highest_error = levels[highest_key].get("error_rate_pct", 0.0)
        if highest_error > _ERROR_PRONE_PCT:
            error_prone.append(
                f"{name} ({highest_error:.1f}% at level {sorted_keys[-1]})"
            )

    return baselines, inflections, error_prone


def _format_capacity_recommendation(
    most_sensitive: list[tuple[str, int]],
    max_level: Any,
) -> str:
    """Return a single capacity recommendation sentence."""
    if most_sensitive:
        rec_name, rec_level = most_sensitive[0]
        return (
            f"**Capacity recommendation**: The most sensitive endpoint (`{rec_name}`) "
            f"degrades significantly at concurrency {rec_level}; "
            f"production deployments should not exceed that concurrency level without "
            f"infrastructure scaling."
        )
    return (
        "**Capacity recommendation**: All endpoints remained stable across tested "
        f"concurrency levels up to {max_level}."
    )


# ---------------------------------------------------------------------------
# Public section renderers
# ---------------------------------------------------------------------------


def render_hardware_section(hardware_data: Optional[dict[str, str]]) -> str:
    """
    Render the Hardware Profile section.

    Args:
        hardware_data: Dict with keys cpu, ram, disk, os, python_version.
                       None or empty dict produces a placeholder.

    Returns:
        Markdown string for the hardware section.
    """
    if not hardware_data:
        return "## Hardware Profile\n\nHardware: Not captured (SSH not configured or unavailable).\n"

    lines = ["## Hardware Profile\n"]
    if hardware_data.get("os"):
        lines.append(f"**OS**: {hardware_data['os']}")
    if hardware_data.get("cpu"):
        lines.append(f"**CPU**:\n```\n{hardware_data['cpu']}\n```")
    if hardware_data.get("cpu_cores"):
        lines.append(f"**CPU Cores**: {hardware_data['cpu_cores']}")
    if hardware_data.get("ram"):
        lines.append(f"**Memory**:\n```\n{hardware_data['ram']}\n```")
    if hardware_data.get("disk"):
        lines.append(f"**Disk**:\n```\n{hardware_data['disk']}\n```")
    if hardware_data.get("python_version"):
        lines.append(f"**Python**: {hardware_data['python_version']}")
    return "\n".join(lines) + "\n"


def render_executive_summary(
    metadata: dict[str, Any],
    scenarios: dict[str, Any],
) -> str:
    """
    Render the Executive Summary section.

    Includes headline metrics, fastest endpoints, most degradation-sensitive,
    error-prone under load, and a capacity recommendation sentence.

    Args:
        metadata: The metadata dict from raw_metrics.json.
        scenarios: The scenarios dict from raw_metrics.json.

    Returns:
        Markdown string for the executive summary section.
    """
    total = metadata.get("total_scenarios", len(scenarios))
    started_at = metadata.get("started_at", "")
    date_str = started_at[:10] if started_at else "unknown"
    concurrency_levels = metadata.get("concurrency_levels", [])
    max_level = max(concurrency_levels) if concurrency_levels else "unknown"

    baselines, inflections, error_prone = _collect_scenario_stats(scenarios)

    fastest = sorted(baselines, key=lambda x: x[1])[:_TOP_N_RANKINGS]
    with_inflection = [(n, lvl) for n, lvl in inflections if lvl is not None]
    most_sensitive = sorted(with_inflection, key=lambda x: x[1])[:_TOP_N_RANKINGS]

    lines = [
        "## Executive Summary\n",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Date | {date_str} |",
        f"| Total scenarios | {total} |",
        f"| Max concurrency tested | {max_level} |",
        "",
        "### Fastest Endpoints (baseline p50)\n",
    ]
    for i, (name, p50) in enumerate(fastest, 1):
        lines.append(f"{i}. `{name}` — {p50:.1f} ms")

    lines += ["", "### Most Degradation-Sensitive (lowest inflection level)\n"]
    if most_sensitive:
        for i, (name, lvl) in enumerate(most_sensitive, 1):
            lines.append(f"{i}. `{name}` — inflection at concurrency {lvl}")
    else:
        lines.append(
            "All tested scenarios showed stable performance (no inflection detected)."
        )

    lines += ["", "### Error-Prone Under Load (>10% errors at highest concurrency)\n"]
    lines += (
        [f"- {e}" for e in error_prone]
        if error_prone
        else ["No scenarios exceeded 10% error rate."]
    )

    lines += ["", _format_capacity_recommendation(most_sensitive, max_level)]
    return "\n".join(lines) + "\n"
