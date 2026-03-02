"""
Unit tests for tools/perf-suite/config.py

Story #333: Performance Test Harness with Single-User Baselines
AC1: CLI Entry Point and Configuration - scenario loading and validation.

TDD: These tests were written BEFORE the implementation.
"""

import pytest
import json
import sys
import os

# Add the perf-suite directory to path so we can import from it
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../tools/perf-suite"))


VALID_SCENARIO = {
    "name": "search_semantic_medium",
    "endpoint": "search_code",
    "protocol": "mcp",
    "method": "POST",
    "parameters": {
        "query_text": "authentication logic",
        "repository_alias": "click-global",
        "search_mode": "semantic",
        "limit": 5,
    },
    "repo_alias": "click-global",
    "priority": "highest",
    "warmup_count": 3,
    "measurement_count": 20,
}


class TestScenarioLoading:
    """Tests for loading and parsing scenario JSON files."""

    def test_load_valid_scenario_file(self, tmp_path):
        from config import load_scenarios_from_file

        scenario_file = tmp_path / "test_scenarios.json"
        scenario_file.write_text(json.dumps([VALID_SCENARIO]))

        scenarios = load_scenarios_from_file(str(scenario_file))
        assert len(scenarios) == 1
        assert scenarios[0].name == "search_semantic_medium"

    def test_load_multiple_scenarios(self, tmp_path):
        from config import load_scenarios_from_file

        second = {**VALID_SCENARIO, "name": "search_fts_medium"}
        scenario_file = tmp_path / "test_scenarios.json"
        scenario_file.write_text(json.dumps([VALID_SCENARIO, second]))

        scenarios = load_scenarios_from_file(str(scenario_file))
        assert len(scenarios) == 2

    def test_scenario_defaults_applied(self, tmp_path):
        """warmup_count and measurement_count should have defaults if omitted."""
        from config import load_scenarios_from_file

        minimal = {
            k: v
            for k, v in VALID_SCENARIO.items()
            if k not in ("warmup_count", "measurement_count")
        }
        scenario_file = tmp_path / "test_scenarios.json"
        scenario_file.write_text(json.dumps([minimal]))

        scenarios = load_scenarios_from_file(str(scenario_file))
        assert scenarios[0].warmup_count == 3
        assert scenarios[0].measurement_count == 20

    def test_load_scenarios_from_directory(self, tmp_path):
        """load_scenarios_from_dir loads all .json files in a directory."""
        from config import load_scenarios_from_dir

        f1 = tmp_path / "search_scenarios.json"
        f2 = tmp_path / "scip_scenarios.json"
        f1.write_text(json.dumps([VALID_SCENARIO]))
        f2.write_text(json.dumps([{**VALID_SCENARIO, "name": "scip_callchain"}]))

        scenarios = load_scenarios_from_dir(str(tmp_path))
        assert len(scenarios) == 2

    def test_file_not_found_raises(self):
        from config import load_scenarios_from_file

        with pytest.raises(FileNotFoundError):
            load_scenarios_from_file("/nonexistent/path/scenarios.json")

    def test_malformed_json_raises_clear_error(self, tmp_path):
        from config import load_scenarios_from_file

        bad_file = tmp_path / "bad.json"
        bad_file.write_text("this is not json {{{")

        with pytest.raises(ValueError, match="[Jj][Ss][Oo][Nn]|[Pp]arse|[Mm]alformed"):
            load_scenarios_from_file(str(bad_file))

    def test_non_list_json_raises_clear_error(self, tmp_path):
        """JSON must be an array of scenarios, not a dict."""
        from config import load_scenarios_from_file

        bad_file = tmp_path / "bad.json"
        bad_file.write_text(json.dumps({"name": "single_object"}))

        with pytest.raises(ValueError, match="[Ll]ist|[Aa]rray"):
            load_scenarios_from_file(str(bad_file))


class TestScenarioValidation:
    """Tests for scenario field validation."""

    def test_missing_required_field_raises(self, tmp_path):
        from config import load_scenarios_from_file

        for required_field in ("name", "endpoint", "protocol", "method", "parameters", "repo_alias", "priority"):
            bad_scenario = {k: v for k, v in VALID_SCENARIO.items() if k != required_field}
            scenario_file = tmp_path / f"missing_{required_field}.json"
            scenario_file.write_text(json.dumps([bad_scenario]))

            with pytest.raises(ValueError, match=required_field):
                load_scenarios_from_file(str(scenario_file))

    def test_invalid_protocol_raises(self, tmp_path):
        from config import load_scenarios_from_file

        bad = {**VALID_SCENARIO, "protocol": "websocket"}
        scenario_file = tmp_path / "bad_protocol.json"
        scenario_file.write_text(json.dumps([bad]))

        with pytest.raises(ValueError, match="[Pp]rotocol|mcp|rest"):
            load_scenarios_from_file(str(scenario_file))

    def test_invalid_priority_raises(self, tmp_path):
        from config import load_scenarios_from_file

        bad = {**VALID_SCENARIO, "priority": "critical"}
        scenario_file = tmp_path / "bad_priority.json"
        scenario_file.write_text(json.dumps([bad]))

        with pytest.raises(ValueError, match="[Pp]riority|highest|high|medium"):
            load_scenarios_from_file(str(scenario_file))

    def test_valid_protocols_accepted(self, tmp_path):
        from config import load_scenarios_from_file

        for protocol in ("mcp", "rest"):
            scenario = {**VALID_SCENARIO, "protocol": protocol}
            scenario_file = tmp_path / f"proto_{protocol}.json"
            scenario_file.write_text(json.dumps([scenario]))
            scenarios = load_scenarios_from_file(str(scenario_file))
            assert scenarios[0].protocol == protocol

    def test_valid_priorities_accepted(self, tmp_path):
        from config import load_scenarios_from_file

        for priority in ("highest", "high", "medium"):
            scenario = {**VALID_SCENARIO, "priority": priority}
            scenario_file = tmp_path / f"prio_{priority}.json"
            scenario_file.write_text(json.dumps([scenario]))
            scenarios = load_scenarios_from_file(str(scenario_file))
            assert scenarios[0].priority == priority


class TestScenarioDataclass:
    """Tests for the Scenario dataclass itself."""

    def test_scenario_fields(self, tmp_path):
        from config import load_scenarios_from_file

        scenario_file = tmp_path / "s.json"
        scenario_file.write_text(json.dumps([VALID_SCENARIO]))
        scenarios = load_scenarios_from_file(str(scenario_file))
        s = scenarios[0]

        assert s.name == "search_semantic_medium"
        assert s.endpoint == "search_code"
        assert s.protocol == "mcp"
        assert s.method == "POST"
        assert s.parameters["query_text"] == "authentication logic"
        assert s.repo_alias == "click-global"
        assert s.priority == "highest"
        assert s.warmup_count == 3
        assert s.measurement_count == 20
