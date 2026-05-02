"""
Tests for dep_map_dispatcher_factory.py (Bug #936).

Verifies that build_dep_map_dispatcher:
  - Builds a CliDispatcher with both claude and codex invokers when
    codex is enabled and CODEX_HOME is set.
  - Builds a Claude-only CliDispatcher (codex=None) when codex is disabled.
  - Builds a Claude-only CliDispatcher when CODEX_HOME is not in os.environ,
    even if codex_integration_config.enabled=True.
  - Passes the configured codex_weight to the CliDispatcher.
  - Collapses effective weight to 0.0 when codex=None (CliDispatcher contract).

Test inventory (exhaustive list):
  test_builds_both_invokers_when_codex_enabled_and_home_set
  test_builds_claude_only_when_codex_disabled
  test_builds_claude_only_when_codex_home_missing
  test_codex_weight_propagated_when_codex_enabled
  test_effective_weight_zero_when_codex_none
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_config(
    codex_enabled: bool = False,
    codex_weight: float = 0.5,
):
    """Build a minimal mock ServerConfig."""
    from code_indexer.server.utils.config_manager import CodexIntegrationConfig

    codex_cfg = CodexIntegrationConfig(
        enabled=codex_enabled,
        codex_weight=codex_weight,
        credential_mode="api_key",
        api_key="placeholder",
    )
    cfg = MagicMock()
    cfg.codex_integration_config = codex_cfg
    return cfg


def _env_without_codex_home() -> dict:
    """Return a copy of os.environ with CODEX_HOME removed."""
    return {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_builds_both_invokers_when_codex_enabled_and_home_set(tmp_path: Path):
    """
    When codex_integration_config.enabled=True and CODEX_HOME is in os.environ,
    build_dep_map_dispatcher returns a CliDispatcher with both claude and codex
    invokers set (neither is None).
    """
    from code_indexer.server.services.dep_map_dispatcher_factory import (
        build_dep_map_dispatcher,
    )

    config = _make_mock_config(codex_enabled=True, codex_weight=0.7)
    codex_home = str(tmp_path / "codex-home")

    with patch.dict("os.environ", {"CODEX_HOME": codex_home}):
        dispatcher = build_dep_map_dispatcher(config)

    assert dispatcher.claude is not None, "claude invoker must always be present"
    assert dispatcher.codex is not None, (
        "codex invoker must be set when Codex enabled and CODEX_HOME is set"
    )


def test_builds_claude_only_when_codex_disabled(tmp_path: Path):
    """
    When codex_integration_config.enabled=False, build_dep_map_dispatcher
    returns a Claude-only CliDispatcher (codex=None).
    """
    from code_indexer.server.services.dep_map_dispatcher_factory import (
        build_dep_map_dispatcher,
    )

    config = _make_mock_config(codex_enabled=False)
    codex_home = str(tmp_path / "codex-home")

    with patch.dict("os.environ", {"CODEX_HOME": codex_home}):
        dispatcher = build_dep_map_dispatcher(config)

    assert dispatcher.claude is not None, "claude invoker must always be present"
    assert dispatcher.codex is None, "codex must be None when Codex disabled"


def test_builds_claude_only_when_codex_home_missing():
    """
    When CODEX_HOME is not set in os.environ, build_dep_map_dispatcher
    returns a Claude-only CliDispatcher even if codex_integration_config.enabled=True.
    """
    from code_indexer.server.services.dep_map_dispatcher_factory import (
        build_dep_map_dispatcher,
    )

    config = _make_mock_config(codex_enabled=True, codex_weight=1.0)

    with patch.dict("os.environ", _env_without_codex_home(), clear=True):
        dispatcher = build_dep_map_dispatcher(config)

    assert dispatcher.claude is not None, "claude invoker must always be present"
    assert dispatcher.codex is None, (
        "codex must be None when CODEX_HOME is absent from os.environ"
    )


def test_codex_weight_propagated_when_codex_enabled(tmp_path: Path):
    """
    The codex_weight from codex_integration_config is passed through to
    CliDispatcher.codex_weight when codex is enabled and CODEX_HOME is set.
    """
    from code_indexer.server.services.dep_map_dispatcher_factory import (
        build_dep_map_dispatcher,
    )

    expected_weight = 0.8
    config = _make_mock_config(codex_enabled=True, codex_weight=expected_weight)
    codex_home = str(tmp_path / "codex-home")

    with patch.dict("os.environ", {"CODEX_HOME": codex_home}):
        dispatcher = build_dep_map_dispatcher(config)

    assert dispatcher.codex_weight == expected_weight, (
        f"codex_weight must be {expected_weight}, got {dispatcher.codex_weight}"
    )


def test_effective_weight_zero_when_codex_none():
    """
    When codex is None (disabled or CODEX_HOME absent), CliDispatcher collapses
    the effective weight to 0.0, ensuring all dispatches route to Claude.
    """
    from code_indexer.server.services.dep_map_dispatcher_factory import (
        build_dep_map_dispatcher,
    )

    config = _make_mock_config(codex_enabled=False, codex_weight=1.0)

    with patch.dict("os.environ", _env_without_codex_home(), clear=True):
        dispatcher = build_dep_map_dispatcher(config)

    # CliDispatcher sets codex_weight=0.0 when codex is None (per its contract)
    assert dispatcher.codex_weight == 0.0, (
        f"effective codex_weight must collapse to 0.0 when codex=None, "
        f"got {dispatcher.codex_weight}"
    )
