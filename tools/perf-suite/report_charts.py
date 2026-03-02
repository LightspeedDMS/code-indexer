"""
ASCII chart and cross-repository comparison table renderers for the CIDX performance report.

Story #335: Performance Report with Hardware Profile
AC3: ASCII bar charts for degradation profiles
AC4: Cross-repository comparison tables

Each public function returns a Markdown string for its section.
"""

from __future__ import annotations

from typing import Any

# Maximum bar width for ASCII charts (characters)
_BAR_MAX_WIDTH = 60


def render_metrics_table(scenario_name: str, scenario_data: dict[str, Any]) -> str:
    """
    Render a Markdown metrics table for a single scenario.

    Columns: Concurrency | p50 (ms) | p95 (ms) | p99 (ms) | Throughput (req/s) | Errors (%)
    The inflection row is bold-marked with ** on every cell.

    Args:
        scenario_name: Human-readable scenario name (used in table context).
        scenario_data: Scenario dict with 'levels' and 'inflection' keys.

    Returns:
        Markdown table string.
    """
    levels = scenario_data.get("levels", {})
    inflection_level = (scenario_data.get("inflection") or {}).get("inflection_level")
    sorted_levels = sorted(int(k) for k in levels.keys())

    rows = [
        "| Concurrency | p50 (ms) | p95 (ms) | p99 (ms) | Throughput (req/s) | Errors (%) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for level in sorted_levels:
        d = levels[str(level)]
        p50, p95, p99 = d.get("p50_ms", 0.0), d.get("p95_ms", 0.0), d.get("p99_ms", 0.0)
        tput, err = d.get("throughput_rps", 0.0), d.get("error_rate_pct", 0.0)
        if level == inflection_level:
            rows.append(
                f"| **{level}** | **{p50:.1f}** | **{p95:.1f}** | **{p99:.1f}** "
                f"| **{tput:.1f}** | **{err:.1f}** |"
            )
        else:
            rows.append(f"| {level} | {p50:.1f} | {p95:.1f} | {p99:.1f} | {tput:.1f} | {err:.1f} |")
    return "\n".join(rows) + "\n"


def render_ascii_chart(scenario_name: str, scenario_data: dict[str, Any]) -> str:
    """
    Render an ASCII horizontal bar chart for p50 degradation profile.

    Each bar: "  LEVEL | ===...=== VALUE ms [<-- inflection]"
    Bar width normalized to _BAR_MAX_WIDTH characters.
    Wrapped in a Markdown code block.

    Args:
        scenario_name: Human-readable scenario name (used in chart title).
        scenario_data: Scenario dict with 'levels' and 'inflection' keys.

    Returns:
        Markdown code block string with ASCII chart.
    """
    levels = scenario_data.get("levels", {})
    inflection_level = (scenario_data.get("inflection") or {}).get("inflection_level")
    sorted_levels = sorted(int(k) for k in levels.keys())
    p50_values = {lvl: levels[str(lvl)].get("p50_ms", 0.0) for lvl in sorted_levels}

    max_p50 = max(p50_values.values()) if p50_values else 1.0
    if max_p50 == 0.0:
        max_p50 = 1.0

    label_width = max(len(str(max(sorted_levels))) if sorted_levels else 2, 2)
    chart_lines = [f"p50 Degradation Profile: {scenario_name}", ""]
    for level in sorted_levels:
        p50 = p50_values[level]
        bar_len = int(round((p50 / max_p50) * _BAR_MAX_WIDTH))
        marker = "  <-- inflection" if level == inflection_level else ""
        chart_lines.append(f"  {level:>{label_width}} | {'=' * bar_len} {p50:.1f} ms{marker}")

    return "```\n" + "\n".join(chart_lines) + "\n```\n"


def render_cross_repo_table(endpoint: str, scenarios: dict[str, Any]) -> str:
    """
    Render a cross-repository comparison table for a given endpoint.

    Only rendered when the endpoint appears in 2+ distinct repositories.
    Columns: Repository | p50 @baseline (ms) | p50 @highest (ms) | Inflection Level | Throughput @1 (req/s)

    Args:
        endpoint: Endpoint name (e.g., "search_code").
        scenarios: All scenarios dict from raw_metrics.json.

    Returns:
        Markdown table string, or empty string if fewer than 2 repos.
    """
    repo_rows: dict[str, dict[str, Any]] = {}
    for _, data in scenarios.items():
        if data.get("endpoint") != endpoint:
            continue
        repo_alias = data.get("repo_alias", "unknown")
        if repo_alias not in repo_rows:
            repo_rows[repo_alias] = data

    if len(repo_rows) < 2:
        return ""

    rows = [
        "| Repository | p50 @1 (ms) | p50 @50 (ms) | Inflection Level | Throughput @1 (req/s) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for repo_alias, data in sorted(repo_rows.items()):
        levels = data.get("levels", {})
        sorted_keys = sorted(int(k) for k in levels.keys())
        baseline_key = str(sorted_keys[0]) if sorted_keys else "1"
        highest_key = str(sorted_keys[-1]) if sorted_keys else "50"

        p50_at_1 = levels.get(baseline_key, {}).get("p50_ms", 0.0)
        p50_at_high = levels.get(highest_key, {}).get("p50_ms", 0.0)
        tput_at_1 = levels.get(baseline_key, {}).get("throughput_rps", 0.0)
        inflection_level = (data.get("inflection") or {}).get("inflection_level")
        inflection_str = str(inflection_level) if inflection_level is not None else "stable"

        rows.append(
            f"| {repo_alias} | {p50_at_1:.1f} | {p50_at_high:.1f} "
            f"| {inflection_str} | {tput_at_1:.1f} |"
        )
    return "\n".join(rows) + "\n"
