"""
Tests verifying that DependencyMapAnalyzer._build_pass2_dispatcher delegates
to dep_map_dispatcher_factory.build_dep_map_dispatcher (Bug #936).

After consolidation, _build_pass2_dispatcher must be a thin shim that calls
build_dep_map_dispatcher forwarding analysis_model, rather than duplicating
dispatcher-construction logic inline (mirrors _build_pass1_dispatcher pattern).

Anti-mock rule: the analyzer is instantiated via its real constructor; only the
factory function and get_config_service at the module-level import boundary are
patched to avoid DB calls.

Test inventory (exhaustive list):
  test_pass2_dispatcher_delegates_to_factory
  test_pass2_dispatcher_passes_analysis_model
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Tuple
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


@contextmanager
def _build_analyzer_with_mocks(
    tmp_path: Path,
    analysis_model: str = "opus",
) -> Iterator[Tuple["DependencyMapAnalyzer", MagicMock, MagicMock]]:
    """
    Construct a real DependencyMapAnalyzer and patch the two module-level
    boundaries that _build_pass2_dispatcher crosses: get_config_service and
    build_dep_map_dispatcher.

    Yields (analyzer, mock_factory, mock_dispatcher).
    """
    from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer
    from code_indexer.server.utils.config_manager import CodexIntegrationConfig

    codex_cfg = CodexIntegrationConfig(
        enabled=False,
        codex_weight=0.0,
        credential_mode="api_key",
        api_key="placeholder",
    )
    config = MagicMock()
    config.codex_integration_config = codex_cfg

    mock_dispatcher = MagicMock(name="fake_pass2_dispatcher")

    with (
        patch(
            "code_indexer.global_repos.dependency_map_analyzer.get_config_service"
        ) as mock_get_cfg,
        patch(
            "code_indexer.global_repos.dependency_map_analyzer.build_dep_map_dispatcher",
            return_value=mock_dispatcher,
        ) as mock_factory,
    ):
        mock_svc = MagicMock()
        mock_svc.get_config.return_value = config
        mock_get_cfg.return_value = mock_svc

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
            analysis_model=analysis_model,
        )
        yield analyzer, mock_factory, mock_dispatcher


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pass2_dispatcher_delegates_to_factory(tmp_path: Path):
    """
    _build_pass2_dispatcher must call build_dep_map_dispatcher exactly once,
    forward analysis_model='opus', and return the dispatcher it produces.
    """
    with _build_analyzer_with_mocks(tmp_path, analysis_model="opus") as (
        analyzer,
        mock_factory,
        mock_dispatcher,
    ):
        result = analyzer._build_pass2_dispatcher()

    mock_factory.assert_called_once()
    call_kwargs = mock_factory.call_args.kwargs
    assert call_kwargs.get("analysis_model") == "opus", (
        f"analysis_model must be forwarded as 'opus', got {call_kwargs.get('analysis_model')!r}"
    )
    assert result is mock_dispatcher, (
        "_build_pass2_dispatcher must return the dispatcher produced by the factory"
    )


def test_pass2_dispatcher_passes_analysis_model(tmp_path: Path):
    """
    When analysis_model='sonnet' is configured on the analyzer,
    _build_pass2_dispatcher forwards 'sonnet' to build_dep_map_dispatcher.
    """
    with _build_analyzer_with_mocks(tmp_path, analysis_model="sonnet") as (
        analyzer,
        mock_factory,
        _,
    ):
        analyzer._build_pass2_dispatcher()

    call_kwargs = mock_factory.call_args.kwargs
    assert call_kwargs.get("analysis_model") == "sonnet", (
        f"analysis_model must be forwarded as 'sonnet', got {call_kwargs.get('analysis_model')!r}"
    )
