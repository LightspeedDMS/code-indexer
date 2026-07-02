"""
CLI entry point for standalone performance report generation.

Story #335: Performance Report with Hardware Profile
AC7: Report output and reproduction command.

Usage:
    python report_cli.py --metrics-file raw_metrics.json --output-dir ./reports
"""

from __future__ import annotations

import argparse

from report import generate_report


def main() -> None:
    """CLI entry point for standalone report generation."""
    parser = argparse.ArgumentParser(
        description="Generate a Markdown performance report from raw_metrics.json",
    )
    parser.add_argument(
        "--metrics-file", required=True, help="Path to raw_metrics.json"
    )
    parser.add_argument(
        "--output-dir", required=True, help="Directory to write PERF_REPORT_*.md"
    )
    parser.add_argument(
        "--ssh-host", default=None, help="SSH host for hardware profile"
    )
    parser.add_argument(
        "--ssh-user", default=None, help="SSH user for hardware profile"
    )
    parser.add_argument(
        "--ssh-password", default=None, help="SSH password for hardware profile"
    )
    args = parser.parse_args()

    report_path = generate_report(
        metrics_file=args.metrics_file,
        output_dir=args.output_dir,
        ssh_host=args.ssh_host,
        ssh_user=args.ssh_user,
        ssh_password=args.ssh_password,
    )
    print(f"Report written to: {report_path}")


if __name__ == "__main__":
    main()
