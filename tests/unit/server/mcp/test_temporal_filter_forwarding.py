"""Tests for Bug #664 and Bug #665: temporal filter parameters silently dropped.

Bug #664: chunk_type filter silently dropped in temporal dispatch
  - execute_temporal_query_with_fusion never passes chunk_type to
    _query_single_provider or _query_multi_provider_fusion.

Bug #665: author and diff_types silently dropped in server/MCP path
  - _execute_temporal_query in semantic_query_manager.py is missing
    author, diff_types, and chunk_type from its signature and call.
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch
from pathlib import Path


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_fake_temporal_results(results=None):
    """Return a minimal TemporalSearchResults-like object."""
    obj = MagicMock()
    obj.results = results or []
    obj.warning = None
    obj.query = "test"
    obj.filter_type = "none"
    obj.filter_value = None
    obj.total_found = 0
    return obj


def _enter_fusion_patches(
    stack: ExitStack,
    collections,
    mock_service_instance,
):
    """Enter all patches needed to run execute_temporal_query_with_fusion
    without touching disk/network.  Returns the ExitStack (already entered).

    Args:
        stack: contextlib.ExitStack already entered by the caller.
        collections: list of (name, path) tuples returned by discovery.
        mock_service_instance: pre-configured MagicMock for TemporalSearchService.
    """
    # migrate_legacy_temporal_collection is lazily imported inside
    # execute_temporal_query_with_fusion; patch at its source module.
    stack.enter_context(
        patch(
            "code_indexer.services.temporal.temporal_migration"
            ".migrate_legacy_temporal_collection"
        )
    )
    stack.enter_context(
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch"
            "._discover_queryable_collections",
            return_value=collections,
        )
    )
    stack.enter_context(
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch"
            ".filter_healthy_temporal_providers",
            return_value=(collections, []),
        )
    )
    stack.enter_context(
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch"
            "._create_embedding_provider_for_collection",
            return_value=MagicMock(),
        )
    )
    # TemporalSearchService is lazily imported inside _query_single_provider /
    # _query_multi_provider_fusion; patch at its source module.
    stack.enter_context(
        patch(
            "code_indexer.services.temporal.temporal_search_service"
            ".TemporalSearchService",
            return_value=mock_service_instance,
        )
    )
    stack.enter_context(
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch"
            "._make_config_manager",
            return_value=MagicMock(),
        )
    )


def _enter_semantic_manager_patches(
    stack: ExitStack,
):
    """Enter patches for SemanticQueryManager._execute_temporal_query heavy deps.

    All three heavy imports inside _execute_temporal_query are lazy (done at
    call time, not at module import time).  Patching at the *caller* module
    won't work because the attributes don't exist there at module level.
    We must patch at the *source* module so the lazy import picks up the mock.

    Returns:
        Tuple (mock_fusion, mock_cm_cls, mock_bf) — the three patched objects.
    """
    # execute_temporal_query_with_fusion: lazily imported from temporal_fusion_dispatch
    mock_fusion = stack.enter_context(
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch"
            ".execute_temporal_query_with_fusion"
        )
    )
    # ConfigManager: lazily imported from code_indexer.proxy.config_manager
    mock_cm_cls = stack.enter_context(
        patch("code_indexer.proxy.config_manager.ConfigManager")
    )
    # BackendFactory: lazily imported from code_indexer.backends.backend_factory
    mock_bf = stack.enter_context(
        patch("code_indexer.backends.backend_factory.BackendFactory")
    )

    # Wire ConfigManager and BackendFactory to sensible mocks
    mock_config = MagicMock()
    mock_cm_instance = MagicMock()
    mock_cm_instance.get_config.return_value = mock_config
    mock_cm_cls.create_with_backtrack.return_value = mock_cm_instance

    mock_backend = MagicMock()
    mock_vector_store = MagicMock()
    mock_backend.get_vector_store_client.return_value = mock_vector_store
    mock_bf.create.return_value = mock_backend

    mock_fusion.return_value = _make_fake_temporal_results()

    return mock_fusion, mock_cm_cls, mock_bf


# ---------------------------------------------------------------------------
# Bug #664: chunk_type forwarding through temporal_fusion_dispatch
# ---------------------------------------------------------------------------


class TestChunkTypeForwarding:
    """Bug #664 — chunk_type must reach service.query_temporal via dispatch."""

    def test_chunk_type_reaches_query_temporal_single_provider_path(self):
        """chunk_type passed to execute_temporal_query_with_fusion reaches
        service.query_temporal() on the single-provider code path."""
        from code_indexer.services.temporal.temporal_fusion_dispatch import (
            execute_temporal_query_with_fusion,
        )

        config = MagicMock()
        vector_store = MagicMock()
        vector_store.project_root = Path("/tmp/test-repo")
        index_path = Path("/tmp/test-repo/.code-indexer/index")
        collections = [("temporal-voyage_code_3", index_path)]

        mock_service = MagicMock()
        mock_service.query_temporal.return_value = _make_fake_temporal_results()

        with ExitStack() as stack:
            _enter_fusion_patches(stack, collections, mock_service)

            execute_temporal_query_with_fusion(
                config=config,
                index_path=index_path,
                vector_store=vector_store,
                query_text="find auth code",
                limit=10,
                chunk_type="function",
            )

        mock_service.query_temporal.assert_called_once()
        _, kwargs = mock_service.query_temporal.call_args
        assert kwargs.get("chunk_type") == "function", (
            f"chunk_type='function' must reach service.query_temporal(); "
            f"got kwargs={kwargs}"
        )

    def test_chunk_type_reaches_query_temporal_multi_provider_path(self):
        """chunk_type passed to execute_temporal_query_with_fusion reaches
        service.query_temporal() on the multi-provider fusion code path."""
        from code_indexer.services.temporal.temporal_fusion_dispatch import (
            execute_temporal_query_with_fusion,
        )

        config = MagicMock()
        vector_store = MagicMock()
        vector_store.project_root = Path("/tmp/test-repo")
        index_path = Path("/tmp/test-repo/.code-indexer/index")
        collections = [
            ("temporal-voyage_code_3", index_path),
            ("temporal-openai_large", index_path),
        ]

        call_kwargs_captured = []

        def fake_query_temporal(**kwargs):
            call_kwargs_captured.append(kwargs)
            return _make_fake_temporal_results()

        mock_service = MagicMock()
        mock_service.query_temporal.side_effect = fake_query_temporal

        with ExitStack() as stack:
            _enter_fusion_patches(stack, collections, mock_service)

            execute_temporal_query_with_fusion(
                config=config,
                index_path=index_path,
                vector_store=vector_store,
                query_text="find auth code",
                limit=10,
                chunk_type="class",
            )

        assert len(call_kwargs_captured) >= 1, (
            "service.query_temporal must be called at least once in multi-provider path"
        )
        for captured in call_kwargs_captured:
            assert captured.get("chunk_type") == "class", (
                f"chunk_type='class' must reach every service.query_temporal() call; "
                f"got kwargs={captured}"
            )


