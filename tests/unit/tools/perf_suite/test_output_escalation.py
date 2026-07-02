"""
Unit tests for escalation output serialization in output.py

Story #334: Concurrency Escalation Tests with Degradation Detection
AC5: Cross-repository comparison support
AC7: Escalation results in JSON output

TDD: These tests were written BEFORE the implementation.
"""

from __future__ import annotations

import json
import sys
import os
from datetime import datetime, timezone


# Add the perf-suite directory to path so we can import from it
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "../../../../tools/perf-suite")
)


def _make_scenario(
    name: str, endpoint: str = "search_code", repo_alias: str = "test-global"
) -> object:
    """Create a minimal Scenario for testing."""
    from config import Scenario

    return Scenario(
        name=name,
        endpoint=endpoint,
        protocol="mcp",
        method="POST",
        parameters={"query_text": "auth"},
        repo_alias=repo_alias,
        priority="highest",
        warmup_count=2,
        measurement_count=5,
    )


def _make_metrics(scenario_name: str, p50: float, error_rate: float = 0.0) -> object:
    """Create a MetricsResult for testing."""
    from metrics import MetricsResult

    return MetricsResult(
        scenario_name=scenario_name,
        p50_ms=p50,
        p95_ms=p50 * 1.5,
        p99_ms=p50 * 2.0,
        throughput_rps=10.0,
        error_rate_pct=error_rate,
        total_requests=5,
        total_errors=0,
        raw_timings=[p50] * 5,
    )


def _make_escalation_result(
    name: str, levels: list, repo_alias: str = "test-global"
) -> object:
    """Create an EscalationResult for testing."""
    from metrics import EscalationResult, InflectionResult

    scenario = _make_scenario(name, repo_alias=repo_alias)
    level_metrics = {level: _make_metrics(name, 100.0 + level * 5) for level in levels}
    inflection = InflectionResult(
        inflection_level=None,
        baseline_p50_ms=100.0 + levels[0] * 5,
        inflection_p50_ms=None,
        baseline_unreliable=False,
    )
    return EscalationResult(
        scenario=scenario,
        level_metrics=level_metrics,
        inflection=inflection,
    )


