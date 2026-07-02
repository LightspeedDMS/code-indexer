"""
Unit tests for CodexInvoker auth_header_provider loud failure mode (bug #937).

Bug #937: CodexInvoker currently logs a WARNING and spawns the subprocess without
CIDX_MCP_AUTH_HEADER when the provider raises. This is anti-silent-failure (MESSI
Rule 13): degraded execution must be loud so operators can diagnose it.

The agreed fix: when the provider raises, CodexInvoker must log ERROR (not WARNING),
return RETRYABLE_ON_OTHER so the dispatcher can failover to Claude, and must NOT
spawn the subprocess (fail fast rather than silently degrade Codex MCP access).

This complements the provider-level fix in test_codex_mcp_auth_header_provider_937.py.
Together, they ensure:
  - Provider succeeds when stored credentials exist (no Claude CLI needed)
  - When provider still raises (store truly empty), error is loud at BOTH levels

Test inventory (5 tests across 3 classes):

  TestAuthHeaderSuccessPath (1 test)
    test_valid_header_injected_and_subprocess_spawns

  TestAuthHeaderLoudFailureMode (3 tests)
    test_provider_raises_logs_error_not_warning
    test_provider_raises_returns_retryable_on_other
    test_provider_raises_subprocess_not_spawned

  TestAuthHeaderProviderNoneDefault (1 test)
    test_none_provider_unchanged_behavior
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple
from unittest.mock import MagicMock, patch

from code_indexer.server.services.codex_invoker import CodexInvoker
from code_indexer.server.services.intelligence_cli_invoker import (
    FailureClass,
    InvocationResult,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FAKE_CODEX_HOME = "/fake/codex-home"
_FLOW = "dep-map-pass2"
_CWD = "/repo"
_PROMPT = "analyze this domain"
_TIMEOUT = 300
_AUTH_HEADER_ENV_VAR = "CIDX_MCP_AUTH_HEADER"

# Clearly synthetic non-credential header value — not derived from username/secret.
# Uses the string "SYNTHETIC-TEST-HEADER-FOR-937" as a recognisable test-only marker.
_VALID_TEST_HEADER = "Basic U1lOVEhFVElDLVRFU1QtSEVBREVSLUZPUi05Mzc="


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_success_proc() -> MagicMock:
    """Return a Popen mock that yields a valid JSONL agent_message event."""
    import json

    agent_event = {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "analysis result"},
    }
    proc = MagicMock()
    proc.pid = 99999
    proc.returncode = 0
    proc.communicate.return_value = (json.dumps(agent_event) + "\n", "")
    return proc


def _raising_provider() -> str:
    """Shared raising auth_header_provider used across loud-failure tests.

    Simulates the failure mode from bug #937: MCPSelfRegistrationService has
    no cached header AND no stored credentials, so the provider raises RuntimeError.
    """
    raise RuntimeError(
        "build_codex_mcp_auth_header_provider: unable to obtain Authorization header "
        "for Codex MCP — both cached header and credentials returned None"
    )


def _make_raising_invoker() -> CodexInvoker:
    """Return a CodexInvoker wired with _raising_provider (shared across loud-failure tests)."""
    return CodexInvoker(
        codex_home=_FAKE_CODEX_HOME,
        auth_header_provider=_raising_provider,
    )


def _invoke_with_patched_popen(
    invoker: CodexInvoker,
    proc: Optional[MagicMock] = None,
) -> Tuple[InvocationResult, MagicMock]:
    """Call invoker.invoke() with Popen patched. Returns (result, mock_popen).

    The default proc is a valid success proc so that the buggy code path
    (which spawns the subprocess even when the provider raises) completes
    normally and the assertions fail for the intended reason rather than
    crashing with AttributeError on proc.communicate().
    """
    if proc is None:
        proc = _make_success_proc()
    with patch("subprocess.Popen", return_value=proc) as mock_popen:
        result = invoker.invoke(flow=_FLOW, cwd=_CWD, prompt=_PROMPT, timeout=_TIMEOUT)
    return result, mock_popen


# ---------------------------------------------------------------------------
# Tests: success path — valid header injected, subprocess spawns
# ---------------------------------------------------------------------------


class TestAuthHeaderSuccessPath:
    """When provider returns a valid header, env var is set and subprocess spawns."""

    def test_valid_header_injected_and_subprocess_spawns(self):
        """
        When auth_header_provider returns a valid 'Basic <b64>' string,
        Popen env must contain CIDX_MCP_AUTH_HEADER set to that string,
        and the invocation must succeed.

        This is the green path after the fix: stored credentials exist,
        provider succeeds, Codex runs with MCP auth.
        """
        invoker = CodexInvoker(
            codex_home=_FAKE_CODEX_HOME,
            auth_header_provider=lambda: _VALID_TEST_HEADER,
        )
        result, mock_popen = _invoke_with_patched_popen(invoker, _make_success_proc())

        assert mock_popen.called, "Popen must be called when provider succeeds"
        env = mock_popen.call_args.kwargs.get("env", {})
        assert env.get(_AUTH_HEADER_ENV_VAR) == _VALID_TEST_HEADER, (
            f"Env must contain {_AUTH_HEADER_ENV_VAR}={_VALID_TEST_HEADER!r}; "
            f"got {env.get(_AUTH_HEADER_ENV_VAR)!r}"
        )
        assert result.success, (
            f"Invocation must succeed when provider returns valid header; "
            f"got error={result.error!r}"
        )


# ---------------------------------------------------------------------------
# Tests: loud failure mode when provider raises
# ---------------------------------------------------------------------------


class TestAuthHeaderLoudFailureMode:
    """When provider raises, failure is loud: ERROR log + RETRYABLE_ON_OTHER + no subprocess."""

    def test_provider_raises_logs_error_not_warning(self, caplog):
        """
        Bug #937 noise complaint: today CodexInvoker logs WARNING when provider raises.
        After the fix, it must log ERROR and NOT WARNING (MESSI Rule 13: Anti-Silent-Failure).

        An ERROR surfaces in monitoring dashboards and alerting rules. A WARNING
        does not. Codex running without MCP auth is a configuration failure, not
        a transient condition — it needs an ERROR.

        Asserts both that at least one ERROR record exists AND no WARNING records
        are emitted for this failure path (fully verifying ERROR-not-WARNING).

        RED: Fails before fix because the current code logs WARNING, not ERROR.
        """
        invoker = _make_raising_invoker()

        with caplog.at_level(
            logging.DEBUG, logger="code_indexer.server.services.codex_invoker"
        ):
            _invoke_with_patched_popen(invoker)

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]

        assert error_records, (
            "CodexInvoker must log ERROR when auth_header_provider raises; "
            "got no ERROR records. This is a MESSI Rule 13 violation — Codex "
            "without MCP auth is a configuration failure that must be loud."
        )
        assert not warning_records, (
            f"CodexInvoker must NOT log WARNING when auth_header_provider raises "
            f"(the failure must be ERROR, not WARNING); "
            f"got WARNING records: {[r.message for r in warning_records]}"
        )

    def test_provider_raises_returns_retryable_on_other(self):
        """
        When provider raises, CodexInvoker must return RETRYABLE_ON_OTHER so the
        dispatcher can failover to Claude instead of silently degrading.

        RED: Fails before fix because current code spawns the subprocess anyway
        (returning whatever the subprocess produces, not RETRYABLE_ON_OTHER).
        """
        invoker = _make_raising_invoker()
        result, _ = _invoke_with_patched_popen(invoker)

        assert not result.success, (
            "Invocation must fail when auth_header_provider raises"
        )
        assert result.failure_class == FailureClass.RETRYABLE_ON_OTHER, (
            f"failure_class must be RETRYABLE_ON_OTHER so dispatcher can failover; "
            f"got {result.failure_class!r}"
        )

    def test_provider_raises_subprocess_not_spawned(self):
        """
        When provider raises, Popen must NOT be called — fail fast, don't
        spawn a Codex subprocess that will silently fail its MCP calls.

        RED: Fails before fix because current code spawns the subprocess anyway.
        """
        invoker = _make_raising_invoker()
        _, mock_popen = _invoke_with_patched_popen(invoker)

        assert not mock_popen.called, (
            "Popen must NOT be called when auth_header_provider raises — "
            "fail fast rather than spawn a degraded Codex process that cannot "
            "reach cidx-local MCP and will produce lower-quality analysis"
        )


# ---------------------------------------------------------------------------
# Tests: None provider default — unchanged behaviour
# ---------------------------------------------------------------------------


class TestAuthHeaderProviderNoneDefault:
    """auth_header_provider=None retains backward-compatible behaviour."""

    def test_none_provider_unchanged_behavior(self):
        """
        When auth_header_provider=None (default), CodexInvoker continues to
        spawn the subprocess normally without CIDX_MCP_AUTH_HEADER in env.
        The new loud-failure mode must NOT affect the None-provider case.
        """
        invoker = CodexInvoker(codex_home=_FAKE_CODEX_HOME)
        result, mock_popen = _invoke_with_patched_popen(invoker, _make_success_proc())

        assert mock_popen.called, "Popen must be called when provider is None"
        env = mock_popen.call_args.kwargs.get("env", {})
        assert _AUTH_HEADER_ENV_VAR not in env, (
            f"{_AUTH_HEADER_ENV_VAR} must NOT be in env when provider is None; "
            f"got env keys: {list(env.keys())}"
        )
        assert result.success, (
            f"Invocation must succeed when provider is None; error={result.error!r}"
        )
