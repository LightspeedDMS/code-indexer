"""
Unit tests for SCIP Self-Healing Service - §-preamble tolerant JSON parsing.

Regression coverage for the pace-maker telemetry preamble bug. On every
cidx-server cluster node pace-maker injects a ``§ ...`` telemetry line at byte 0
of the server's ``claude -p`` stdout. A bare ``json.loads`` on that stdout
raises exactly ``Expecting value: line 1 column 1 (char 0)`` -- identically to
the self-monitoring scanner bug fixed in commit cd755829.

``SCIPSelfHealingService._parse_claude_response`` must route Claude stdout
through the shared ``extract_json_from_llm_response`` helper so the preamble is
stripped before parsing, while STILL failing loudly (MESSI rule #13,
anti-silent-failure) on a genuinely empty/garbage response -- never coercing a
failed parse into a false success.
"""

import json

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.scip_self_healing import (
    ClaudeResponse,
    SCIPSelfHealingService,
)

# The exact pace-maker telemetry preamble shape that breaks bare json.loads.
PACEMAKER_PREAMBLE = "§ △0.0 ◎surg ■other ◇1.0 ↻1"

VALID_HEALING_JSON = json.dumps(
    {
        "status": "progress",
        "actions_taken": ["pip install requests", "retry scip generate"],
        "reasoning": "Installed the missing dependency and re-ran indexing.",
    }
)


@pytest.fixture
def job_manager(tmp_path):
    """Create a BackgroundJobManager for testing."""
    storage_path = tmp_path / "jobs.json"
    return BackgroundJobManager(storage_path=str(storage_path))


@pytest.fixture
def service(job_manager, tmp_path):
    """Create SCIPSelfHealingService instance."""
    return SCIPSelfHealingService(job_manager=job_manager, repo_root=tmp_path)


class TestParseClaudeResponsePreambleTolerant:
    """_parse_claude_response must tolerate pace-maker §-preamble noise."""

    def test_pacemaker_preamble_parses_valid_response(self, service):
        """RED->GREEN: a §-preamble before the JSON must not break parsing."""
        stdout = f"{PACEMAKER_PREAMBLE}\n\n{VALID_HEALING_JSON}".encode("utf-8")

        response = service._parse_claude_response(stdout, "job-1", "backend/")

        assert isinstance(response, ClaudeResponse)
        assert response.status == "progress"
        assert response.actions_taken == [
            "pip install requests",
            "retry scip generate",
        ]
        assert "Installed the missing dependency" in response.reasoning

    def test_code_fence_wrapped_response_parses(self, service):
        """A markdown ```json code fence (with §-preamble) must parse."""
        stdout = (f"{PACEMAKER_PREAMBLE}\n```json\n{VALID_HEALING_JSON}\n```\n").encode(
            "utf-8"
        )

        response = service._parse_claude_response(stdout, "job-2", "backend/")

        assert response.status == "progress"
        assert response.actions_taken == [
            "pip install requests",
            "retry scip generate",
        ]

    def test_leading_warning_line_parses(self, service):
        """A leading ``Warning:`` prose line must be stripped before parsing."""
        stdout = (
            "Warning: no stdin data received within timeout\n"
            f"{PACEMAKER_PREAMBLE}\n"
            f"{VALID_HEALING_JSON}\n"
        ).encode("utf-8")

        response = service._parse_claude_response(stdout, "job-3", "backend/")

        assert response.status == "progress"
        assert response.reasoning.startswith("Installed the missing dependency")

    def test_plain_json_still_parses(self, service):
        """No preamble: behavior unchanged, JSON still parses correctly."""
        stdout = VALID_HEALING_JSON.encode("utf-8")

        response = service._parse_claude_response(stdout, "job-4", "backend/")

        assert response.status == "progress"

    def test_empty_response_fails_loudly_no_false_success(self, service):
        """Anti-silent-failure: an empty response must raise, never succeed."""
        with pytest.raises(json.JSONDecodeError):
            service._parse_claude_response(b"", "job-5", "backend/")

    def test_preamble_only_no_json_fails_loudly(self, service):
        """Only telemetry/preamble noise, no JSON payload -> must raise loudly."""
        stdout = f"{PACEMAKER_PREAMBLE}\n".encode("utf-8")

        with pytest.raises(json.JSONDecodeError):
            service._parse_claude_response(stdout, "job-6", "backend/")

    def test_garbage_response_fails_loudly(self, service):
        """Non-JSON garbage must raise loudly, never coerced to success."""
        stdout = b"this is not json at all"

        with pytest.raises(json.JSONDecodeError):
            service._parse_claude_response(stdout, "job-7", "backend/")
