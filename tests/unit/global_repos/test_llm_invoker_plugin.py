"""
Pluggable LLM invocation: a deployment can supply its own agent backend.

Repo analysis (and the golden-repo registration lifecycle built on it) executes
its prompt by shelling out to the `claude` CLI. A deployment that cannot ship
the CLI in the server image -- e.g. one that must keep LLM execution inside a
separate sandboxed agent runner -- currently has no way to substitute a backend,
and every registration lifecycle job dies with `exit 127`.

These tests pin the seam:
  * with no plugin configured, NOTHING changes (the CLI subprocess still runs);
  * a plugin can be selected by env var or entry point, and fully replaces the
    subprocess -- including the `claude mcp` self-registration that would
    otherwise still require the CLI;
  * a plugin receives the exact documented contract and its return value is
    passed straight through;
  * misconfiguration fails LOUD rather than silently falling back to the CLI a
    plugin deployment specifically cannot run.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from code_indexer.global_repos import llm_invoker_plugin
from code_indexer.global_repos.llm_invoker_plugin import (
    ENV_VAR,
    LlmInvokerPluginError,
    get_llm_invoker,
)
from code_indexer.global_repos.repo_analyzer import invoke_claude_cli


@pytest.fixture(autouse=True)
def _clear_plugin_cache(monkeypatch):
    """The resolved plugin is cached per process; isolate every test."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    llm_invoker_plugin.reset_cache()
    yield
    llm_invoker_plugin.reset_cache()


# --------------------------------------------------------------------------
# A test plugin, addressable by "module:callable" (this module is importable).
# --------------------------------------------------------------------------

calls: list = []


def recording_invoker(repo_path, prompt, shell_timeout_seconds, outer_timeout_seconds):
    calls.append((repo_path, prompt, shell_timeout_seconds, outer_timeout_seconds))
    return True, "output from the plugin"


def failing_invoker(repo_path, prompt, shell_timeout_seconds, outer_timeout_seconds):
    return False, "backend refused the job"


not_callable = "I am a string, not a callable"


_THIS = "tests.unit.global_repos.test_llm_invoker_plugin"


class TestNoPluginIsTheDefault:
    def test_resolves_to_none_when_nothing_configured(self):
        assert get_llm_invoker() is None

    def test_invoke_claude_cli_still_spawns_the_subprocess(self):
        """The default path must be untouched: no plugin -> CLI as before."""
        with patch(
            "code_indexer.global_repos.repo_analyzer.subprocess.Popen"
        ) as mock_popen:
            proc = mock_popen.return_value
            proc.communicate.return_value = ("cli output", "")
            proc.returncode = 0

            ok, out = invoke_claude_cli("/repo", "analyze", 10, 20)

        assert mock_popen.called, "the Claude CLI subprocess must still be spawned"
        assert ok is True
        assert "cli output" in out


class TestPluginSelectedByEnvVar:
    def test_plugin_replaces_the_subprocess_entirely(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:recording_invoker")
        calls.clear()

        with patch(
            "code_indexer.global_repos.repo_analyzer.subprocess.Popen"
        ) as mock_popen:
            ok, out = invoke_claude_cli("/repo", "analyze this", 10, 20)

        assert (ok, out) == (True, "output from the plugin")
        assert (
            not mock_popen.called
        ), "no subprocess may be spawned when a plugin is set"

    def test_plugin_receives_the_documented_contract(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:recording_invoker")
        calls.clear()

        invoke_claude_cli("/srv/repo", "the prompt", 300, 360)

        assert calls == [("/srv/repo", "the prompt", 300, 360)]

    def test_plugin_failure_is_passed_through_not_swallowed(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:failing_invoker")

        ok, out = invoke_claude_cli("/repo", "analyze", 10, 20)

        assert ok is False
        assert out == "backend refused the job"

    def test_the_claude_mcp_self_registration_is_skipped(self, monkeypatch):
        """MCP self-registration shells out to `claude mcp` -- exactly what a
        plugin deployment does not have. It must not run."""
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:recording_invoker")

        with patch(
            "code_indexer.server.services.mcp_self_registration_service."
            "MCPSelfRegistrationService.get_instance"
        ) as mock_get:
            invoke_claude_cli("/repo", "analyze", 10, 20)

        assert not mock_get.called

    def test_argument_validation_still_applies_to_plugins(self, monkeypatch):
        """A plugin gets the same guarantees the CLI does."""
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:recording_invoker")

        with pytest.raises(ValueError):
            invoke_claude_cli("", "analyze", 10, 20)
        with pytest.raises(ValueError):
            invoke_claude_cli("/repo", "analyze", 20, 10)  # outer <= shell


class TestPluginSelectedByEntryPoint:
    def test_entry_point_plugin_is_used(self, monkeypatch):
        class _EP:
            name = "orchestrator"

            def load(self):
                return recording_invoker

        monkeypatch.setattr(
            llm_invoker_plugin, "entry_points", lambda group=None: [_EP()]
        )
        calls.clear()

        assert get_llm_invoker() is recording_invoker

    def test_no_entry_points_means_no_plugin(self, monkeypatch):
        monkeypatch.setattr(llm_invoker_plugin, "entry_points", lambda group=None: [])
        assert get_llm_invoker() is None

    def test_env_var_wins_over_entry_point(self, monkeypatch):
        class _EP:
            name = "entrypoint-one"

            def load(self):
                return failing_invoker

        monkeypatch.setattr(
            llm_invoker_plugin, "entry_points", lambda group=None: [_EP()]
        )
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:recording_invoker")

        assert get_llm_invoker() is recording_invoker


class TestMisconfigurationFailsLoud:
    """A deployment that asked for a plugin must NOT silently get the CLI --
    that is the exact behaviour it was configured to avoid."""

    def test_unknown_module(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "no_such_module_xyz:invoke")
        with pytest.raises(LlmInvokerPluginError, match="cannot import module"):
            get_llm_invoker()

    def test_missing_attribute(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:does_not_exist")
        with pytest.raises(LlmInvokerPluginError, match="has no attribute"):
            get_llm_invoker()

    def test_not_callable(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:not_callable")
        with pytest.raises(LlmInvokerPluginError, match="not callable"):
            get_llm_invoker()

    def test_malformed_spec(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "module.without.colon")
        with pytest.raises(LlmInvokerPluginError, match="package.module:callable"):
            get_llm_invoker()

    def test_entry_point_that_fails_to_load(self, monkeypatch):
        class _EP:
            name = "broken"

            def load(self):
                raise ImportError("boom")

        monkeypatch.setattr(
            llm_invoker_plugin, "entry_points", lambda group=None: [_EP()]
        )
        with pytest.raises(LlmInvokerPluginError, match="failed to load"):
            get_llm_invoker()
