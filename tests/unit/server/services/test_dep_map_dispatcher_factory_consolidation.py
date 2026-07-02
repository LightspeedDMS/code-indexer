"""
Tests for build_dep_map_dispatcher claude_soft_timeout_seconds parameter (Bug #936).

Verifies that build_dep_map_dispatcher accepts claude_soft_timeout_seconds and
correctly forwards it to ClaudeInvoker at construction time, and that invalid
values propagate ClaudeInvoker's own validation errors without being swallowed.

Test inventory (exhaustive list):
  test_factory_forwards_soft_timeout_to_claude_invoker
  test_factory_none_soft_timeout_omits_kwarg
  test_factory_soft_timeout_zero_raises
  test_factory_soft_timeout_float_raises
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


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
# Tests: claude_soft_timeout_seconds parameter forwarding
# ---------------------------------------------------------------------------


def test_factory_forwards_soft_timeout_to_claude_invoker():
    """
    When claude_soft_timeout_seconds=120, build_dep_map_dispatcher constructs
    ClaudeInvoker with soft_timeout_seconds=120.
    """
    from code_indexer.server.services.dep_map_dispatcher_factory import (
        build_dep_map_dispatcher,
    )

    config = _make_mock_config(codex_enabled=False)

    with (
        patch.dict("os.environ", _env_without_codex_home(), clear=True),
        patch(
            "code_indexer.server.services.dep_map_dispatcher_factory.ClaudeInvoker"
        ) as MockClaudeInvoker,
    ):
        MockClaudeInvoker.return_value = MagicMock()
        build_dep_map_dispatcher(
            config,
            analysis_model="opus",
            claude_soft_timeout_seconds=120,
        )

    MockClaudeInvoker.assert_called_once_with(
        analysis_model="opus",
        soft_timeout_seconds=120,
    )


def test_factory_none_soft_timeout_omits_kwarg():
    """
    When claude_soft_timeout_seconds=None, build_dep_map_dispatcher constructs
    ClaudeInvoker WITHOUT soft_timeout_seconds so the invoker uses its own default.
    """
    from code_indexer.server.services.dep_map_dispatcher_factory import (
        build_dep_map_dispatcher,
    )

    config = _make_mock_config(codex_enabled=False)

    with (
        patch.dict("os.environ", _env_without_codex_home(), clear=True),
        patch(
            "code_indexer.server.services.dep_map_dispatcher_factory.ClaudeInvoker"
        ) as MockClaudeInvoker,
    ):
        MockClaudeInvoker.return_value = MagicMock()
        build_dep_map_dispatcher(
            config,
            analysis_model="sonnet",
            claude_soft_timeout_seconds=None,
        )

    MockClaudeInvoker.assert_called_once_with(
        analysis_model="sonnet",
    )


def test_factory_soft_timeout_zero_raises():
    """
    When claude_soft_timeout_seconds=0, ClaudeInvoker raises ValueError
    (soft_timeout_seconds must be > 0). The factory does not swallow the error.
    """
    from code_indexer.server.services.dep_map_dispatcher_factory import (
        build_dep_map_dispatcher,
    )

    config = _make_mock_config(codex_enabled=False)

    with (
        patch.dict("os.environ", _env_without_codex_home(), clear=True),
        pytest.raises(ValueError),
    ):
        build_dep_map_dispatcher(config, claude_soft_timeout_seconds=0)


def test_factory_soft_timeout_float_raises():
    """
    When claude_soft_timeout_seconds is a float (e.g. 90.0), ClaudeInvoker raises
    ValueError because it requires type(x) is int exactly (bool subclass excluded).
    The factory forwards the bad value without masking the error.

    The float literal is intentional: this test exercises runtime type validation
    in ClaudeInvoker. The type: ignore is justified because we deliberately pass
    a float to verify that the invoker's strict int check fires at runtime.
    """
    from code_indexer.server.services.dep_map_dispatcher_factory import (
        build_dep_map_dispatcher,
    )

    config = _make_mock_config(codex_enabled=False)

    with (
        patch.dict("os.environ", _env_without_codex_home(), clear=True),
        pytest.raises((ValueError, TypeError)),
    ):
        build_dep_map_dispatcher(
            config,
            claude_soft_timeout_seconds=90.0,  # type: ignore[arg-type]  # intentional bad type for runtime validation test
        )
