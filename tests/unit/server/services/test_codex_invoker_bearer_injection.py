"""
Unit tests for CodexInvoker bearer_token_provider injection.

Tests the optional bearer_token_provider parameter added to CodexInvoker
so MCP tools registered via HTTP transport can authenticate via JWT Bearer
tokens injected into the CIDX_MCP_BEARER_TOKEN env var.

Test inventory (4 tests across 3 classes):

  TestBearerTokenProviderDefault (1 test)
    test_no_provider_means_no_cidx_mcp_bearer_token_in_env

  TestBearerTokenProviderInjection (2 tests)
    test_provider_token_injected_into_env
    test_provider_raises_subprocess_still_spawns_without_token

  TestBearerTokenProviderFreshPerInvocation (1 test)
    test_provider_called_once_per_invoke
"""

from __future__ import annotations

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
_BEARER_ENV_VAR = "CIDX_MCP_BEARER_TOKEN"
_TEST_TOKEN_A = "tok-abc"
_TEST_TOKEN_B = "tok-xyz"


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


class TestBearerTokenProviderDefault:
    """When bearer_token_provider is None, env does NOT contain CIDX_MCP_BEARER_TOKEN."""

    def test_no_provider_means_no_cidx_mcp_bearer_token_in_env(self):
        """
        With the default bearer_token_provider=None, Popen env must NOT contain
        CIDX_MCP_BEARER_TOKEN at all (backward-compat: existing behaviour unchanged).
        """
        invoker = CodexInvoker(codex_home=_FAKE_CODEX_HOME)
        proc = _make_success_proc()

        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            invoker.invoke(flow=_FLOW, cwd=_CWD, prompt=_PROMPT, timeout=_TIMEOUT)

        envs = _capture_popen_envs(mock_popen.call_args_list)
        assert envs, "Popen must have been called"
        for env in envs:
            assert _BEARER_ENV_VAR not in env, (
                f"CIDX_MCP_BEARER_TOKEN must NOT be in env when provider is None; "
                f"got env keys: {list(env.keys())}"
            )


# ---------------------------------------------------------------------------
# Tests: provider injection
# ---------------------------------------------------------------------------


class TestBearerTokenProviderInjection:
    """When bearer_token_provider is set, token is injected into subprocess env."""

    def test_provider_token_injected_into_env(self):
        """
        When bearer_token_provider returns 'tok-abc', Popen env must contain
        CIDX_MCP_BEARER_TOKEN='tok-abc'.
        """
        invoker = CodexInvoker(
            codex_home=_FAKE_CODEX_HOME,
            bearer_token_provider=lambda: _TEST_TOKEN_A,
        )
        proc = _make_success_proc()

        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            invoker.invoke(flow=_FLOW, cwd=_CWD, prompt=_PROMPT, timeout=_TIMEOUT)

        envs = _capture_popen_envs(mock_popen.call_args_list)
        assert envs, "Popen must have been called"
        assert envs[0].get(_BEARER_ENV_VAR) == _TEST_TOKEN_A, (
            f"CIDX_MCP_BEARER_TOKEN must be {_TEST_TOKEN_A!r}; "
            f"got {envs[0].get(_BEARER_ENV_VAR)!r}"
        )

    def test_provider_raises_subprocess_still_spawns_without_token(self):
        """
        When bearer_token_provider raises an exception, Popen must still be
        called (subprocess spawns) and CIDX_MCP_BEARER_TOKEN must NOT be in env
        (graceful degradation: MCP calls will fail but the invocation proceeds).
        """

        def _raising_provider() -> str:
            raise RuntimeError("JWT signing failed")

        invoker = CodexInvoker(
            codex_home=_FAKE_CODEX_HOME,
            bearer_token_provider=_raising_provider,
        )
        proc = _make_success_proc()

        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            invoker.invoke(flow=_FLOW, cwd=_CWD, prompt=_PROMPT, timeout=_TIMEOUT)

        assert mock_popen.called, "Popen must still be called even when provider raises"
        envs = _capture_popen_envs(mock_popen.call_args_list)
        for env in envs:
            assert _BEARER_ENV_VAR not in env, (
                f"CIDX_MCP_BEARER_TOKEN must NOT be in env when provider raises; "
                f"got {list(env.keys())}"
            )


# ---------------------------------------------------------------------------
# Tests: provider freshness
# ---------------------------------------------------------------------------


class TestBearerTokenProviderFreshPerInvocation:
    """Provider callable is invoked fresh on every invoke() call (not cached)."""

    def test_provider_called_once_per_invoke(self):
        """
        When invoke() is called 3 times, bearer_token_provider must be called
        exactly 3 times (once per invocation, never cached).
        """
        call_count = 0
        tokens = [_TEST_TOKEN_A, _TEST_TOKEN_B, "tok-third"]

        def _counting_provider() -> str:
            nonlocal call_count
            token = tokens[call_count % len(tokens)]
            call_count += 1
            return token

        invoker = CodexInvoker(
            codex_home=_FAKE_CODEX_HOME,
            bearer_token_provider=_counting_provider,
        )

        proc = _make_success_proc()
        with patch("subprocess.Popen", return_value=proc):
            for _ in range(3):
                invoker.invoke(flow=_FLOW, cwd=_CWD, prompt=_PROMPT, timeout=_TIMEOUT)

        assert call_count == 3, (
            f"bearer_token_provider must be called once per invoke(); "
            f"expected 3 calls, got {call_count}"
        )
