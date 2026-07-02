"""
Tests verifying that DescriptionRefreshScheduler._build_cli_dispatcher delegates
to dep_map_dispatcher_factory.build_dep_map_dispatcher (Bug #936).

After consolidation, _build_cli_dispatcher must be a thin shim that calls
build_dep_map_dispatcher forwarding analysis_model and claude_soft_timeout_seconds,
rather than duplicating dispatcher-construction logic inline.

Anti-mock rule: the scheduler is instantiated via its real __init__ using injectable
backends; only the factory function at the module-level import boundary is patched.

Test inventory (exhaustive list):
  test_cli_dispatcher_delegates_to_factory
  test_cli_dispatcher_passes_soft_timeout_constant
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scheduler(analysis_model: str = "opus"):
    """
    Construct a DescriptionRefreshScheduler via its real __init__ using
    injectable backend mode (no db_path required when both backends are supplied).
    """
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )

    tracking_backend = MagicMock(name="tracking_backend")
    golden_backend = MagicMock(name="golden_backend")

    return DescriptionRefreshScheduler(
        tracking_backend=tracking_backend,
        golden_backend=golden_backend,
        analysis_model=analysis_model,
    )


def _make_mock_config():
    """Build a minimal mock ServerConfig (codex disabled for simplicity)."""
    from code_indexer.server.utils.config_manager import CodexIntegrationConfig

    codex_cfg = CodexIntegrationConfig(
        enabled=False,
        codex_weight=0.0,
        credential_mode="api_key",
        api_key="placeholder",
    )
    cfg = MagicMock()
    cfg.codex_integration_config = codex_cfg
    return cfg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cli_dispatcher_delegates_to_factory():
    """
    _build_cli_dispatcher must call build_dep_map_dispatcher and return its result,
    forwarding analysis_model from the scheduler instance.

    Patching the factory at the module-level import in description_refresh_scheduler
    proves the scheduler no longer constructs the dispatcher itself.
    """
    config = _make_mock_config()
    mock_dispatcher = MagicMock(name="fake_dispatcher")

    scheduler = _make_scheduler(analysis_model="opus")

    with patch(
        "code_indexer.server.services.description_refresh_scheduler.build_dep_map_dispatcher",
        return_value=mock_dispatcher,
    ) as mock_factory:
        result = scheduler._build_cli_dispatcher(config)

    mock_factory.assert_called_once()
    call_kwargs = mock_factory.call_args.kwargs
    assert call_kwargs.get("analysis_model") == "opus", (
        f"analysis_model must be forwarded as 'opus', got {call_kwargs.get('analysis_model')!r}"
    )
    assert result is mock_dispatcher, (
        "_build_cli_dispatcher must return the dispatcher produced by the factory"
    )


def test_cli_dispatcher_passes_soft_timeout_constant():
    """
    _build_cli_dispatcher must forward _CLAUDE_CLI_SOFT_TIMEOUT_SECONDS as
    claude_soft_timeout_seconds to build_dep_map_dispatcher.

    This ensures the description-refresh path continues to use its configured
    timeout budget after the builder consolidation.
    """
    from code_indexer.server.services.description_refresh_scheduler import (
        _CLAUDE_CLI_SOFT_TIMEOUT_SECONDS,
    )

    config = _make_mock_config()
    mock_dispatcher = MagicMock(name="fake_dispatcher")

    scheduler = _make_scheduler(analysis_model="opus")

    with patch(
        "code_indexer.server.services.description_refresh_scheduler.build_dep_map_dispatcher",
        return_value=mock_dispatcher,
    ) as mock_factory:
        scheduler._build_cli_dispatcher(config)

    call_kwargs = mock_factory.call_args.kwargs
    assert (
        call_kwargs.get("claude_soft_timeout_seconds")
        == _CLAUDE_CLI_SOFT_TIMEOUT_SECONDS
    ), (
        f"Expected claude_soft_timeout_seconds={_CLAUDE_CLI_SOFT_TIMEOUT_SECONDS}, "
        f"got {call_kwargs.get('claude_soft_timeout_seconds')!r}"
    )
