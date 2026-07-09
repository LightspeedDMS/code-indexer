"""
Tests for Bug #1049: build_dep_map_dispatcher must derive claude soft timeout
from config.dependency_map_pass_timeout_seconds when no explicit override is
passed.

Bug: call sites in dependency_map_service.py and dependency_map_analyzer.py
never pass claude_soft_timeout_seconds, so ClaudeInvoker always defaults to
_DEFAULT_SOFT_TIMEOUT_SECONDS = 1800, ignoring the operator config value
(e.g. dependency_map_pass_timeout_seconds=18000).

Fix (Option B): factory reads config.dependency_map_pass_timeout_seconds when
claude_soft_timeout_seconds is None and config is provided.
"""

from __future__ import annotations

from types import SimpleNamespace

from code_indexer.server.services.dep_map_dispatcher_factory import (
    build_dep_map_dispatcher,
)


def _make_config(
    dep_map_timeout: int | None = None,
    codex_enabled: bool = False,
) -> SimpleNamespace:
    """
    Build a minimal fake config that resembles ServerConfig for these tests.
    Uses SimpleNamespace so no DB or FastAPI context is needed.
    """
    codex_cfg = SimpleNamespace(enabled=codex_enabled, codex_weight=0.5)
    attrs: dict = {"codex_integration_config": codex_cfg}
    if dep_map_timeout is not None:
        attrs["dependency_map_pass_timeout_seconds"] = dep_map_timeout
    return SimpleNamespace(**attrs)


class TestDepMapDispatcherFactoryTimeoutConfigBug1049:
    """Regression tests for Bug #1049: config-derived timeout not propagated."""

    def test_soft_timeout_derived_from_config_when_not_passed_explicitly(
        self,
    ) -> None:
        """
        When no explicit claude_soft_timeout_seconds is passed and config has
        dependency_map_pass_timeout_seconds=18000, the ClaudeInvoker inside the
        dispatcher MUST be built with soft_timeout_seconds=18000.

        This test FAILS before the Bug #1049 fix because the factory ignores
        the config attribute and always uses ClaudeInvoker's default (1800).
        """
        config = _make_config(dep_map_timeout=18000)
        dispatcher = build_dep_map_dispatcher(config)

        actual_timeout = dispatcher.claude._soft_timeout_seconds
        assert actual_timeout == 18000, (
            f"Expected ClaudeInvoker to use config.dependency_map_pass_timeout_seconds "
            f"(18000) but got {actual_timeout}. "
            f"Bug #1049: factory must derive timeout from config when no explicit "
            f"override is provided."
        )

    def test_explicit_override_wins_over_config(self) -> None:
        """
        When claude_soft_timeout_seconds=600 is passed explicitly alongside a
        config that declares dependency_map_pass_timeout_seconds=18000, the
        explicit override (600) MUST win.

        Regression guard for the precedence contract: caller-override > config.
        """
        config = _make_config(dep_map_timeout=18000)
        dispatcher = build_dep_map_dispatcher(config, claude_soft_timeout_seconds=600)

        actual_timeout = dispatcher.claude._soft_timeout_seconds
        assert actual_timeout == 600, (
            f"Expected explicit override (600) to win over config (18000) "
            f"but got {actual_timeout}."
        )

    def test_falls_back_to_default_when_config_missing_attribute(self) -> None:
        """
        When config does NOT have dependency_map_pass_timeout_seconds and no
        explicit override is passed, the factory must NOT crash and must let
        ClaudeInvoker use its own built-in default (1800).

        Confirms backward compatibility for code paths that do not set the
        config field.
        """
        # Config without dependency_map_pass_timeout_seconds attribute
        config = _make_config(dep_map_timeout=None)
        assert not hasattr(config, "dependency_map_pass_timeout_seconds"), (
            "Test setup error: config should NOT have the attribute for this test."
        )

        # Must not raise; must produce a valid dispatcher
        dispatcher = build_dep_map_dispatcher(config)

        actual_timeout = dispatcher.claude._soft_timeout_seconds
        assert actual_timeout == 1800, (
            f"Expected ClaudeInvoker default timeout (1800) when config lacks "
            f"dependency_map_pass_timeout_seconds, but got {actual_timeout}."
        )
