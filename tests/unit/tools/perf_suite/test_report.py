"""
Unit tests for tools/perf-suite report generation (Story #335).

AC1: Hardware profile capture
AC2: Endpoint metrics tables
AC3: ASCII bar charts for degradation profiles
AC4: Cross-repository comparison tables
AC5: Executive summary with key findings
AC6: Information sanitization
AC7: Report output and reproduction command

TDD: These tests are written BEFORE the implementation.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


# Add the perf-suite directory to path so we can import from it
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "../../../../tools/perf-suite")
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

SAMPLE_RAW_METRICS = {
    "metadata": {
        "server_url": "http://<server>:8000",
        "started_at": "2025-01-15T10:00:00+00:00",
        "finished_at": "2025-01-15T10:30:00+00:00",
        "total_scenarios": 4,
        "concurrency_levels": [1, 2, 5, 10, 20, 50],
    },
    "scenarios": {
        "semantic_search_tries": {
            "endpoint": "search_code",
            "repo_alias": "tries-global",
            "priority": "highest",
            "levels": {
                "1": {
                    "p50_ms": 120.0,
                    "p95_ms": 150.0,
                    "p99_ms": 180.0,
                    "throughput_rps": 8.3,
                    "error_rate_pct": 0.0,
                    "total_requests": 20,
                    "total_errors": 0,
                    "raw_timings": [],
                },
                "2": {
                    "p50_ms": 130.0,
                    "p95_ms": 165.0,
                    "p99_ms": 195.0,
                    "throughput_rps": 14.0,
                    "error_rate_pct": 0.0,
                    "total_requests": 40,
                    "total_errors": 0,
                    "raw_timings": [],
                },
                "5": {
                    "p50_ms": 155.0,
                    "p95_ms": 200.0,
                    "p99_ms": 240.0,
                    "throughput_rps": 30.0,
                    "error_rate_pct": 0.0,
                    "total_requests": 100,
                    "total_errors": 0,
                    "raw_timings": [],
                },
                "10": {
                    "p50_ms": 200.0,
                    "p95_ms": 260.0,
                    "p99_ms": 310.0,
                    "throughput_rps": 46.0,
                    "error_rate_pct": 0.0,
                    "total_requests": 200,
                    "total_errors": 0,
                    "raw_timings": [],
                },
                "20": {
                    "p50_ms": 260.0,
                    "p95_ms": 350.0,
                    "p99_ms": 420.0,
                    "throughput_rps": 70.0,
                    "error_rate_pct": 2.0,
                    "total_requests": 400,
                    "total_errors": 8,
                    "raw_timings": [],
                },
                "50": {
                    "p50_ms": 550.0,
                    "p95_ms": 720.0,
                    "p99_ms": 850.0,
                    "throughput_rps": 85.0,
                    "error_rate_pct": 5.0,
                    "total_requests": 1000,
                    "total_errors": 50,
                    "raw_timings": [],
                },
            },
            "inflection": {
                "inflection_level": 20,
                "baseline_p50_ms": 120.0,
                "inflection_p50_ms": 260.0,
                "baseline_unreliable": False,
            },
        },
        "semantic_search_flask": {
            "endpoint": "search_code",
            "repo_alias": "flask-global",
            "priority": "highest",
            "levels": {
                "1": {
                    "p50_ms": 90.0,
                    "p95_ms": 110.0,
                    "p99_ms": 130.0,
                    "throughput_rps": 11.0,
                    "error_rate_pct": 0.0,
                    "total_requests": 20,
                    "total_errors": 0,
                    "raw_timings": [],
                },
                "2": {
                    "p50_ms": 95.0,
                    "p95_ms": 115.0,
                    "p99_ms": 135.0,
                    "throughput_rps": 20.0,
                    "error_rate_pct": 0.0,
                    "total_requests": 40,
                    "total_errors": 0,
                    "raw_timings": [],
                },
                "5": {
                    "p50_ms": 105.0,
                    "p95_ms": 130.0,
                    "p99_ms": 160.0,
                    "throughput_rps": 45.0,
                    "error_rate_pct": 0.0,
                    "total_requests": 100,
                    "total_errors": 0,
                    "raw_timings": [],
                },
                "10": {
                    "p50_ms": 120.0,
                    "p95_ms": 155.0,
                    "p99_ms": 190.0,
                    "throughput_rps": 80.0,
                    "error_rate_pct": 0.0,
                    "total_requests": 200,
                    "total_errors": 0,
                    "raw_timings": [],
                },
                "20": {
                    "p50_ms": 145.0,
                    "p95_ms": 190.0,
                    "p99_ms": 230.0,
                    "throughput_rps": 130.0,
                    "error_rate_pct": 0.0,
                    "total_requests": 400,
                    "total_errors": 0,
                    "raw_timings": [],
                },
                "50": {
                    "p50_ms": 185.0,
                    "p95_ms": 240.0,
                    "p99_ms": 290.0,
                    "throughput_rps": 260.0,
                    "error_rate_pct": 0.0,
                    "total_requests": 1000,
                    "total_errors": 0,
                    "raw_timings": [],
                },
            },
            "inflection": {
                "inflection_level": None,
                "baseline_p50_ms": 90.0,
                "inflection_p50_ms": None,
                "baseline_unreliable": False,
            },
        },
        "fts_search_tries": {
            "endpoint": "search_code",
            "repo_alias": "tries-global",
            "priority": "high",
            "levels": {
                "1": {
                    "p50_ms": 50.0,
                    "p95_ms": 70.0,
                    "p99_ms": 90.0,
                    "throughput_rps": 18.0,
                    "error_rate_pct": 0.0,
                    "total_requests": 20,
                    "total_errors": 0,
                    "raw_timings": [],
                },
                "50": {
                    "p50_ms": 600.0,
                    "p95_ms": 800.0,
                    "p99_ms": 950.0,
                    "throughput_rps": 80.0,
                    "error_rate_pct": 15.0,
                    "total_requests": 1000,
                    "total_errors": 150,
                    "raw_timings": [],
                },
            },
            "inflection": {
                "inflection_level": 50,
                "baseline_p50_ms": 50.0,
                "inflection_p50_ms": 600.0,
                "baseline_unreliable": False,
            },
        },
        "git_blame_flask": {
            "endpoint": "git_blame",
            "repo_alias": "flask-global",
            "priority": "medium",
            "levels": {
                "1": {
                    "p50_ms": 200.0,
                    "p95_ms": 250.0,
                    "p99_ms": 300.0,
                    "throughput_rps": 5.0,
                    "error_rate_pct": 0.0,
                    "total_requests": 20,
                    "total_errors": 0,
                    "raw_timings": [],
                },
                "50": {
                    "p50_ms": 800.0,
                    "p95_ms": 1000.0,
                    "p99_ms": 1200.0,
                    "throughput_rps": 60.0,
                    "error_rate_pct": 20.0,
                    "total_requests": 1000,
                    "total_errors": 200,
                    "raw_timings": [],
                },
            },
            "inflection": {
                "inflection_level": 50,
                "baseline_p50_ms": 200.0,
                "inflection_p50_ms": 800.0,
                "baseline_unreliable": False,
            },
        },
    },
}


def _write_sample_raw_metrics(tmp_path: Path) -> Path:
    """Write sample raw_metrics.json to tmp_path."""
    output_file = tmp_path / "raw_metrics.json"
    output_file.write_text(json.dumps(SAMPLE_RAW_METRICS, indent=2), encoding="utf-8")
    return output_file


# ---------------------------------------------------------------------------
# AC2: Metrics Table Rendering
# ---------------------------------------------------------------------------


class TestMetricsTableRendering:
    """AC2: One Markdown table per scenario with correct format, rounding, and inflection."""

    def test_table_has_required_columns(self):
        from report_sections import render_metrics_table

        scenario_data = SAMPLE_RAW_METRICS["scenarios"]["semantic_search_tries"]
        table = render_metrics_table("semantic_search_tries", scenario_data)

        assert "Concurrency" in table
        assert "p50" in table
        assert "p95" in table
        assert "p99" in table
        assert "Throughput" in table
        assert "Errors" in table

    def test_table_has_one_row_per_level(self):
        from report_sections import render_metrics_table

        scenario_data = SAMPLE_RAW_METRICS["scenarios"]["semantic_search_tries"]
        table = render_metrics_table("semantic_search_tries", scenario_data)

        # 6 concurrency levels → 6 data rows (plus header + separator)
        lines = [line for line in table.splitlines() if line.strip()]
        data_rows = [
            row
            for row in lines
            if "|" in row and "---" not in row and "Concurrency" not in row
        ]
        assert len(data_rows) == 6

    def test_response_times_rounded_to_one_decimal(self):
        from report_sections import render_metrics_table

        scenario_data = SAMPLE_RAW_METRICS["scenarios"]["semantic_search_tries"]
        table = render_metrics_table("semantic_search_tries", scenario_data)

        # 120.0 ms should appear as "120.0" (1 decimal place)
        assert "120.0" in table

    def test_inflection_row_is_bold(self):
        from report_sections import render_metrics_table

        scenario_data = SAMPLE_RAW_METRICS["scenarios"]["semantic_search_tries"]
        # inflection_level = 20
        table = render_metrics_table("semantic_search_tries", scenario_data)

        lines = table.splitlines()
        # Find the row for level 20 — it should have ** markers
        level_20_rows = [
            row
            for row in lines
            if "| 20 |" in row or "| **20**" in row or "**20**" in row
        ]
        assert len(level_20_rows) >= 1
        # Verify bold markers present in the inflection row
        inflection_row = level_20_rows[0]
        assert "**" in inflection_row

    def test_no_inflection_row_without_inflection(self):
        from report_sections import render_metrics_table

        scenario_data = SAMPLE_RAW_METRICS["scenarios"]["semantic_search_flask"]
        # inflection_level = None
        table = render_metrics_table("semantic_search_flask", scenario_data)

        # No row should be bolded (no ** in data rows)
        lines = table.splitlines()
        data_rows = [
            row
            for row in lines
            if "|" in row and "---" not in row and "Concurrency" not in row
        ]
        bold_rows = [r for r in data_rows if "**" in r]
        assert len(bold_rows) == 0

    def test_table_is_valid_markdown(self):
        from report_sections import render_metrics_table

        scenario_data = SAMPLE_RAW_METRICS["scenarios"]["fts_search_tries"]
        table = render_metrics_table("fts_search_tries", scenario_data)

        lines = table.splitlines()
        # First non-empty line is the header
        header_lines = [line for line in lines if line.strip().startswith("|")]
        assert len(header_lines) >= 3  # header, separator, at least 1 data row
        # Second line should be the separator (contains ---)
        separator = header_lines[1]
        assert "---" in separator


# ---------------------------------------------------------------------------
# AC3: ASCII Bar Chart Rendering
# ---------------------------------------------------------------------------


class TestAsciiBarChartRendering:
    """AC3: ASCII horizontal bar charts with correct widths, labels, inflection marker."""

    def test_chart_uses_equals_characters(self):
        from report_sections import render_ascii_chart

        scenario_data = SAMPLE_RAW_METRICS["scenarios"]["semantic_search_tries"]
        chart = render_ascii_chart("semantic_search_tries", scenario_data)

        assert "=" in chart

    def test_chart_labels_include_concurrency_level(self):
        from report_sections import render_ascii_chart

        scenario_data = SAMPLE_RAW_METRICS["scenarios"]["semantic_search_tries"]
        chart = render_ascii_chart("semantic_search_tries", scenario_data)

        # Each level should appear as a label
        assert "  1 |" in chart or "1 |" in chart
        assert " 50 |" in chart or "50 |" in chart

    def test_chart_shows_p50_values(self):
        from report_sections import render_ascii_chart

        scenario_data = SAMPLE_RAW_METRICS["scenarios"]["semantic_search_tries"]
        chart = render_ascii_chart("semantic_search_tries", scenario_data)

        # p50 = 120.0 at level 1 should appear
        assert "120.0" in chart or "120" in chart

    def test_chart_marks_inflection_level(self):
        from report_sections import render_ascii_chart

        scenario_data = SAMPLE_RAW_METRICS["scenarios"]["semantic_search_tries"]
        # inflection_level = 20
        chart = render_ascii_chart("semantic_search_tries", scenario_data)

        assert "inflection" in chart.lower()

    def test_chart_wrapped_in_code_block(self):
        from report_sections import render_ascii_chart

        scenario_data = SAMPLE_RAW_METRICS["scenarios"]["semantic_search_tries"]
        chart = render_ascii_chart("semantic_search_tries", scenario_data)

        assert "```" in chart

    def test_chart_bars_normalized_to_max_width(self):
        from report_sections import render_ascii_chart

        scenario_data = SAMPLE_RAW_METRICS["scenarios"]["semantic_search_tries"]
        chart = render_ascii_chart("semantic_search_tries", scenario_data)

        lines = chart.splitlines()
        bar_lines = [row for row in lines if "=" in row]
        # The longest bar (highest p50 = 550.0) should have bars at max width
        bar_lengths = [row.count("=") for row in bar_lines]
        assert max(bar_lengths) <= 60  # max column width is 60

    def test_chart_stable_scenario_has_no_inflection_marker(self):
        from report_sections import render_ascii_chart

        scenario_data = SAMPLE_RAW_METRICS["scenarios"]["semantic_search_flask"]
        # inflection_level = None
        chart = render_ascii_chart("semantic_search_flask", scenario_data)

        assert "inflection" not in chart.lower()


# ---------------------------------------------------------------------------
# AC4: Cross-Repository Comparison Tables
# ---------------------------------------------------------------------------


class TestCrossRepoComparisonTables:
    """AC4: Comparison tables for each endpoint tested across multiple repos."""

    def test_comparison_table_has_required_columns(self):
        from report_sections import render_cross_repo_table

        # search_code tested against both tries-global and flask-global
        scenarios = SAMPLE_RAW_METRICS["scenarios"]
        table = render_cross_repo_table("search_code", scenarios)

        assert "Repository" in table
        assert "p50" in table
        assert "Inflection" in table
        assert "Throughput" in table

    def test_comparison_table_has_one_row_per_repo(self):
        from report_sections import render_cross_repo_table

        scenarios = SAMPLE_RAW_METRICS["scenarios"]
        table = render_cross_repo_table("search_code", scenarios)

        lines = [row for row in table.splitlines() if row.strip()]
        data_rows = [
            row
            for row in lines
            if "|" in row and "---" not in row and "Repository" not in row
        ]
        # search_code appears in: semantic_search_tries (tries-global),
        #                          semantic_search_flask (flask-global),
        #                          fts_search_tries (tries-global)
        # Unique repos: tries-global (2 scenarios), flask-global (1 scenario)
        # Comparison is per-repo, so 2 rows (one per unique repo)
        assert len(data_rows) >= 2

    def test_comparison_table_only_for_multi_repo_endpoints(self):
        from report_sections import render_cross_repo_table

        scenarios = SAMPLE_RAW_METRICS["scenarios"]
        # git_blame only tested against flask-global → no cross-repo comparison
        table = render_cross_repo_table("git_blame", scenarios)
        # Should return empty string or note since there's only 1 repo
        assert (
            table == ""
            or "only one" in table.lower()
            or table is None
            or len(table.strip()) == 0
        )

    def test_comparison_includes_inflection_level(self):
        from report_sections import render_cross_repo_table

        scenarios = SAMPLE_RAW_METRICS["scenarios"]
        table = render_cross_repo_table("search_code", scenarios)

        # tries-global has inflection at level 20
        # flask-global has no inflection (stable)
        assert "20" in table or "stable" in table.lower()

    def test_comparison_includes_baseline_p50(self):
        from report_sections import render_cross_repo_table

        scenarios = SAMPLE_RAW_METRICS["scenarios"]
        table = render_cross_repo_table("search_code", scenarios)

        # flask-global baseline p50 at level 1 = 90.0 ms
        assert "90.0" in table or "90" in table


# ---------------------------------------------------------------------------
# AC5: Executive Summary
# ---------------------------------------------------------------------------


class TestExecutiveSummary:
    """AC5: Executive summary with headline metrics and key findings."""

    def test_summary_includes_total_scenarios(self):
        from report_sections import render_executive_summary

        metadata = SAMPLE_RAW_METRICS["metadata"]
        scenarios = SAMPLE_RAW_METRICS["scenarios"]
        summary = render_executive_summary(metadata, scenarios)

        assert "4" in summary  # total_scenarios = 4

    def test_summary_includes_date(self):
        from report_sections import render_executive_summary

        metadata = SAMPLE_RAW_METRICS["metadata"]
        scenarios = SAMPLE_RAW_METRICS["scenarios"]
        summary = render_executive_summary(metadata, scenarios)

        assert "2025" in summary

    def test_summary_lists_fastest_endpoints(self):
        from report_sections import render_executive_summary

        metadata = SAMPLE_RAW_METRICS["metadata"]
        scenarios = SAMPLE_RAW_METRICS["scenarios"]
        summary = render_executive_summary(metadata, scenarios)

        # fts_search_tries has baseline p50 = 50.0ms (fastest)
        assert "fastest" in summary.lower() or "Fastest" in summary

    def test_summary_lists_most_sensitive(self):
        from report_sections import render_executive_summary

        metadata = SAMPLE_RAW_METRICS["metadata"]
        scenarios = SAMPLE_RAW_METRICS["scenarios"]
        summary = render_executive_summary(metadata, scenarios)

        # fts_search_tries has inflection at level 50 but only 2 levels tested
        # semantic_search_tries has inflection at level 20
        assert (
            "sensitive" in summary.lower()
            or "inflection" in summary.lower()
            or "degradation" in summary.lower()
        )

    def test_summary_lists_error_prone_endpoints(self):
        from report_sections import render_executive_summary

        metadata = SAMPLE_RAW_METRICS["metadata"]
        scenarios = SAMPLE_RAW_METRICS["scenarios"]
        summary = render_executive_summary(metadata, scenarios)

        # fts_search_tries has 15% errors at level 50 (>10% threshold)
        # git_blame_flask has 20% errors at level 50 (>10% threshold)
        assert "error" in summary.lower()

    def test_summary_has_capacity_recommendation(self):
        from report_sections import render_executive_summary

        metadata = SAMPLE_RAW_METRICS["metadata"]
        scenarios = SAMPLE_RAW_METRICS["scenarios"]
        summary = render_executive_summary(metadata, scenarios)

        assert (
            "recommend" in summary.lower()
            or "capacity" in summary.lower()
            or "concurrency" in summary.lower()
        )


# ---------------------------------------------------------------------------
# AC6: Information Sanitization
# ---------------------------------------------------------------------------


class TestSanitization:
    """AC6: Server URLs/IPs replaced, no passwords or tokens in report."""

    def test_ip_addresses_replaced(self):
        from sanitizer import sanitize_report_content

        content = "Server at 192.168.60.20:8000 responded with 200 OK"
        sanitized = sanitize_report_content(content)

        assert "192.168.60.20" not in sanitized
        assert "<staging-server>" in sanitized or "<server>" in sanitized

    def test_password_literal_replaced(self):
        from sanitizer import sanitize_report_content

        content = "Run with --password=admin123 to reproduce"
        sanitized = sanitize_report_content(content)

        assert "admin123" not in sanitized

    def test_bearer_token_replaced(self):
        from sanitizer import sanitize_report_content

        content = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def"
        sanitized = sanitize_report_content(content)

        assert "eyJhbGciOiJIUzI1NiJ9.abc.def" not in sanitized

    def test_username_in_reproduction_command_replaced(self):
        from sanitizer import sanitize_reproduction_command

        cmd = "python run_perf_suite.py --username admin --password secret123 --server-url http://192.168.60.20:8000"
        sanitized = sanitize_reproduction_command(cmd)

        assert "secret123" not in sanitized
        assert "192.168.60.20" not in sanitized
        assert "<password>" in sanitized
        assert "<staging-server>" in sanitized or "<server>" in sanitized

    def test_post_scan_detects_ip_and_warns(self, capsys):
        from sanitizer import post_generation_scan

        content = "192.168.1.100 is the server address"
        warnings = post_generation_scan(content)

        assert len(warnings) > 0
        assert any(
            "ip" in w.lower() or "address" in w.lower() or "192" in w for w in warnings
        )

    def test_post_scan_detects_bearer_token(self):
        from sanitizer import post_generation_scan

        content = "Use Bearer abc123token for auth"
        warnings = post_generation_scan(content)

        assert len(warnings) > 0

    def test_post_scan_clean_content_returns_no_warnings(self):
        from sanitizer import post_generation_scan

        content = "This is clean content with no sensitive data"
        warnings = post_generation_scan(content)

        assert len(warnings) == 0

    def test_sanitize_server_url_replaces_host(self):
        from sanitizer import sanitize_url_in_content

        content = "Server: http://192.168.60.20:8000/api"
        sanitized = sanitize_url_in_content(content)

        assert "192.168.60.20" not in sanitized


# ---------------------------------------------------------------------------
# AC1: Hardware Profile Section
# ---------------------------------------------------------------------------


class TestHardwareProfileSection:
    """AC1: Hardware profile capture with SSH or placeholder."""

    def test_hardware_section_with_data(self):
        from report_sections import render_hardware_section

        hardware_data = {
            "cpu": "Intel(R) Xeon(R) CPU E5-2676 v3 @ 2.40GHz",
            "cpu_cores": "8",
            "ram": "              total        used        free\nMem:            16G         8G          8G",
            "disk": "sda    disk  100G",
            "os": "Ubuntu 22.04.1 LTS",
            "python_version": "Python 3.11.4",
        }
        section = render_hardware_section(hardware_data)

        assert "CPU" in section or "cpu" in section.lower()
        assert "Intel" in section or "Xeon" in section
        assert "16G" in section or "RAM" in section or "Memory" in section
        assert "Ubuntu" in section

    def test_hardware_section_without_data_shows_placeholder(self):
        from report_sections import render_hardware_section

        section = render_hardware_section(None)

        assert "not captured" in section.lower() or "Hardware: Not captured" in section

    def test_hardware_section_with_empty_dict_shows_placeholder(self):
        from report_sections import render_hardware_section

        section = render_hardware_section({})

        assert (
            "not captured" in section.lower()
            or "n/a" in section.lower()
            or "Hardware" in section
        )

    def test_hardware_capture_skips_gracefully_on_no_ssh(self):
        """When SSH is not configured, capture returns None without raising."""
        from hardware import capture_hardware_profile

        # No SSH params → should return None, not raise
        result = capture_hardware_profile(
            ssh_host=None,
            ssh_user=None,
            ssh_password=None,
        )

        assert result is None

    def test_hardware_capture_returns_dict_structure(self, tmp_path):
        """capture_hardware_profile returns a dict with expected keys or None."""
        from hardware import capture_hardware_profile

        # When no SSH configured, returns None
        result = capture_hardware_profile(
            ssh_host=None,
            ssh_user=None,
            ssh_password=None,
        )
        # None is valid (SSH unavailable)
        assert result is None or isinstance(result, dict)

    def test_hardware_capture_with_invalid_host_returns_none(self):
        """SSH failure on invalid host returns None, does not raise."""
        from hardware import capture_hardware_profile

        result = capture_hardware_profile(
            ssh_host="127.0.0.1",
            ssh_user="nonexistent_user_xyzabc",
            ssh_password="wrong_password_123",
        )

        assert result is None


# ---------------------------------------------------------------------------
# AC7: Full Report Generation
# ---------------------------------------------------------------------------


class TestFullReportGeneration:
    """AC7: Full report reads raw_metrics.json, produces valid Markdown."""

    def test_generate_report_creates_output_file(self, tmp_path):
        from report import generate_report

        metrics_file = _write_sample_raw_metrics(tmp_path)

        report_path = generate_report(
            metrics_file=str(metrics_file),
            output_dir=str(tmp_path),
        )

        assert report_path is not None
        assert Path(report_path).exists()

    def test_report_is_utf8_encoded(self, tmp_path):
        from report import generate_report

        metrics_file = _write_sample_raw_metrics(tmp_path)
        report_path = generate_report(
            metrics_file=str(metrics_file),
            output_dir=str(tmp_path),
        )

        content = Path(report_path).read_text(encoding="utf-8")
        assert len(content) > 0

    def test_report_contains_hardware_section(self, tmp_path):
        from report import generate_report

        metrics_file = _write_sample_raw_metrics(tmp_path)
        report_path = generate_report(
            metrics_file=str(metrics_file),
            output_dir=str(tmp_path),
        )

        content = Path(report_path).read_text(encoding="utf-8")
        assert "Hardware" in content

    def test_report_contains_executive_summary(self, tmp_path):
        from report import generate_report

        metrics_file = _write_sample_raw_metrics(tmp_path)
        report_path = generate_report(
            metrics_file=str(metrics_file),
            output_dir=str(tmp_path),
        )

        content = Path(report_path).read_text(encoding="utf-8")
        assert "Summary" in content or "Executive" in content

    def test_report_contains_metrics_tables(self, tmp_path):
        from report import generate_report

        metrics_file = _write_sample_raw_metrics(tmp_path)
        report_path = generate_report(
            metrics_file=str(metrics_file),
            output_dir=str(tmp_path),
        )

        content = Path(report_path).read_text(encoding="utf-8")
        # Markdown table separator
        assert "---" in content
        assert "|" in content

    def test_report_contains_ascii_charts(self, tmp_path):
        from report import generate_report

        metrics_file = _write_sample_raw_metrics(tmp_path)
        report_path = generate_report(
            metrics_file=str(metrics_file),
            output_dir=str(tmp_path),
        )

        content = Path(report_path).read_text(encoding="utf-8")
        # ASCII charts in code blocks
        assert "```" in content
        assert "=" in content

    def test_report_contains_reproduction_command(self, tmp_path):
        from report import generate_report

        metrics_file = _write_sample_raw_metrics(tmp_path)
        report_path = generate_report(
            metrics_file=str(metrics_file),
            output_dir=str(tmp_path),
        )

        content = Path(report_path).read_text(encoding="utf-8")
        assert "Reproduce" in content or "reproduce" in content

    def test_report_contains_cross_repo_section(self, tmp_path):
        from report import generate_report

        metrics_file = _write_sample_raw_metrics(tmp_path)
        report_path = generate_report(
            metrics_file=str(metrics_file),
            output_dir=str(tmp_path),
        )

        content = Path(report_path).read_text(encoding="utf-8")
        assert "Cross" in content or "Comparison" in content or "Repository" in content

    def test_report_structure_order(self, tmp_path):
        """Report sections appear in the correct order per AC7."""
        from report import generate_report

        metrics_file = _write_sample_raw_metrics(tmp_path)
        report_path = generate_report(
            metrics_file=str(metrics_file),
            output_dir=str(tmp_path),
        )

        content = Path(report_path).read_text(encoding="utf-8")
        hardware_pos = content.find("Hardware")
        summary_pos = (
            content.find("Summary")
            if "Summary" in content
            else content.find("Executive")
        )
        reproduce_pos = content.find("Reproduce")

        # Hardware → Summary → Reproduce (order per AC7)
        assert hardware_pos < summary_pos
        assert summary_pos < reproduce_pos


# ---------------------------------------------------------------------------
# AC7: Report Filename Format
# ---------------------------------------------------------------------------


class TestReportFilenameFormat:
    """AC7: Report filename is PERF_REPORT_YYYYMMDD.md."""

    def test_filename_matches_perf_report_pattern(self, tmp_path):
        from report import generate_report

        metrics_file = _write_sample_raw_metrics(tmp_path)
        report_path = generate_report(
            metrics_file=str(metrics_file),
            output_dir=str(tmp_path),
        )

        filename = Path(report_path).name
        assert filename.startswith("PERF_REPORT_")
        assert filename.endswith(".md")

    def test_filename_date_is_from_test_run(self, tmp_path):
        """Date in filename comes from metadata.started_at in raw_metrics.json."""
        from report import generate_report

        metrics_file = _write_sample_raw_metrics(tmp_path)
        report_path = generate_report(
            metrics_file=str(metrics_file),
            output_dir=str(tmp_path),
        )

        filename = Path(report_path).name
        # started_at = "2025-01-15T10:00:00+00:00" → PERF_REPORT_20250115.md
        assert "20250115" in filename

    def test_filename_date_format_is_yyyymmdd(self, tmp_path):
        """Date part is YYYYMMDD (8 digits)."""
        from report import generate_report

        metrics_file = _write_sample_raw_metrics(tmp_path)
        report_path = generate_report(
            metrics_file=str(metrics_file),
            output_dir=str(tmp_path),
        )

        filename = Path(report_path).name
        # PERF_REPORT_YYYYMMDD.md → date part is 8 digits
        date_part = filename.replace("PERF_REPORT_", "").replace(".md", "")
        assert len(date_part) == 8
        assert date_part.isdigit()
