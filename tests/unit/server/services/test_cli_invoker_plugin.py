"""
Pluggable primary CLI invoker at the dispatcher seam.

build_dep_map_dispatcher is the single source of truth for every
description/analysis LLM call (lifecycle, dep-map, description refresh,
self-monitoring). A deployment that cannot ship the `claude` CLI in the image
can substitute the primary invoker; with no plugin configured, nothing changes.

These tests pin:
  * no plugin -> a real ClaudeInvoker in the claude= slot (default unchanged);
  * a plugin -> its invoker takes the claude= slot AND codex is disabled (the
    plugin IS where execution happens; failover to a second local CLI is wrong);
  * the factory receives the same (analysis_model, soft_timeout) the built-in
    ClaudeInvoker would have; and
  * misconfiguration fails loud rather than silently using the CLI.
"""

from __future__ import annotations

import pytest

from code_indexer.server.services import cli_invoker_plugin
from code_indexer.server.services.cli_invoker_plugin import (
    ENV_VAR,
    CliInvokerPluginError,
    get_invoker_factory,
)
from code_indexer.server.services.claude_invoker import ClaudeInvoker
from code_indexer.server.services.dep_map_dispatcher_factory import (
    build_dep_map_dispatcher,
)
from code_indexer.server.services.intelligence_cli_invoker import InvocationResult


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    # Deterministic regardless of what happens to be pip-installed in the dev
    # venv: these tests exercise the env-var path explicitly, and the default
    # case must mean "no entry point either". (In CI the orchestrator plugin is
    # not installed; locally it may be.)
    monkeypatch.setattr(cli_invoker_plugin, "entry_points", lambda group=None: [])
    cli_invoker_plugin.reset_cache()
    yield
    cli_invoker_plugin.reset_cache()


class _StubInvoker:
    def __init__(self, model, soft_timeout):
        self.model = model
        self.soft_timeout = soft_timeout

    def invoke(self, flow, cwd, prompt, timeout, max_turns=0):
        return InvocationResult(True, "stub", "", "stub", False)


last_factory_args = {}


def make_stub(analysis_model, soft_timeout_seconds):
    last_factory_args["model"] = analysis_model
    last_factory_args["soft_timeout"] = soft_timeout_seconds
    return _StubInvoker(analysis_model, soft_timeout_seconds)


not_callable = "nope"

_THIS = "tests.unit.server.services.test_cli_invoker_plugin"


class TestDefaultIsUnchanged:
    def test_no_plugin_uses_claude_invoker(self):
        assert get_invoker_factory() is None
        dispatcher = build_dep_map_dispatcher(None)
        assert isinstance(dispatcher.claude, ClaudeInvoker)


class TestPluginTakesThePrimarySlot:
    def test_plugin_invoker_replaces_claude_and_disables_codex(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:make_stub")
        dispatcher = build_dep_map_dispatcher(None)

        # Identity check by name: the env-var spec re-imports this module under a
        # second name, so `is _StubInvoker` would compare two class objects.
        assert type(dispatcher.claude).__name__ == "_StubInvoker"
        assert not isinstance(dispatcher.claude, ClaudeInvoker)
        # Codex routing must be off: the plugin is where execution happens.
        assert dispatcher.codex is None
        assert dispatcher.codex_weight == 0.0

    def test_factory_gets_the_model_and_timeout_claude_would_have(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:make_stub")

        dispatcher = build_dep_map_dispatcher(
            None, analysis_model="sonnet", claude_soft_timeout_seconds=123
        )

        # Read the args back off the invoker the factory produced, rather than a
        # module global (double-import makes the global unreliable).
        assert dispatcher.claude.model == "sonnet"
        assert dispatcher.claude.soft_timeout == 123

    def test_dispatch_routes_through_the_plugin(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:make_stub")
        dispatcher = build_dep_map_dispatcher(None)

        result = dispatcher.dispatch("describe", "/repo", "prompt", 10)

        assert result.success is True
        assert result.cli_used == "stub"


class TestMisconfigurationFailsLoud:
    def test_unknown_module(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "no_such_module_zzz:make")
        with pytest.raises(CliInvokerPluginError, match="cannot import module"):
            get_invoker_factory()

    def test_missing_attribute(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:does_not_exist")
        with pytest.raises(CliInvokerPluginError, match="has no attribute"):
            get_invoker_factory()

    def test_not_callable(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:not_callable")
        with pytest.raises(CliInvokerPluginError, match="not callable"):
            get_invoker_factory()

    def test_malformed_spec(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "no.colon.here")
        with pytest.raises(CliInvokerPluginError, match="package.module:factory"):
            get_invoker_factory()
