"""
Unit tests for Story #349 — File-Based Output for Pass 1 Dependency Map Synthesis.

Tests verify:
1. Prompt contains file-output instructions (path, validation command)
2. Output format instructions appear BEFORE repo descriptions in prompt (primacy)
3. File-based read path (file exists with valid JSON → returns domains, file deleted)
4. Stdout fallback when file missing (falls back to _extract_json)
5. File cleanup after successful parse
6. Retry logic triggers when both file and stdout fail
7. Error diagnostics include file path info
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer


# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def analyzer(tmp_path):
    """Create a DependencyMapAnalyzer instance for testing."""
    return DependencyMapAnalyzer(
        golden_repos_root=tmp_path,
        cidx_meta_path=tmp_path / "cidx-meta",
        pass_timeout=600,
    )


@pytest.fixture
def staging_dir(tmp_path):
    """Create and return the staging directory."""
    d = tmp_path / "cidx-meta" / "dependency-map.staging"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def repo_list():
    """Standard repo list for tests."""
    return [
        {
            "alias": "auth-service",
            "description_summary": "Auth service",
            "clone_path": "/golden-repos/auth-service",
            "file_count": 42,
            "total_bytes": 1024 * 1024,
        },
        {
            "alias": "web-app",
            "description_summary": "Web application",
            "clone_path": "/golden-repos/web-app",
            "file_count": 100,
            "total_bytes": 2 * 1024 * 1024,
        },
    ]


@pytest.fixture
def repo_descriptions():
    """Standard repo descriptions for tests."""
    return {
        "auth-service": "Auth service description",
        "web-app": "Web app description",
    }


@pytest.fixture
def valid_domain_json(repo_list):
    """Valid JSON domain list matching repo_list aliases."""
    return json.dumps(
        [
            {
                "name": "authentication",
                "description": "Auth domain",
                "participating_repos": ["auth-service", "web-app"],
                "repo_paths": {
                    "auth-service": "/golden-repos/auth-service",
                    "web-app": "/golden-repos/web-app",
                },
                "evidence": "Shared auth tokens",
            }
        ]
    )


# ─── AC1: Prompt structure — file-output instructions ────────────────────────


class TestPass1PromptFileOutputInstructions:
    """AC1: The prompt instructs Claude to write JSON to a file and validate it."""

    def _capture_prompt(self, analyzer, staging_dir, repo_descriptions, repo_list, valid_domain_json):
        """Helper: run pass 1 and capture the prompt sent to Claude CLI."""
        captured_prompt = {}

        def fake_invoke(prompt, timeout, max_turns, allowed_tools=None, **kwargs):
            captured_prompt["value"] = prompt
            return valid_domain_json

        analyzer._invoke_claude_cli = fake_invoke
        analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list, max_turns=10)
        return captured_prompt["value"]

    def test_prompt_contains_pass1_file_path_relative(
        self, analyzer, staging_dir, repo_descriptions, repo_list, valid_domain_json
    ):
        """Prompt must include the relative path from cwd for the output file."""
        prompt = self._capture_prompt(analyzer, staging_dir, repo_descriptions, repo_list, valid_domain_json)

        # The relative path from cwd (golden-repos root) to the staging file
        assert "cidx-meta/dependency-map.staging/pass1_domains.json" in prompt

    def test_prompt_contains_pass1_file_path_absolute(
        self, analyzer, staging_dir, repo_descriptions, repo_list, valid_domain_json
    ):
        """Prompt must include the absolute path to the output file."""
        prompt = self._capture_prompt(analyzer, staging_dir, repo_descriptions, repo_list, valid_domain_json)

        expected_abs_path = str(staging_dir / "pass1_domains.json")
        assert expected_abs_path in prompt

    def test_prompt_contains_validation_command(
        self, analyzer, staging_dir, repo_descriptions, repo_list, valid_domain_json
    ):
        """Prompt must instruct Claude to validate with python3 -m json.tool."""
        prompt = self._capture_prompt(analyzer, staging_dir, repo_descriptions, repo_list, valid_domain_json)

        assert "python3 -m json.tool" in prompt

    def test_prompt_instructs_not_to_output_to_stdout(
        self, analyzer, staging_dir, repo_descriptions, repo_list, valid_domain_json
    ):
        """Prompt must explicitly say NOT to output JSON to stdout."""
        prompt = self._capture_prompt(analyzer, staging_dir, repo_descriptions, repo_list, valid_domain_json)

        # Must contain some form of "do not output to stdout" instruction
        prompt_lower = prompt.lower()
        assert "do not output" in prompt_lower or "not output" in prompt_lower or "only write" in prompt_lower

    def test_prompt_instructs_self_correction_on_validation_failure(
        self, analyzer, staging_dir, repo_descriptions, repo_list, valid_domain_json
    ):
        """Prompt must instruct Claude to fix errors and re-validate until valid."""
        prompt = self._capture_prompt(analyzer, staging_dir, repo_descriptions, repo_list, valid_domain_json)

        prompt_lower = prompt.lower()
        # Must contain some instruction about fixing/re-validating
        has_fix_instruction = (
            "fix" in prompt_lower
            or "re-validate" in prompt_lower
            or "revalidate" in prompt_lower
            or "retry" in prompt_lower
        )
        assert has_fix_instruction, f"Prompt missing fix/re-validate instruction. Prompt snippet: {prompt[200:400]}"


# ─── AC1: Prompt structure — output format BEFORE repo descriptions ───────────


class TestPass1PromptPrimacy:
    """AC1: Output format instructions must appear BEFORE repo descriptions (primacy/recency)."""

    def _capture_prompt(self, analyzer, staging_dir, repo_descriptions, repo_list, valid_domain_json):
        """Helper: run pass 1 and capture the prompt sent to Claude CLI."""
        captured_prompt = {}

        def fake_invoke(prompt, timeout, max_turns, allowed_tools=None, **kwargs):
            captured_prompt["value"] = prompt
            return valid_domain_json

        analyzer._invoke_claude_cli = fake_invoke
        analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list, max_turns=10)
        return captured_prompt["value"]

    def test_file_output_instructions_appear_before_repo_descriptions(
        self, analyzer, staging_dir, repo_descriptions, repo_list, valid_domain_json
    ):
        """Output format + file instructions must appear before the repo descriptions section."""
        prompt = self._capture_prompt(analyzer, staging_dir, repo_descriptions, repo_list, valid_domain_json)

        file_instruction_pos = prompt.find("pass1_domains.json")
        repo_desc_section_pos = prompt.find("## Repository Descriptions")

        assert file_instruction_pos != -1, "File instruction not found in prompt"
        assert repo_desc_section_pos != -1, "Repo descriptions section not found in prompt"
        assert file_instruction_pos < repo_desc_section_pos, (
            f"File output instruction (pos {file_instruction_pos}) must appear "
            f"BEFORE repo descriptions (pos {repo_desc_section_pos})"
        )

    def test_json_schema_appears_before_repo_descriptions(
        self, analyzer, staging_dir, repo_descriptions, repo_list, valid_domain_json
    ):
        """JSON schema must appear before repo descriptions."""
        prompt = self._capture_prompt(analyzer, staging_dir, repo_descriptions, repo_list, valid_domain_json)

        # JSON schema contains the array example or field names
        schema_pos = prompt.find("participating_repos")
        repo_desc_section_pos = prompt.find("## Repository Descriptions")

        assert schema_pos != -1, "JSON schema ('participating_repos') not found in prompt"
        assert repo_desc_section_pos != -1, "Repo descriptions section not found in prompt"
        assert schema_pos < repo_desc_section_pos, (
            f"JSON schema (pos {schema_pos}) must appear "
            f"BEFORE repo descriptions (pos {repo_desc_section_pos})"
        )


# ─── AC2: File-based read path ───────────────────────────────────────────────


class TestPass1FileBasedReadPath:
    """AC2: After Claude CLI returns, analyzer reads JSON from the output file."""

    def test_reads_domains_from_file_when_file_exists(
        self, analyzer, staging_dir, repo_list, repo_descriptions, valid_domain_json
    ):
        """When pass1_domains.json exists, domains are read from the file."""
        pass1_file = staging_dir / "pass1_domains.json"

        def fake_invoke(prompt, timeout, max_turns, allowed_tools=None, **kwargs):
            # Simulate Claude writing the file
            pass1_file.write_text(valid_domain_json)
            return "Claude processed the task and wrote the file."

        analyzer._invoke_claude_cli = fake_invoke
        result = analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list, max_turns=10)

        assert len(result) == 1
        assert result[0]["name"] == "authentication"

    def test_file_based_path_returns_correct_domain_count(
        self, analyzer, staging_dir, repo_list, repo_descriptions
    ):
        """File-based path correctly returns all domains from the JSON file."""
        two_domain_json = json.dumps(
            [
                {
                    "name": "authentication",
                    "description": "Auth domain",
                    "participating_repos": ["auth-service"],
                    "repo_paths": {"auth-service": "/golden-repos/auth-service"},
                    "evidence": "Auth tokens",
                },
                {
                    "name": "web-frontend",
                    "description": "Web domain",
                    "participating_repos": ["web-app"],
                    "repo_paths": {"web-app": "/golden-repos/web-app"},
                    "evidence": "React components",
                },
            ]
        )

        pass1_file = staging_dir / "pass1_domains.json"

        def fake_invoke(prompt, timeout, max_turns, allowed_tools=None, **kwargs):
            pass1_file.write_text(two_domain_json)
            return "Done."

        analyzer._invoke_claude_cli = fake_invoke
        result = analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list, max_turns=10)

        assert len(result) == 2
        names = {d["name"] for d in result}
        assert "authentication" in names
        assert "web-frontend" in names


# ─── AC2 + AC5: File cleanup after successful parse ──────────────────────────


class TestPass1FileCleanup:
    """AC5: pass1_domains.json must be deleted after successful parse."""

    def test_file_deleted_after_successful_parse(
        self, analyzer, staging_dir, repo_list, repo_descriptions, valid_domain_json
    ):
        """pass1_domains.json is deleted after successful read and parse."""
        pass1_file = staging_dir / "pass1_domains.json"

        def fake_invoke(prompt, timeout, max_turns, allowed_tools=None, **kwargs):
            pass1_file.write_text(valid_domain_json)
            return "Done."

        analyzer._invoke_claude_cli = fake_invoke
        analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list, max_turns=10)

        assert not pass1_file.exists(), "pass1_domains.json must be deleted after successful parse"

    def test_file_deleted_even_after_invalid_json_in_file(
        self, analyzer, staging_dir, repo_list, repo_descriptions
    ):
        """pass1_domains.json is deleted even if the file contains invalid JSON (fallback to stdout)."""
        pass1_file = staging_dir / "pass1_domains.json"

        # Create a valid stdout response as fallback
        fallback_json = json.dumps(
            [
                {
                    "name": "auth",
                    "description": "Auth domain",
                    "participating_repos": ["auth-service"],
                    "repo_paths": {"auth-service": "/golden-repos/auth-service"},
                }
            ]
        )

        def fake_invoke(prompt, timeout, max_turns, allowed_tools=None, **kwargs):
            # Write invalid JSON to file
            pass1_file.write_text("this is not valid json {{{")
            return fallback_json  # Valid JSON in stdout as fallback

        analyzer._invoke_claude_cli = fake_invoke
        # Should fall through to stdout extraction and not crash
        # (file has invalid JSON → fallback to stdout)
        try:
            analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list, max_turns=10)
        except Exception:
            pass  # May or may not succeed depending on retry behavior

        # File must be deleted regardless
        assert not pass1_file.exists(), "pass1_domains.json must be deleted even after invalid JSON parse"


# ─── AC2: Stdout fallback ────────────────────────────────────────────────────


class TestPass1StdoutFallback:
    """AC2: When pass1_domains.json does not exist, fall back to stdout extraction."""

    def test_falls_back_to_stdout_when_file_missing(
        self, analyzer, staging_dir, repo_list, repo_descriptions, valid_domain_json
    ):
        """When file is not written, falls back to _extract_json on stdout."""
        def fake_invoke(prompt, timeout, max_turns, allowed_tools=None, **kwargs):
            # Does NOT write a file — stdout has the JSON
            return valid_domain_json

        analyzer._invoke_claude_cli = fake_invoke
        result = analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list, max_turns=10)

        assert len(result) == 1
        assert result[0]["name"] == "authentication"

    def test_stdout_fallback_logs_warning(
        self, analyzer, staging_dir, repo_list, repo_descriptions, valid_domain_json, caplog
    ):
        """When falling back to stdout extraction, a WARNING must be logged."""
        import logging

        def fake_invoke(prompt, timeout, max_turns, allowed_tools=None, **kwargs):
            return valid_domain_json

        analyzer._invoke_claude_cli = fake_invoke

        with caplog.at_level(logging.WARNING, logger="code_indexer.global_repos.dependency_map_analyzer"):
            analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list, max_turns=10)

        # Should have a warning about fallback to stdout
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        has_fallback_warning = any(
            "fallback" in msg.lower() or "stdout" in msg.lower()
            for msg in warning_messages
        )
        assert has_fallback_warning, f"Expected fallback warning, got warnings: {warning_messages}"

    def test_file_read_logs_info(
        self, analyzer, staging_dir, repo_list, repo_descriptions, valid_domain_json, caplog
    ):
        """When file-based read succeeds, logs INFO with file path and size."""
        import logging

        pass1_file = staging_dir / "pass1_domains.json"

        def fake_invoke(prompt, timeout, max_turns, allowed_tools=None, **kwargs):
            pass1_file.write_text(valid_domain_json)
            return "Done."

        analyzer._invoke_claude_cli = fake_invoke

        with caplog.at_level(logging.INFO, logger="code_indexer.global_repos.dependency_map_analyzer"):
            analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list, max_turns=10)

        info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
        has_file_info = any(
            "pass1" in msg.lower() and ("file" in msg.lower() or "bytes" in msg.lower())
            for msg in info_messages
        )
        assert has_file_info, f"Expected INFO about file read, got info messages: {info_messages}"


# ─── AC3 + AC4: Retry logic ──────────────────────────────────────────────────


class TestPass1RetryLogic:
    """AC3 + AC4: Retry logic when both file and stdout fail."""

    def test_retry_triggered_when_file_missing_and_stdout_unparseable(
        self, analyzer, staging_dir, repo_list, repo_descriptions, valid_domain_json
    ):
        """When first attempt has no file and no parseable stdout, a retry occurs."""
        call_count = [0]

        def fake_invoke(prompt, timeout, max_turns, allowed_tools=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First attempt: no file, bad stdout
                return "I analyzed the repositories and found the following domains: authentication, web-frontend"
            else:
                # Second attempt (retry): Claude writes the file
                pass1_file = staging_dir / "pass1_domains.json"
                pass1_file.write_text(valid_domain_json)
                return "Done."

        analyzer._invoke_claude_cli = fake_invoke
        result = analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list, max_turns=10)

        assert call_count[0] == 2, f"Expected 2 invocations (initial + retry), got {call_count[0]}"
        assert len(result) >= 1

    def test_retry_prompt_contains_file_reminder(
        self, analyzer, staging_dir, repo_list, repo_descriptions, valid_domain_json
    ):
        """Retry prompt must include explicit reminder to write the file."""
        prompts_received = []

        def fake_invoke(prompt, timeout, max_turns, allowed_tools=None, **kwargs):
            prompts_received.append(prompt)
            if len(prompts_received) == 1:
                # First attempt fails
                return "Narrative response without JSON"
            else:
                # Retry: write the file
                pass1_file = staging_dir / "pass1_domains.json"
                pass1_file.write_text(valid_domain_json)
                return "Done."

        analyzer._invoke_claude_cli = fake_invoke
        analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list, max_turns=10)

        assert len(prompts_received) == 2, f"Expected 2 prompts, got {len(prompts_received)}"
        retry_prompt = prompts_received[1]
        retry_lower = retry_prompt.lower()
        # Retry prompt must emphasize the file requirement
        has_file_reminder = (
            "pass1_domains.json" in retry_prompt
            or "must write" in retry_lower
            or "write" in retry_lower
        )
        assert has_file_reminder, f"Retry prompt missing file reminder. Got: {retry_prompt[:300]}"

    def test_raises_runtime_error_when_both_attempts_fail(
        self, analyzer, staging_dir, repo_list, repo_descriptions
    ):
        """RuntimeError is raised when both attempts fail (no file, no parseable stdout)."""
        def fake_invoke(prompt, timeout, max_turns, allowed_tools=None, **kwargs):
            return "I analyzed the repositories and here are my findings: lots of interesting code."

        analyzer._invoke_claude_cli = fake_invoke

        with pytest.raises(RuntimeError) as exc_info:
            analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list, max_turns=10)

        error_msg = str(exc_info.value).lower()
        assert "pass 1" in error_msg, f"Error must mention 'Pass 1'. Got: {exc_info.value}"

    def test_error_message_includes_file_path_info(
        self, analyzer, staging_dir, repo_list, repo_descriptions
    ):
        """RuntimeError message must include diagnostic info about the file path checked."""
        def fake_invoke(prompt, timeout, max_turns, allowed_tools=None, **kwargs):
            return "Narrative response without valid JSON"

        analyzer._invoke_claude_cli = fake_invoke

        with pytest.raises(RuntimeError) as exc_info:
            analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list, max_turns=10)

        error_msg = str(exc_info.value)
        # Error must contain the file path for diagnostics
        assert "pass1_domains.json" in error_msg, (
            f"Error must include file path. Got: {error_msg}"
        )

    def test_retry_reads_file_on_second_attempt(
        self, analyzer, staging_dir, repo_list, repo_descriptions, valid_domain_json
    ):
        """On retry, if Claude writes the file, result is read from the file."""
        call_count = [0]

        def fake_invoke(prompt, timeout, max_turns, allowed_tools=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return "Narrative only, no JSON"
            else:
                pass1_file = staging_dir / "pass1_domains.json"
                pass1_file.write_text(valid_domain_json)
                return "File written."

        analyzer._invoke_claude_cli = fake_invoke
        result = analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list, max_turns=10)

        assert len(result) == 1
        assert result[0]["name"] == "authentication"
        # File must be cleaned up after retry read
        assert not (staging_dir / "pass1_domains.json").exists()

    def test_no_extra_retries_beyond_one(
        self, analyzer, staging_dir, repo_list, repo_descriptions
    ):
        """Only one retry is allowed — does not loop indefinitely."""
        call_count = [0]

        def fake_invoke(prompt, timeout, max_turns, allowed_tools=None, **kwargs):
            call_count[0] += 1
            return "No JSON here"

        analyzer._invoke_claude_cli = fake_invoke

        with pytest.raises(RuntimeError):
            analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list, max_turns=10)

        # Must be exactly 2 calls (first attempt + one retry)
        assert call_count[0] == 2, f"Expected exactly 2 calls, got {call_count[0]}"

    def test_stale_pass1_file_deleted_before_retry(
        self, analyzer, staging_dir, repo_list, repo_descriptions, valid_domain_json
    ):
        """A stale pass1_domains.json from first attempt is deleted before retry."""
        call_count = [0]

        def fake_invoke(prompt, timeout, max_turns, allowed_tools=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First attempt: write invalid JSON (will fail to parse)
                pass1_file = staging_dir / "pass1_domains.json"
                pass1_file.write_text("INVALID JSON CONTENT {{{")
                return "Done with invalid file."
            else:
                # Retry: write valid JSON
                pass1_file = staging_dir / "pass1_domains.json"
                pass1_file.write_text(valid_domain_json)
                return "Done."

        analyzer._invoke_claude_cli = fake_invoke
        result = analyzer.run_pass_1_synthesis(staging_dir, repo_descriptions, repo_list, max_turns=10)

        assert call_count[0] == 2
        assert len(result) >= 1
