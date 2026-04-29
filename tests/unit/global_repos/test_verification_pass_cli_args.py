"""
Unit tests for Story #724 v2 verification pass — CLI arguments contract.

Verifies the subprocess cmd and prompt built by invoke_verification_pass:
  - contains --dangerously-skip-permissions
  - config.fact_check_timeout_seconds flows to subprocess timeout kwarg
  - does NOT contain --output-format json (v1 flag, nuked in v2)
  - prompt appends _build_file_based_instructions output:
      FILE_EDIT_COMPLETE sentinel string, temp file path, and repo alias

6 tests across 2 classes (TestCmdFlags: 3, TestPromptContent: 3).
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.dependency_map_analyzer import (
    DependencyMapAnalyzer,
    VerificationFailed,
    _VERIFICATION_SEMAPHORE_STATE,
)

# Named constants — no magic numbers in test bodies
_SENTINEL = "FILE_EDIT_COMPLETE"
_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_MAX_CONCURRENT_CLI = 2
_DEFAULT_MAX_TURNS = 30
_PROMPT_PREVIEW_CHARS = 300


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_semaphore():
    _VERIFICATION_SEMAPHORE_STATE.clear()
    yield
    _VERIFICATION_SEMAPHORE_STATE.clear()


@pytest.fixture()
def analyzer(tmp_path):
    repos = tmp_path / "golden-repos"
    repos.mkdir(parents=True, exist_ok=True)
    return DependencyMapAnalyzer(
        golden_repos_root=repos,
        cidx_meta_path=tmp_path / "cidx-meta",
        pass_timeout=_DEFAULT_TIMEOUT_SECONDS,
        analysis_model="opus",
    )


@pytest.fixture()
def cfg():
    c = MagicMock()
    c.fact_check_timeout_seconds = _DEFAULT_TIMEOUT_SECONDS
    c.max_concurrent_claude_cli = _DEFAULT_MAX_CONCURRENT_CLI
    c.dependency_map_delta_max_turns = _DEFAULT_MAX_TURNS
    return c


@pytest.fixture()
def capture(analyzer, cfg, tmp_path):
    """Factory fixture: call capture(cfg_override=None) -> (cmds, prompts, temp_file, call_kwargs).

    Patches subprocess.run inside ClaudeInvoker to record every cmd, the embedded
    prompt (from cmd[3]), and the kwargs; both attempts exhaust via TimeoutExpired
    causing VerificationFailed, which is expected here — the fixture is a
    capture-only helper, not a success-path test harness.
    """

    def _run(cfg_override=None) -> tuple:
        active_cfg = cfg_override if cfg_override is not None else cfg
        temp_file = tmp_path / "domain.md"
        temp_file.write_text("# Content\n")
        cmds: list = []
        prompts: list = []
        call_kwargs: list = []

        def fake_run(cmd, input=None, **kwargs):
            cmds.append(list(cmd))
            # ClaudeInvoker embeds the prompt inside cmd[3] (the shell command
            # string passed to `script -q -c <cmd> /dev/null`), not via stdin.
            prompts.append(cmd[3] if len(cmd) > 3 else "")
            call_kwargs.append(dict(kwargs))
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=_DEFAULT_TIMEOUT_SECONDS)

        try:
            with patch(
                "code_indexer.server.services.claude_invoker.subprocess.run",
                side_effect=fake_run,
            ):
                analyzer.invoke_verification_pass(
                    temp_file, [{"alias": "r1"}], active_cfg
                )
        except VerificationFailed:
            # Expected: capture helper exhausts both retry attempts intentionally
            pass

        return cmds, prompts, temp_file, call_kwargs

    return _run


# ---------------------------------------------------------------------------
# Tests — cmd flags
# ---------------------------------------------------------------------------


class TestCmdFlags:
    """--dangerously-skip-permissions, config timeout propagation, absence of --output-format json."""

    def test_cmd_includes_dangerously_skip_permissions(self, capture):
        """Every cmd must include --dangerously-skip-permissions."""
        cmds, _, _, _ = capture()
        assert cmds, "No subprocess.run calls were captured"
        for cmd in cmds:
            assert any("--dangerously-skip-permissions" in arg for arg in cmd), (
                f"--dangerously-skip-permissions missing from cmd: {cmd}"
            )

    def test_cmd_timeout_from_config(self, capture, cfg):
        """fact_check_timeout_seconds from config must reach subprocess timeout kwarg.

        Pass 1 now routes through CliDispatcher -> ClaudeInvoker which passes
        config.fact_check_timeout_seconds as the hard outer subprocess timeout.
        ClaudeInvoker does not use --max-turns; it uses a soft inner shell timeout.
        """
        _SENTINEL_TIMEOUT = 77  # distinctive value to prove config is read, not hardcoded
        cfg.fact_check_timeout_seconds = _SENTINEL_TIMEOUT
        cmds, _, _, call_kwargs = capture(cfg_override=cfg)
        assert cmds, "No subprocess.run calls were captured"
        for kw in call_kwargs:
            assert "timeout" in kw, (
                f"subprocess.run was not called with timeout kwarg: {kw}"
            )
            assert kw["timeout"] == cfg.fact_check_timeout_seconds, (
                f"Expected subprocess timeout={cfg.fact_check_timeout_seconds} from "
                f"fact_check_timeout_seconds, got {kw['timeout']!r}"
            )

    def test_cmd_does_NOT_include_output_format_json(self, capture):
        """--output-format must be absent (v1 flag removed in v2)."""
        cmds, _, _, _ = capture()
        assert cmds, "No subprocess.run calls were captured"
        for cmd in cmds:
            assert not any("--output-format" in arg for arg in cmd), (
                f"--output-format found in cmd (must be absent in v2): {cmd}"
            )


# ---------------------------------------------------------------------------
# Tests — prompt content
# ---------------------------------------------------------------------------


class TestPromptContent:
    """Prompt sent to subprocess contains _build_file_based_instructions output."""

    def test_prompt_contains_file_edit_complete_sentinel(self, capture):
        """Prompt must reference FILE_EDIT_COMPLETE (from _build_file_based_instructions)."""
        _, prompts, _, _ = capture()
        assert prompts, "No prompts were captured"
        prompt = prompts[0]
        assert _SENTINEL in prompt, (
            f"FILE_EDIT_COMPLETE not in prompt — _build_file_based_instructions not appended. "
            f"Prompt start: {prompt[:_PROMPT_PREVIEW_CHARS]}"
        )

    def test_prompt_contains_temp_file_path(self, capture):
        """Prompt must contain the absolute temp file path."""
        _, prompts, temp_file, _ = capture()
        assert prompts, "No prompts were captured"
        prompt = prompts[0]
        assert str(temp_file) in prompt, (
            f"Temp file path {temp_file} not in prompt. "
            f"Prompt start: {prompt[:_PROMPT_PREVIEW_CHARS]}"
        )

    def test_prompt_contains_repo_alias(self, capture):
        """Prompt must mention the repo alias passed in repo_list."""
        _, prompts, _, _ = capture()
        assert prompts, "No prompts were captured"
        prompt = prompts[0]
        assert "r1" in prompt, (
            f"Repo alias 'r1' not found in prompt. "
            f"Prompt start: {prompt[:_PROMPT_PREVIEW_CHARS]}"
        )