# ---------------------------------------------------------------------------
# Bug #665: author / diff_type / chunk_type forwarding in semantic_query_manager
# ---------------------------------------------------------------------------


class TestExecuteTemporalQueryParameterForwarding:
    """Bug #665 — _execute_temporal_query must accept and forward
    author, diff_type (converted to list), and chunk_type to
    execute_temporal_query_with_fusion."""

    def test_author_forwarded_to_fusion(self):
        """author passed to _execute_temporal_query reaches
        execute_temporal_query_with_fusion."""
        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryManager,
        )

        manager = SemanticQueryManager.__new__(SemanticQueryManager)

        with ExitStack() as stack:
            mock_fusion, _, _ = _enter_semantic_manager_patches(stack)

            manager._execute_temporal_query(
                repo_path=Path("/tmp/test-repo"),
                repository_alias="test-repo",
                query_text="auth code",
                limit=10,
                min_score=None,
                time_range=None,
                time_range_all=True,
                author="alice",
            )

        mock_fusion.assert_called_once()
        _, kwargs = mock_fusion.call_args
        assert kwargs.get("author") == "alice", (
            f"author='alice' must reach execute_temporal_query_with_fusion; "
            f"got kwargs keys={list(kwargs.keys())}"
        )

    def test_diff_type_string_converted_to_list(self):
        """diff_type plain string is converted to single-item list passed
        as diff_types to execute_temporal_query_with_fusion."""
        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryManager,
        )

        manager = SemanticQueryManager.__new__(SemanticQueryManager)

        with ExitStack() as stack:
            mock_fusion, _, _ = _enter_semantic_manager_patches(stack)

            manager._execute_temporal_query(
                repo_path=Path("/tmp/test-repo"),
                repository_alias="test-repo",
                query_text="auth code",
                limit=10,
                min_score=None,
                time_range=None,
                diff_type="added",
            )

        mock_fusion.assert_called_once()
        _, kwargs = mock_fusion.call_args
        assert kwargs.get("diff_types") == ["added"], (
            f"diff_type='added' must become diff_types=['added'] in fusion call; "
            f"got diff_types={kwargs.get('diff_types')}"
        )

    def test_comma_separated_diff_type_split_into_list(self):
        """Comma-separated diff_type string is split into list items."""
        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryManager,
        )

        manager = SemanticQueryManager.__new__(SemanticQueryManager)

        with ExitStack() as stack:
            mock_fusion, _, _ = _enter_semantic_manager_patches(stack)

            manager._execute_temporal_query(
                repo_path=Path("/tmp/test-repo"),
                repository_alias="test-repo",
                query_text="auth code",
                limit=10,
                min_score=None,
                time_range=None,
                diff_type="added,modified",
            )

        mock_fusion.assert_called_once()
        _, kwargs = mock_fusion.call_args
        assert kwargs.get("diff_types") == ["added", "modified"], (
            f"diff_type='added,modified' must become diff_types=['added', 'modified']; "
            f"got diff_types={kwargs.get('diff_types')}"
        )

    def test_chunk_type_forwarded_to_fusion(self):
        """chunk_type passed to _execute_temporal_query reaches
        execute_temporal_query_with_fusion."""
        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryManager,
        )

        manager = SemanticQueryManager.__new__(SemanticQueryManager)

        with ExitStack() as stack:
            mock_fusion, _, _ = _enter_semantic_manager_patches(stack)

            manager._execute_temporal_query(
                repo_path=Path("/tmp/test-repo"),
                repository_alias="test-repo",
                query_text="auth code",
                limit=10,
                min_score=None,
                time_range=None,
                chunk_type="function",
            )

        mock_fusion.assert_called_once()
        _, kwargs = mock_fusion.call_args
        assert kwargs.get("chunk_type") == "function", (
            f"chunk_type='function' must reach execute_temporal_query_with_fusion; "
            f"got kwargs={kwargs}"
        )
