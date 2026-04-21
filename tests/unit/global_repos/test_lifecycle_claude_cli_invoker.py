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
from typing import Tuple
from unittest.mock import patch

import pytest

from code_indexer.global_repos.unified_response_parser import UnifiedResult


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
    When invoke_claude_cli returns (True, raw_json), the adapter parses
    the body via UnifiedResponseParser and returns a UnifiedResult with
    the expected description and lifecycle fields.
    """
    from code_indexer.global_repos.lifecycle_claude_cli_invoker import (
        LifecycleClaudeCliInvoker,
    )

    repo_path = tmp_path / "alias-a"
    repo_path.mkdir()

    # Capture the arguments passed to invoke_claude_cli so we can assert
    # the timeouts and the prompt non-emptiness in the same test.
    captured: dict = {}

    def _fake_invoke(
        rp: str, prompt: str, shell_timeout: int, outer_timeout: int
    ) -> Tuple[bool, str]:
        captured["repo_path"] = rp
        captured["prompt"] = prompt
        captured["shell_timeout"] = shell_timeout
        captured["outer_timeout"] = outer_timeout
        return True, _VALID_RESPONSE_JSON

    invoker = LifecycleClaudeCliInvoker()

    with patch(
        "code_indexer.global_repos.lifecycle_claude_cli_invoker.invoke_claude_cli",
        side_effect=_fake_invoke,
    ):
        result = invoker("alias-a", repo_path)

    # -- return type and content -----------------------------------------
    assert isinstance(result, UnifiedResult)
    assert result.description == "A Python service for semantic code search."
    assert result.lifecycle["ci_system"] == "github-actions"
    assert result.lifecycle["confidence"] == "high"

    # -- subprocess wiring ----------------------------------------------
    # Repo path must be passed as string (cwd for subprocess).
    assert captured["repo_path"] == str(repo_path)
    # v3 timeouts: 240s inner shell timeout + 300s outer Python timeout.
    assert captured["shell_timeout"] == 240
    assert captured["outer_timeout"] == 300
    # Prompt must be loaded from the packaged lifecycle_unified.md file.
    assert captured["prompt"], "prompt must be non-empty"
    assert "description" in captured["prompt"]
    assert "lifecycle" in captured["prompt"]


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

    # The prompt must explicitly instruct always-emit for all three v3 sections.
    assert "Always emit all three v3 sections" in prompt, (
        "Prompt must instruct Claude to always emit all three v3 sections"
    )

    # The prompt must explicitly forbid omitting sections.
    assert "NEVER omit a section" in prompt, (
        "Prompt must explicitly forbid omitting a section (anti-omit rule)"
    )

    # The old anti-hallucination rule that said OMIT must be gone.
    assert "OMIT the section entirely (do not emit a section full of nulls)" not in prompt, (
        "Old 'OMIT the section entirely' instruction must be replaced with always-emit guidance"
    )


# ---------------------------------------------------------------------------
# Failure path: invoker raises RuntimeError on subprocess failure
# ---------------------------------------------------------------------------


def test_invoker_raises_on_cli_failure(tmp_path: Path) -> None:
    """
    When invoke_claude_cli returns (False, error_msg), the adapter MUST
    raise RuntimeError with a message that preserves the alias and the
    subprocess error.  LifecycleBatchRunner._run_sub_batch logs the
    exception at ERROR level and proceeds with other repos in the
    sub-batch (per LifecycleBatchRunner contract).
    """
    from code_indexer.global_repos.lifecycle_claude_cli_invoker import (
        LifecycleClaudeCliInvoker,
    )

    repo_path = tmp_path / "alias-b"
    repo_path.mkdir()

    def _fail_invoke(
        rp: str, prompt: str, shell_timeout: int, outer_timeout: int
    ) -> Tuple[bool, str]:
        return False, "Claude CLI timed out after 240s"

    invoker = LifecycleClaudeCliInvoker()

    with patch(
        "code_indexer.global_repos.lifecycle_claude_cli_invoker.invoke_claude_cli",
        side_effect=_fail_invoke,
    ):
        with pytest.raises(RuntimeError) as exc_info:
            invoker("alias-b", repo_path)

    # Exception message should name the alias so errors are diagnosable
    # and preserve the upstream error text so operators can see WHY.
    message = str(exc_info.value)
    assert "alias-b" in message
    assert "timed out" in message
