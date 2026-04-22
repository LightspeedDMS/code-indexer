"""
A10 unit tests — MCP self-registration centralization (Story #885).

Tests:
  A10f: test_invoke_claude_cli_triggers_mcp_self_registration
  A10g: test_ensure_registered_cached_across_many_invocations

Both tests FAIL (RED) until A10a lands (MCPSelfRegistrationService.get_instance()
added + call inserted at top of invoke_claude_cli).

AC covered: AC-V4-12, AC-V4-13.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# Minimal valid Claude CLI stdout — text mode, as produced by the script wrapper
_VALID_CLI_OUTPUT = '{"description": "test", "lifecycle": {}}'


# ---------------------------------------------------------------------------
# A10f — invoke_claude_cli calls ensure_registered at subprocess boundary
# ---------------------------------------------------------------------------


def test_invoke_claude_cli_triggers_mcp_self_registration(tmp_path):
    """
    AC-V4-12 — invoke_claude_cli calls MCPSelfRegistrationService.get_instance().ensure_registered()
    before spawning the Claude CLI subprocess.

    Fails RED until A10a adds get_instance() and the centralized call.
    """
    from code_indexer.server.services.mcp_self_registration_service import (
        MCPSelfRegistrationService,
    )

    mock_service = MagicMock(spec=MCPSelfRegistrationService)
    mock_service.ensure_registered.return_value = True

    with patch.object(
        MCPSelfRegistrationService, "get_instance", return_value=mock_service
    ) as mock_get_instance:
        with patch("subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = _VALID_CLI_OUTPUT
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc

            from code_indexer.global_repos.repo_analyzer import invoke_claude_cli

            invoke_claude_cli(
                repo_path=str(tmp_path),
                prompt="test prompt",
                shell_timeout_seconds=30,
                outer_timeout_seconds=60,
            )

        mock_get_instance.assert_called()
        mock_service.ensure_registered.assert_called_once()


# ---------------------------------------------------------------------------
# A10g — cache short-circuits registration work on subsequent invocations
# ---------------------------------------------------------------------------


def test_ensure_registered_cached_across_many_invocations(tmp_path):
    """
    AC-V4-13 — After the first invoke_claude_cli call the MCPSelfRegistrationService
    singleton's _registration_checked flag is True; all subsequent calls skip
    the registration subprocess entirely.

    Uses the REAL MCPSelfRegistrationService via set_instance() / get_instance().
    Only subprocess.run is patched.

    Assertion: calls 2-100 through invoke_claude_cli produce exactly 99 additional
    subprocess invocations (one CLI spawn per call, zero registration subprocesses).
    If registration is NOT cached, additional subprocess calls for --version /
    mcp get will appear, and total_after_all - total_after_first will exceed 99.

    Fails RED until A10a adds get_instance(), set_instance(), and the centralized
    call in invoke_claude_cli.
    """
    from code_indexer.server.services.mcp_self_registration_service import (
        MCPSelfRegistrationService,
    )

    mock_config_manager = MagicMock()
    mock_cred_manager = MagicMock()
    real_service = MCPSelfRegistrationService(
        config_manager=mock_config_manager,
        mcp_credential_manager=mock_cred_manager,
    )
    real_service._registration_checked = False

    original = MCPSelfRegistrationService.get_instance()
    MCPSelfRegistrationService.set_instance(real_service)

    def _fake_run(cmd, *args, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = _VALID_CLI_OUTPUT if (
            isinstance(cmd, list) and cmd and cmd[0] == "script"
        ) else ""
        result.stderr = ""
        return result

    try:
        with patch("subprocess.run", side_effect=_fake_run) as mock_run:
            from code_indexer.global_repos.repo_analyzer import invoke_claude_cli

            # First call: registration work (--version, mcp get) may occur
            invoke_claude_cli(
                repo_path=str(tmp_path),
                prompt="test",
                shell_timeout_seconds=30,
                outer_timeout_seconds=60,
            )
            total_after_first = mock_run.call_count

            # Calls 2-100: only 1 subprocess per call (the CLI spawn), no registration
            for _ in range(99):
                invoke_claude_cli(
                    repo_path=str(tmp_path),
                    prompt="test",
                    shell_timeout_seconds=30,
                    outer_timeout_seconds=60,
                )

            additional_calls = mock_run.call_count - total_after_first
            assert additional_calls == 99, (
                f"Invocations 2-100 produced {additional_calls} subprocess calls "
                f"(expected exactly 99 — one CLI spawn each, zero registration overhead). "
                f"Cache short-circuit is broken."
            )
    finally:
        MCPSelfRegistrationService.set_instance(original)
