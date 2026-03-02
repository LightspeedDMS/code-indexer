"""
Performance report generation for the CIDX performance test harness.

Story #335: Performance Report with Hardware Profile
AC7: Report output and reproduction command.

Reads raw_metrics.json produced by run_perf_suite.py and generates a
comprehensive Markdown performance report:
  PERF_REPORT_YYYYMMDD.md

Report structure (per AC7):
  1. Hardware Profile
  2. Executive Summary
  3. Test Repositories
  4. Metrics Tables (per scenario, grouped by priority)
  5. Degradation Charts (ASCII bar charts)
  6. Cross-Repo Comparisons
  7. How to Reproduce
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

from hardware import capture_hardware_profile
from report_charts import render_ascii_chart, render_cross_repo_table, render_metrics_table
from report_sections import (
    render_executive_summary,
    render_hardware_section,
)
from sanitizer import post_generation_scan, sanitize_report_content

# Priority display order for grouping metrics tables and charts
_PRIORITY_ORDER = ["highest", "high", "medium"]
_PRIORITY_LABELS = {
    "highest": "HIGHEST Priority",
    "high": "HIGH Priority",
    "medium": "MEDIUM Priority",
}


def _build_reproduction_section(metrics_file: str, cli_args: Optional[dict[str, str]]) -> str:
    """Build the 'How to Reproduce' section with a sanitized CLI command."""
    from sanitizer import sanitize_reproduction_command

    lines = ["## How to Reproduce\n"]
    lines.append("Run the following command to reproduce this performance test:\n")
    lines.append("```bash")

    if cli_args:
        server = cli_args.get("server_url", "http://<staging-server>:8000")
        username = cli_args.get("username", "<admin-user>")
        password = cli_args.get("password", "<password>")
        raw_cmd = (
            f"python run_perf_suite.py "
            f"--server-url {server} "
            f"--username {username} "
            f"--password {password} "
            f"--output-dir ./perf-results"
        )
        cmd = sanitize_reproduction_command(raw_cmd)
    else:
        cmd = (
            "python run_perf_suite.py "
            "--server-url http://<staging-server>:8000 "
            "--username <admin-user> "
            "--password <password> "
            "--output-dir ./perf-results"
        )

    lines.append(cmd)
    lines.append("```")
    lines.append(f"\nRaw metrics file: `{Path(metrics_file).name}`")
    return "\n".join(lines) + "\n"


def _build_test_repositories_section(scenarios: dict[str, Any]) -> str:
    """Build the 'Test Repositories' section listing unique repos."""
    seen: dict[str, str] = {}  # repo_alias -> endpoint (first seen)
    for data in scenarios.values():
        alias = data.get("repo_alias", "unknown")
        endpoint = data.get("endpoint", "unknown")
        if alias not in seen:
            seen[alias] = endpoint

    lines = ["## Test Repositories\n", "| Repository Alias | Endpoint |", "| --- | --- |"]
    for alias in sorted(seen.keys()):
        lines.append(f"| {alias} | {seen[alias]} |")
    return "\n".join(lines) + "\n"


def _build_metrics_and_charts(scenarios: dict[str, Any]) -> str:
    """Build Metrics Tables and Degradation Charts sections grouped by priority."""
    table_sections: list[str] = ["## Metrics Tables\n"]
    chart_sections: list[str] = ["## Degradation Charts\n"]

    for priority in _PRIORITY_ORDER:
        priority_scenarios = {
            name: data
            for name, data in scenarios.items()
            if data.get("priority") == priority
        }
        if not priority_scenarios:
            continue

        label = _PRIORITY_LABELS.get(priority, priority.upper())
        table_sections.append(f"### {label}\n")
        chart_sections.append(f"### {label}\n")

        for name, data in sorted(priority_scenarios.items()):
            table_sections.append(f"#### {name}\n")
            table_sections.append(render_metrics_table(name, data))
            chart_sections.append(f"#### {name}\n")
            chart_sections.append(render_ascii_chart(name, data))

    return "\n".join(table_sections) + "\n" + "\n".join(chart_sections) + "\n"


def _build_cross_repo_section(scenarios: dict[str, Any]) -> str:
    """Build the Cross-Repository Comparisons section."""
    endpoints = sorted({data.get("endpoint", "") for data in scenarios.values()})
    lines = ["## Cross-Repository Comparisons\n"]
    has_content = False
    for endpoint in endpoints:
        table = render_cross_repo_table(endpoint, scenarios)
        if table:
            lines.append(f"### {endpoint}\n")
            lines.append(table)
            has_content = True
    if not has_content:
        lines.append("No endpoints were tested against multiple repositories.\n")
    return "\n".join(lines) + "\n"


def generate_report(
    metrics_file: str,
    output_dir: str,
    ssh_host: Optional[str] = None,
    ssh_user: Optional[str] = None,
    ssh_password: Optional[str] = None,
    cli_args: Optional[dict[str, str]] = None,
) -> str:
    """
    Generate a Markdown performance report from raw_metrics.json.

    Reads the metrics file, renders all sections, applies sanitization,
    runs a post-generation safety scan, and writes PERF_REPORT_YYYYMMDD.md.

    Args:
        metrics_file: Path to raw_metrics.json.
        output_dir: Directory to write the report file.
        ssh_host: Optional SSH host for hardware profiling.
        ssh_user: Optional SSH username for hardware profiling.
        ssh_password: Optional SSH password for hardware profiling.
        cli_args: Optional dict with server_url/username/password for reproduction cmd.

    Returns:
        Absolute path to the written report file.
    """
    raw = Path(metrics_file).read_text(encoding="utf-8")
    data = json.loads(raw)
    metadata: dict[str, Any] = data.get("metadata", {})
    scenarios: dict[str, Any] = data.get("scenarios", {})

    # Derive report date from test run start time
    started_at = metadata.get("started_at", "")
    date_str = started_at[:10].replace("-", "") if started_at else "00000000"

    # Capture hardware profile (gracefully returns None if SSH unavailable)
    hardware_data = capture_hardware_profile(
        ssh_host=ssh_host,
        ssh_user=ssh_user,
        ssh_password=ssh_password,
    )

    # Build all sections
    sections = [
        f"# CIDX Performance Report — {date_str[:4]}-{date_str[4:6]}-{date_str[6:]}\n",
        render_hardware_section(hardware_data),
        render_executive_summary(metadata, scenarios),
        _build_test_repositories_section(scenarios),
        _build_metrics_and_charts(scenarios),
        _build_cross_repo_section(scenarios),
        _build_reproduction_section(metrics_file, cli_args),
    ]

    content = "\n".join(sections)
    content = sanitize_report_content(content)

    # Post-generation safety scan (warns but always writes)
    warnings = post_generation_scan(content)
    for warning in warnings:
        print(f"[SANITIZER WARNING] {warning}", file=sys.stderr)

    output_path = Path(output_dir) / f"PERF_REPORT_{date_str}.md"
    output_path.write_text(content, encoding="utf-8")
    return str(output_path)
