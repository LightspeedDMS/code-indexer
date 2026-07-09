"""Bug #1298 — REST/MCP temporal unindexed-override warning must name the
embedder, not a generic "temporal index not available" message.

When a temporal search issues an EXPLICIT `temporal_embedder` override to an
embedder that has NO indexed collections, the anti-fallback behavior is
correct (0 results, no silent redirect to the active embedder — AC8 of Story
#1291). The defect is purely cosmetic: the warning surfaced through
`query_user_repositories` (the shared REST `/api/query` + MCP `search_code`
single-repo entry point, per `test_temporal_embedder_override_server_wiring_
1291.py`) is the GENERIC "Temporal index not available for this repository..."
built at the `query_user_repositories` level, instead of the embedder-specific
"Temporal embedder 'X' has no indexed collections" message that
`execute_temporal_query_with_fusion` (temporal_fusion_dispatch.py) already
produces and attaches to `TemporalSearchResults.warning`.

This test mocks only the heavy lazy imports inside `_execute_temporal_query`
(ConfigManager, BackendFactory, execute_temporal_query_with_fusion) — exactly
the pattern used by test_temporal_embedder_override_server_wiring_1291.py —
so the REAL `query_user_repositories` -> `_perform_search` ->
`_search_single_repository` -> `_execute_temporal_query` chain executes,
proving the warning text is lost/genericized along that real path.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack
from unittest.mock import MagicMock, patch


_SPECIFIC_WARNING = (
    "Temporal embedder 'embed-v4.0' has no indexed collections. Run cidx "
    "index --index-commits with temporal.embedders including 'embed-v4.0' "
    "first."
)


def _enter_semantic_manager_patches(stack: ExitStack):
    """Patch the three heavy lazy imports inside _execute_temporal_query so
    it returns a typed embedder-specific empty result (AC8 path)."""
    mock_fusion = stack.enter_context(
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch"
            ".execute_temporal_query_with_fusion"
        )
    )
    mock_cm_cls = stack.enter_context(
        patch("code_indexer.proxy.config_manager.ConfigManager")
    )
    mock_bf = stack.enter_context(
        patch("code_indexer.backends.backend_factory.BackendFactory")
    )

    mock_config = MagicMock()
    mock_cm_instance = MagicMock()
    mock_cm_instance.get_config.return_value = mock_config
    mock_cm_cls.create_with_backtrack.return_value = mock_cm_instance

    mock_backend = MagicMock()
    mock_vector_store = MagicMock()
    mock_backend.get_vector_store_client.return_value = mock_vector_store
    mock_bf.create.return_value = mock_backend

    fake_results = MagicMock()
    fake_results.results = []
    fake_results.warning = _SPECIFIC_WARNING
    fake_results.query = "auth code"
    fake_results.filter_type = "none"
    fake_results.filter_value = None
    fake_results.total_found = 0
    mock_fusion.return_value = fake_results

    return mock_fusion, mock_cm_cls, mock_bf


class TestQueryUserRepositoriesEmbedderSpecificWarning:
    """query_user_repositories() must surface the embedder-specific warning
    from the fusion-dispatch layer, not a generic re-derived message, when an
    explicit temporal_embedder override has no indexed collections."""

    def test_unindexed_embedder_override_surfaces_specific_warning(
        self, monkeypatch, tmp_path
    ):
        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryManager,
        )

        manager = SemanticQueryManager.__new__(SemanticQueryManager)
        manager.max_results_per_query = 50
        manager.logger = logging.getLogger("test.semantic_query_manager")

        class _FakeARM:
            activated_repos_dir = str(tmp_path / "activated-repos")

            def list_activated_repositories(self, _u):
                return [{"user_alias": "myrepo", "repo_path": str(tmp_path)}]

            def get_activated_repo_path(self, _u, _a):
                return str(tmp_path)

        manager.activated_repo_manager = _FakeARM()

        import code_indexer.server.app as _app_mod

        class _FakeState:
            backend_registry = None
            http_client_factory = None

        class _FakeApp:
            state = _FakeState()

        monkeypatch.setattr(_app_mod, "app", _FakeApp(), raising=False)

        with ExitStack() as stack:
            _enter_semantic_manager_patches(stack)

            result = manager.query_user_repositories(
                username="alice",
                query_text="auth code",
                repository_alias="myrepo",
                time_range_all=True,
                query_strategy="primary_only",
                temporal_embedder="embed-v4.0",
            )

        # AC8 (anti-fallback): still 0 results, no silent redirect.
        assert result["results"] == []
        assert result["total_results"] == 0

        warning = result.get("warning")
        assert warning is not None, (
            "Expected a warning explaining why 0 temporal results were "
            "returned for the unindexed embedder override"
        )
        assert "embed-v4.0" in warning, (
            "Bug #1298: warning must name the requested embedder "
            f"('embed-v4.0'); got: {warning!r}"
        )
        assert "has no indexed collections" in warning, (
            "Bug #1298: warning must use the embedder-specific "
            f"'has no indexed collections' message; got: {warning!r}"
        )
        assert "Temporal index not available for this repository" not in warning, (
            "Bug #1298: warning must NOT be the generic message when an "
            f"explicit unindexed embedder override was requested; got: {warning!r}"
        )
