"""
Unit tests for CodexInvoker auth_header_provider injection (v9.23.10).

Replaces test_codex_invoker_bearer_injection.py. The provider parameter is
renamed from bearer_token_provider to auth_header_provider, and the env var
is renamed from CIDX_MCP_BEARER_TOKEN to CIDX_MCP_AUTH_HEADER. The value is
the literal Authorization header value, not a bearer token.

Test inventory (4 tests across 3 classes):

  TestAuthHeaderProviderDefault (1 test)
    test_no_provider_means_no_cidx_mcp_auth_header_in_env

  TestAuthHeaderProviderInjection (2 tests)
    test_provider_value_injected_into_env
    test_provider_raises_subprocess_still_spawns_without_header

  TestAuthHeaderProviderFreshPerInvocation (1 test)
    test_provider_called_once_per_invoke
"""

from __future__ import annotations

import logging
from typing import List
from unittest.mock import MagicMock, patch

from code_indexer.server.services.codex_invoker import CodexInvoker


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FAKE_CODEX_HOME = "/fake/codex-home"
_FLOW = "describe"
_CWD = "/repo"
_PROMPT = "describe this repo"
_TIMEOUT = 60
_AUTH_HEADER_ENV_VAR = "CIDX_MCP_AUTH_HEADER"
# Clearly synthetic placeholder values — not real credentials.
_TEST_HEADER_A = "Basic abc"
_TEST_HEADER_B = "Basic xyz"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_success_proc() -> MagicMock:
    """Return a Popen mock that yields a valid JSONL agent_message event."""
    import json

    agent_event = {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "result text"},
    }
    stdout = json.dumps(agent_event) + "\n"
    proc = MagicMock()
    proc.pid = 12345
    proc.returncode = 0
    proc.communicate.return_value = (stdout, "")
    return proc


def _capture_popen_envs(call_args_list: list) -> List[dict]:
    """Extract env dicts from a list of Popen call_args."""
    envs = []
    for c in call_args_list:
        env = c.kwargs.get("env", {})
        envs.append(dict(env))
    return envs


# ---------------------------------------------------------------------------
# Tests: default behaviour (no provider)
# ---------------------------------------------------------------------------


class TestAuthHeaderProviderDefault:
    """When auth_header_provider is None, env does NOT contain CIDX_MCP_AUTH_HEADER."""

    def test_no_provider_means_no_cidx_mcp_auth_header_in_env(self):
        """
        With the default auth_header_provider=None, Popen env must NOT contain
        CIDX_MCP_AUTH_HEADER at all (backward-compat: existing behaviour unchanged).
        """
        invoker = CodexInvoker(codex_home=_FAKE_CODEX_HOME)
        proc = _make_success_proc()

        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            invoker.invoke(flow=_FLOW, cwd=_CWD, prompt=_PROMPT, timeout=_TIMEOUT)

        envs = _capture_popen_envs(mock_popen.call_args_list)
        assert envs, "Popen must have been called"
        for env in envs:
            assert _AUTH_HEADER_ENV_VAR not in env, (
                f"CIDX_MCP_AUTH_HEADER must NOT be in env when provider is None; "
                f"got env keys: {list(env.keys())}"
            )


# ---------------------------------------------------------------------------
# Tests: provider injection
# ---------------------------------------------------------------------------


class TestAuthHeaderProviderInjection:
    """When auth_header_provider is set, value is injected into subprocess env."""

    def test_provider_value_injected_into_env(self):
        """
        When auth_header_provider returns 'Basic abc', Popen env must contain
        CIDX_MCP_AUTH_HEADER='Basic abc' (literal header value for codex to use).
        """
        invoker = CodexInvoker(
            codex_home=_FAKE_CODEX_HOME,
            auth_header_provider=lambda: _TEST_HEADER_A,
        )
        proc = _make_success_proc()

        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            invoker.invoke(flow=_FLOW, cwd=_CWD, prompt=_PROMPT, timeout=_TIMEOUT)

        envs = _capture_popen_envs(mock_popen.call_args_list)
        assert envs, "Popen must have been called"
        assert envs[0].get(_AUTH_HEADER_ENV_VAR) == _TEST_HEADER_A, (
            f"CIDX_MCP_AUTH_HEADER must be {_TEST_HEADER_A!r}; "
            f"got {envs[0].get(_AUTH_HEADER_ENV_VAR)!r}"
        )

    def test_provider_raises_subprocess_still_spawns_without_header(self, caplog):
        """
        When auth_header_provider raises an exception, Popen must still be
        called (subprocess spawns) and CIDX_MCP_AUTH_HEADER must NOT be in env
        (graceful degradation: MCP calls may fail but the invocation proceeds).

        Foundation 13 (Anti-Silent-Failure): provider failures MUST log WARNING.
        """

        def _raising_provider() -> str:
            raise RuntimeError("Credential fetch failed")

        invoker = CodexInvoker(
            codex_home=_FAKE_CODEX_HOME,
            auth_header_provider=_raising_provider,
        )
        proc = _make_success_proc()

        with caplog.at_level(logging.WARNING):
            with patch("subprocess.Popen", return_value=proc) as mock_popen:
                invoker.invoke(flow=_FLOW, cwd=_CWD, prompt=_PROMPT, timeout=_TIMEOUT)

        assert mock_popen.called, "subprocess must still spawn when provider raises"

        envs = _capture_popen_envs(mock_popen.call_args_list)
        for env in envs:
            assert _AUTH_HEADER_ENV_VAR not in env, (
                f"CIDX_MCP_AUTH_HEADER must NOT be in env when provider raises; "
                f"got {list(env.keys())}"
            )

        # Foundation 13: graceful degradation must produce an observable WARNING
        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "auth_header" in r.message.lower() or "provider" in r.message.lower()
            for r in warning_records
        ), (
            f"Expected WARNING about auth_header provider failure; "
            f"got: {[r.message for r in warning_records]}"
        )


# ---------------------------------------------------------------------------
# Tests: provider freshness per invocation
# ---------------------------------------------------------------------------


class TestAuthHeaderProviderFreshPerInvocation:
    """Provider callable is invoked once per invoke() call (not cached)."""

    def test_provider_called_once_per_invoke(self):
        """
        When invoke() is called 3 times, auth_header_provider must be called
        exactly 3 times (once per invocation, never cached at the invoker level).
        Cycles between _TEST_HEADER_A and _TEST_HEADER_B to confirm each call
        gets a fresh value from the provider.
        """
        call_count = 0
        headers = [_TEST_HEADER_A, _TEST_HEADER_B]

        def _counting_provider() -> str:
            nonlocal call_count
            header = headers[call_count % len(headers)]
            call_count += 1
            return header

        invoker = CodexInvoker(
            codex_home=_FAKE_CODEX_HOME,
            auth_header_provider=_counting_provider,
        )

        proc = _make_success_proc()
        with patch("subprocess.Popen", return_value=proc):
            for _ in range(3):
                invoker.invoke(flow=_FLOW, cwd=_CWD, prompt=_PROMPT, timeout=_TIMEOUT)

        assert call_count == 3, (
            f"auth_header_provider must be called once per invoke(); "
            f"expected 3 calls, got {call_count}"
        )