class TestWriteEscalationResults:
    """Tests for output.write_escalation_results()."""

    def test_creates_raw_metrics_json_file(self, tmp_path):
        """write_escalation_results creates raw_metrics.json in output_dir."""
        from output import write_escalation_results

        results = [_make_escalation_result("scenario_a", [1, 2])]
        started_at = datetime.now(timezone.utc)
        finished_at = datetime.now(timezone.utc)

        output_file = write_escalation_results(
            output_dir=tmp_path,
            results=results,
            started_at=started_at,
            finished_at=finished_at,
            server_url="http://localhost:8000",
            concurrency_levels=[1, 2],
        )

        assert output_file.exists()
        assert output_file.name == "raw_metrics.json"

    def test_output_is_valid_json(self, tmp_path):
        """The output file is valid JSON."""
        from output import write_escalation_results

        results = [_make_escalation_result("scenario_a", [1, 2, 5])]
        started_at = datetime.now(timezone.utc)
        finished_at = datetime.now(timezone.utc)

        output_file = write_escalation_results(
            output_dir=tmp_path,
            results=results,
            started_at=started_at,
            finished_at=finished_at,
            server_url="http://localhost:8000",
            concurrency_levels=[1, 2, 5],
        )

        content = output_file.read_text()
        # Should not raise
        data = json.loads(content)
        assert isinstance(data, dict)

    def test_metadata_includes_concurrency_levels(self, tmp_path):
        """Metadata section includes concurrency_levels list."""
        from output import write_escalation_results

        concurrency_levels = [1, 2, 5, 10]
        results = [_make_escalation_result("scenario_a", concurrency_levels)]
        started_at = datetime.now(timezone.utc)
        finished_at = datetime.now(timezone.utc)

        output_file = write_escalation_results(
            output_dir=tmp_path,
            results=results,
            started_at=started_at,
            finished_at=finished_at,
            server_url="http://localhost:8000",
            concurrency_levels=concurrency_levels,
        )

        data = json.loads(output_file.read_text())
        assert "metadata" in data
        assert data["metadata"]["concurrency_levels"] == concurrency_levels

    def test_metadata_includes_timestamps(self, tmp_path):
        """Metadata includes started_at and finished_at ISO 8601 timestamps."""
        from output import write_escalation_results

        started_at = datetime(2025, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
        finished_at = datetime(2025, 3, 1, 12, 5, 0, tzinfo=timezone.utc)
        results = [_make_escalation_result("scenario_a", [1])]

        output_file = write_escalation_results(
            output_dir=tmp_path,
            results=results,
            started_at=started_at,
            finished_at=finished_at,
            server_url="http://localhost:8000",
            concurrency_levels=[1],
        )

        data = json.loads(output_file.read_text())
        assert "2025-03-01" in data["metadata"]["started_at"]
        assert "2025-03-01" in data["metadata"]["finished_at"]

    def test_server_url_is_sanitized(self, tmp_path):
        """Hostname in server_url is replaced with <server> placeholder."""
        from output import write_escalation_results

        results = [_make_escalation_result("scenario_a", [1])]
        started_at = datetime.now(timezone.utc)
        finished_at = datetime.now(timezone.utc)

        output_file = write_escalation_results(
            output_dir=tmp_path,
            results=results,
            started_at=started_at,
            finished_at=finished_at,
            server_url="http://192.168.60.20:8000",
            concurrency_levels=[1],
        )

        data = json.loads(output_file.read_text())
        assert "192.168.60.20" not in data["metadata"]["server_url"]
        assert "<server>" in data["metadata"]["server_url"]

    def test_scenarios_is_dict_not_list(self, tmp_path):
        """Scenarios in output is a dict keyed by scenario name (not a list)."""
        from output import write_escalation_results

        results = [
            _make_escalation_result("semantic_search_tries", [1, 2]),
            _make_escalation_result("fts_search_flask", [1, 2]),
        ]
        started_at = datetime.now(timezone.utc)
        finished_at = datetime.now(timezone.utc)

        output_file = write_escalation_results(
            output_dir=tmp_path,
            results=results,
            started_at=started_at,
            finished_at=finished_at,
            server_url="http://localhost:8000",
            concurrency_levels=[1, 2],
        )

        data = json.loads(output_file.read_text())
        assert isinstance(data["scenarios"], dict)
        assert "semantic_search_tries" in data["scenarios"]
        assert "fts_search_flask" in data["scenarios"]

    def test_each_scenario_has_levels_dict(self, tmp_path):
        """Each scenario entry has a 'levels' dict keyed by concurrency level."""
        from output import write_escalation_results

        results = [_make_escalation_result("scenario_a", [1, 5, 10])]
        started_at = datetime.now(timezone.utc)
        finished_at = datetime.now(timezone.utc)

        output_file = write_escalation_results(
            output_dir=tmp_path,
            results=results,
            started_at=started_at,
            finished_at=finished_at,
            server_url="http://localhost:8000",
            concurrency_levels=[1, 5, 10],
        )

        data = json.loads(output_file.read_text())
        scenario_data = data["scenarios"]["scenario_a"]
        assert "levels" in scenario_data
        levels = scenario_data["levels"]
        # Keys are strings in JSON
        level_keys = [str(k) for k in levels.keys()]
        assert "1" in level_keys
        assert "5" in level_keys
        assert "10" in level_keys

    def test_each_level_has_p50_p95_p99(self, tmp_path):
        """Each level entry has p50_ms, p95_ms, p99_ms."""
        from output import write_escalation_results

        results = [_make_escalation_result("scenario_a", [1, 2])]
        started_at = datetime.now(timezone.utc)
        finished_at = datetime.now(timezone.utc)

        output_file = write_escalation_results(
            output_dir=tmp_path,
            results=results,
            started_at=started_at,
            finished_at=finished_at,
            server_url="http://localhost:8000",
            concurrency_levels=[1, 2],
        )

        data = json.loads(output_file.read_text())
        level_data = list(data["scenarios"]["scenario_a"]["levels"].values())[0]
        assert "p50_ms" in level_data
        assert "p95_ms" in level_data
        assert "p99_ms" in level_data
        assert "throughput_rps" in level_data
        assert "error_rate_pct" in level_data
        assert "raw_timings" in level_data

    def test_each_scenario_has_inflection_section(self, tmp_path):
        """Each scenario entry has an 'inflection' dict."""
        from output import write_escalation_results

        results = [_make_escalation_result("scenario_a", [1, 2])]
        started_at = datetime.now(timezone.utc)
        finished_at = datetime.now(timezone.utc)

        output_file = write_escalation_results(
            output_dir=tmp_path,
            results=results,
            started_at=started_at,
            finished_at=finished_at,
            server_url="http://localhost:8000",
            concurrency_levels=[1, 2],
        )

        data = json.loads(output_file.read_text())
        scenario_data = data["scenarios"]["scenario_a"]
        assert "inflection" in scenario_data
        inflection = scenario_data["inflection"]
        assert "inflection_level" in inflection
        assert "baseline_p50_ms" in inflection
        assert "baseline_unreliable" in inflection

    def test_each_scenario_has_endpoint_and_repo(self, tmp_path):
        """Each scenario entry includes endpoint and repo_alias for cross-repo comparison."""
        from output import write_escalation_results

        results = [
            _make_escalation_result(
                "semantic_search_tries", [1], repo_alias="tries-global"
            )
        ]
        started_at = datetime.now(timezone.utc)
        finished_at = datetime.now(timezone.utc)

        output_file = write_escalation_results(
            output_dir=tmp_path,
            results=results,
            started_at=started_at,
            finished_at=finished_at,
            server_url="http://localhost:8000",
            concurrency_levels=[1],
        )

        data = json.loads(output_file.read_text())
        scenario_data = data["scenarios"]["semantic_search_tries"]
        assert scenario_data["endpoint"] == "search_code"
        assert scenario_data["repo_alias"] == "tries-global"

    def test_total_scenarios_count_in_metadata(self, tmp_path):
        """Metadata includes total_scenarios count."""
        from output import write_escalation_results

        results = [
            _make_escalation_result("s1", [1]),
            _make_escalation_result("s2", [1]),
            _make_escalation_result("s3", [1]),
        ]
        started_at = datetime.now(timezone.utc)
        finished_at = datetime.now(timezone.utc)

        output_file = write_escalation_results(
            output_dir=tmp_path,
            results=results,
            started_at=started_at,
            finished_at=finished_at,
            server_url="http://localhost:8000",
            concurrency_levels=[1],
        )

        data = json.loads(output_file.read_text())
        assert data["metadata"]["total_scenarios"] == 3


class TestPrintEscalationSummary:
    """Tests for output.print_escalation_summary()."""

    def test_print_summary_does_not_raise(self, capsys):
        """print_escalation_summary runs without error."""
        from output import print_escalation_summary

        results = [_make_escalation_result("scenario_a", [1, 2, 5])]
        # Should not raise
        print_escalation_summary(results)

    def test_print_summary_includes_scenario_name(self, capsys):
        """Output includes the scenario name."""
        from output import print_escalation_summary

        results = [_make_escalation_result("semantic_search_tries", [1, 2])]
        print_escalation_summary(results)

        captured = capsys.readouterr()
        assert "semantic_search_tries" in captured.out

    def test_print_summary_includes_inflection_info(self, capsys):
        """Output mentions inflection or stability status."""
        from output import print_escalation_summary
        from metrics import EscalationResult, InflectionResult

        scenario = _make_scenario("test_inflection")
        inflection = InflectionResult(
            inflection_level=10,
            baseline_p50_ms=100.0,
            inflection_p50_ms=210.0,
            baseline_unreliable=False,
        )
        result = EscalationResult(
            scenario=scenario,
            level_metrics={
                1: _make_metrics("test_inflection", 100.0),
                10: _make_metrics("test_inflection", 210.0),
            },
            inflection=inflection,
        )

        print_escalation_summary([result])
        captured = capsys.readouterr()
        # Should mention the inflection level or some degradation info
        assert (
            "10" in captured.out
            or "inflection" in captured.out.lower()
            or "degradat" in captured.out.lower()
        )
