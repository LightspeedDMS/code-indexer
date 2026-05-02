"""
Unit tests for LifecycleClaudeCliInvoker (Story #876 Phase B-1 Deliverable 1).

The adapter satisfies the `claude_cli_invoker` callable contract required
by LifecycleBatchRunner:

    claude_cli_invoker(alias: str, repo_path: Path) -> UnifiedResult

Responsibilities:
  - Load the lifecycle_unified.md prompt (packaged under
    src/code_indexer/server/prompts/).
  - Call invoke_claude_cli(repo_path, prompt, 180, 240) to run the Claude CLI
    with Phase 2 timeouts.
  - Parse the raw response via UnifiedResponseParser.parse.
  - Return the UnifiedResult on success.
  - Raise RuntimeError on subprocess failure so that
    LifecycleBatchRunner._run_sub_batch logs the per-repo failure and
    continues with other repos in the same sub-batch.

These tests mock the subprocess-level invoke_claude_cli so they run
deterministically with no external dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.unified_response_parser import UnifiedResult
from code_indexer.server.services.intelligence_cli_invoker import InvocationResult


def _make_default_config_service_mock() -> MagicMock:
    """Return a ConfigService mock that yields default 360/420 lifecycle timeouts.

    The real ConfigService is a module-level singleton.  When other tests in
    the full suite call update_setting() against it they mutate its in-memory
    state, so tests that depend on the default values must use an isolated mock
    instead of the live singleton (cf. test_lifecycle_claude_cli_invoker_config.py).
    """
    lifecycle_cfg = MagicMock()
    lifecycle_cfg.shell_timeout_seconds = 360
    lifecycle_cfg.outer_timeout_seconds = 420

    server_config = MagicMock()
    server_config.lifecycle_analysis_config = lifecycle_cfg

    mock_svc = MagicMock()
    mock_svc.get_config.return_value = server_config
    return mock_svc


# ---------------------------------------------------------------------------
# Shared valid response body (matches the unified prompt schema)
# ---------------------------------------------------------------------------


_VALID_RESPONSE_JSON = json.dumps(
    {
        "description": "A Python service for semantic code search.",
        "lifecycle": {
            "ci_system": "github-actions",
            "deployment_target": "pypi",
            "language_ecosystem": "python/poetry",
            "build_system": "poetry",
            "testing_framework": "pytest",
            "confidence": "high",
        },
    }
)


# ---------------------------------------------------------------------------
# Happy path: invoker returns a UnifiedResult on subprocess success
# ---------------------------------------------------------------------------


def test_invoker_returns_unified_result_on_success(tmp_path: Path) -> None:
    """
    When the dispatcher returns a successful InvocationResult, the adapter
    parses the body via UnifiedResponseParser and returns a UnifiedResult with
    the expected description and lifecycle fields.

    Patches build_dep_map_dispatcher (the external factory used by
    _build_dispatcher) rather than invoke_claude_cli, which is no longer
    called after the Bug #936 refactor to CliDispatcher.dispatch().
    """
    from code_indexer.global_repos.lifecycle_claude_cli_invoker import (
        LifecycleClaudeCliInvoker,
    )

    repo_path = tmp_path / "alias-a"
    repo_path.mkdir()

    # Build the mock config and extract the expected outer timeout so there
    # are no magic numbers in the assertions below.
    mock_config_svc = _make_default_config_service_mock()
    expected_outer_timeout = (
        mock_config_svc.get_config().lifecycle_analysis_config.outer_timeout_seconds
    )

    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch.return_value = InvocationResult(
        success=True,
        output=_VALID_RESPONSE_JSON,
        error="",
        cli_used="claude",
        was_failover=False,
    )

    invoker = LifecycleClaudeCliInvoker()

    with (
        patch(
            "code_indexer.global_repos.lifecycle_claude_cli_invoker.build_dep_map_dispatcher",
            return_value=mock_dispatcher,
        ),
        patch(
            "code_indexer.global_repos.lifecycle_claude_cli_invoker.get_config_service",
            return_value=mock_config_svc,
        ),
    ):
        result = invoker("alias-a", repo_path)

    # -- return type and content -----------------------------------------
    assert isinstance(result, UnifiedResult)
    assert result.description == "A Python service for semantic code search."
    assert result.lifecycle["ci_system"] == "github-actions"
    assert result.lifecycle["confidence"] == "high"

    # -- dispatcher wiring -----------------------------------------------
    assert mock_dispatcher.dispatch.called
    call_kwargs = mock_dispatcher.dispatch.call_args.kwargs
    # cwd must be the repo path as string (subprocess working directory).
    assert call_kwargs.get("cwd") == str(repo_path)
    # outer timeout must match what ConfigService provides.
    assert call_kwargs.get("timeout") == expected_outer_timeout
    # Prompt must be loaded from the packaged lifecycle_unified.md file.
    prompt_used = call_kwargs.get("prompt", "")
    assert prompt_used, "prompt must be non-empty"
    assert "description" in prompt_used
    assert "lifecycle" in prompt_used


# ---------------------------------------------------------------------------
# Prompt content: always-emit v3 guidance (AC-V3-11 compliance)
# ---------------------------------------------------------------------------


def test_prompt_contains_always_emit_v3_guidance() -> None:
    """
    AC-V3-11 compliance: the lifecycle_unified.md prompt MUST instruct
    Claude to always emit all three v3 sections (branching, ci, release)
    using escape values when evidence is absent — never omitting a section.

    This prevents the 'tries' (no-CI Delphi/Lazarus repo) regression where
    Claude omitted the ci section entirely instead of emitting escape values.

    The anti-hallucination rule must NOT say 'OMIT the section entirely' for
    optional sections; instead it must enforce always-emit with escape values.
    """
    from code_indexer.global_repos.lifecycle_claude_cli_invoker import (
        _PROMPT_TEXT as prompt,
    )

    # Each v3 section declared REQUIRED (guards against accidental reversion
    # of the six OPTIONAL -> REQUIRED edits)
    for section in ("branching", "ci", "release"):
        required_marker = f"**lifecycle.{section}** (REQUIRED in v3"
        assert required_marker in prompt, (
            f"section '{section}' not declared REQUIRED in v3 — "
            f"expected marker '{required_marker}' missing"
        )

    # Escape-value summary block intact with all six enum-field entries
    # (including the new default_branch escape from the codex review fix)
    assert "**CRITICAL — enum escape values:**" in prompt
    for field in (
        "branching.default_branch",
        "branching.model",
        "ci.deploy_on",
        "ci.trigger_events",
        "release.versioning",
        "release.artifact_types",
    ):
        assert f"`{field}`:" in prompt, (
            f"escape-value summary missing entry for '{field}'"
        )

    # Anti-hallucination rule explicitly names all three sections
    assert "Always emit all three v3 sections (`branching`, `ci`, `release`)" in prompt
    assert "NEVER omit a section" in prompt

    # Old OPTIONAL language must be gone
    assert (
        "OMIT the section entirely (do not emit a section full of nulls)" not in prompt
    )


# ---------------------------------------------------------------------------
# Failure path: invoker raises RuntimeError on subprocess failure
# ---------------------------------------------------------------------------


def test_invoker_raises_on_cli_failure(tmp_path: Path) -> None:
    """
    When the dispatcher reports failure, the adapter MUST raise RuntimeError
    with a message that preserves the alias and the error detail.
    LifecycleBatchRunner._run_sub_batch logs the exception at ERROR level and
    proceeds with other repos in the sub-batch (per LifecycleBatchRunner contract).
    """
    from code_indexer.global_repos.lifecycle_claude_cli_invoker import (
        LifecycleClaudeCliInvoker,
    )

    repo_path = tmp_path / "alias-b"
    repo_path.mkdir()

    invoker = LifecycleClaudeCliInvoker()

    mock_dispatch_result = MagicMock()
    mock_dispatch_result.success = False
    mock_dispatch_result.error = "Claude CLI timed out after 240s"
    mock_dispatch_result.output = ""

    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch.return_value = mock_dispatch_result

    with (
        patch(
            "code_indexer.global_repos.lifecycle_claude_cli_invoker.build_dep_map_dispatcher",
            return_value=mock_dispatcher,
        ),
        patch(
            "code_indexer.global_repos.lifecycle_claude_cli_invoker.get_config_service",
            return_value=_make_default_config_service_mock(),
        ),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            invoker("alias-b", repo_path)

    # Exception message should name the alias so errors are diagnosable
    # and preserve the upstream error text so operators can see WHY.
    message = str(exc_info.value)
    assert "alias-b" in message
    assert "timed out" in message
