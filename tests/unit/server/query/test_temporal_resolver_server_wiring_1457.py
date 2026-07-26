"""Story #1457 AC1/AC2 live wiring investigation: threading a real
TemporalShardResolver from SemanticQueryManager's confirmed golden-repos
convention into execute_temporal_query_with_fusion.

Round 8 confirmed SemanticQueryManager._search_single_repository's
`is_global` branch already constructs the EXACT golden_repos_dir/
AliasManager pattern AC2's build_dedicated_temporal_read_store uses:
    data_dir = Path(activated_repo_manager.activated_repos_dir).parent
    golden_repos_dir = data_dir / "golden-repos"

This file proves _execute_temporal_query (the direct dispatch call site,
mirroring test_temporal_embedder_override_server_wiring_1291.py's exact
established pattern for threading a new optional param through this same
call site) can construct a REAL TemporalShardResolver from a
`golden_repo_alias` and forward it to execute_temporal_query_with_fusion --
gated on BOTH golden_repo_alias being provided AND a real query_tracker
being present on the manager instance (constructing a resolver WITHOUT a
query_tracker would make pin() a true no-op, silently reintroducing the
mid-read deletion hazard AC8 Step 6 exists to prevent -- so the safety gate
itself is part of what this test proves).

Follows the exact `SemanticQueryManager.__new__(...)` + patched-heavy-
collaborators convention already established in
test_temporal_embedder_override_server_wiring_1291.py. The resolver
CONSTRUCTION itself is real (not mocked) -- it is the actual thing under
test.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch


def _enter_semantic_manager_patches(stack: ExitStack):
    """Patch the three heavy lazy imports inside _execute_temporal_query
    (identical to the established pattern in
    test_temporal_embedder_override_server_wiring_1291.py)."""
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
    fake_results.warning = None
    fake_results.query = "test"
    fake_results.filter_type = "none"
    fake_results.filter_value = None
    fake_results.total_found = 0
    mock_fusion.return_value = fake_results

    return mock_fusion, mock_cm_cls, mock_bf


def _make_manager(tmp_path: Path, query_tracker):
    from code_indexer.server.query.semantic_query_manager import (
        SemanticQueryManager,
    )

    manager = SemanticQueryManager.__new__(SemanticQueryManager)
    manager.query_tracker = query_tracker
    manager.activated_repo_manager = MagicMock()
    manager.activated_repo_manager.activated_repos_dir = str(
        tmp_path / "activated-repos"
    )
    return manager


class TestExecuteTemporalQueryConstructsResolverForGoldenRepoAlias:
    def test_golden_repo_alias_with_query_tracker_constructs_real_resolver(
        self, tmp_path
    ):
        from code_indexer.global_repos.query_tracker import QueryTracker
        from code_indexer.services.temporal.temporal_shard_resolver import (
            TemporalShardResolver,
        )

        manager = _make_manager(tmp_path, QueryTracker())
        repo_path = tmp_path / "golden-repos" / "evolution"

        with ExitStack() as stack:
            mock_fusion, _, _ = _enter_semantic_manager_patches(stack)

            manager._execute_temporal_query(
                repo_path=repo_path,
                repository_alias="evolution",
                query_text="auth code",
                limit=10,
                min_score=None,
                time_range=None,
                time_range_all=True,
                golden_repo_alias="evolution",
            )

        mock_fusion.assert_called_once()
        _, kwargs = mock_fusion.call_args
        resolver = kwargs.get("resolver")
        assert resolver is not None
        assert isinstance(resolver, TemporalShardResolver)
        assert resolver._repo_alias == "evolution"
        assert resolver._sister_root == tmp_path / "golden-repos"
        assert resolver._legacy_index_path == repo_path / ".code-indexer" / "index"

    def test_golden_repo_alias_with_global_suffix_is_normalized(self, tmp_path):
        """Story #1457 HIGH #7 (2026-07-23 code review): an is_global
        query passes its full '-global'-suffixed user_alias as
        golden_repo_alias (query_user_repositories's is_global branch),
        but maybe_relocate_shard_to_sister_location ALWAYS publishes
        under the bare codebase_dir.name (golden repo directories are
        never named with '-global' -- that suffix is purely a query-
        facing alias-registry convention). Without normalization, a
        global query's resolver constructs a namespace that can NEVER
        match what was published -- exactly one '-global' suffix must be
        stripped before constructing the resolver."""
        from code_indexer.global_repos.query_tracker import QueryTracker
        from code_indexer.services.temporal.temporal_shard_resolver import (
            TemporalShardResolver,
        )

        manager = _make_manager(tmp_path, QueryTracker())
        repo_path = tmp_path / "golden-repos" / "evolution"

        with ExitStack() as stack:
            mock_fusion, _, _ = _enter_semantic_manager_patches(stack)

            manager._execute_temporal_query(
                repo_path=repo_path,
                repository_alias="evolution-global",
                query_text="auth code",
                limit=10,
                min_score=None,
                time_range=None,
                time_range_all=True,
                golden_repo_alias="evolution-global",
            )

        _, kwargs = mock_fusion.call_args
        resolver = kwargs.get("resolver")
        assert resolver is not None
        assert isinstance(resolver, TemporalShardResolver)
        assert resolver._repo_alias == "evolution", (
            "the resolver's repo_alias must have the '-global' suffix "
            "stripped -- it must match the bare golden-repo directory "
            "name the publish side uses, not the query-facing alias"
        )

    def test_golden_repo_alias_omitted_no_resolver_byte_identical(self, tmp_path):
        from code_indexer.global_repos.query_tracker import QueryTracker

        manager = _make_manager(tmp_path, QueryTracker())
        repo_path = tmp_path / "activated" / "evolution"

        with ExitStack() as stack:
            mock_fusion, _, _ = _enter_semantic_manager_patches(stack)

            manager._execute_temporal_query(
                repo_path=repo_path,
                repository_alias="evolution",
                query_text="auth code",
                limit=10,
                min_score=None,
                time_range=None,
                time_range_all=True,
            )

        _, kwargs = mock_fusion.call_args
        assert kwargs.get("resolver") is None

    def test_no_query_tracker_no_resolver_even_with_golden_repo_alias(self, tmp_path):
        """Safety gate: without a real query_tracker, a constructed resolver's
        pin() would be a true no-op (per its own documented CLI/solo
        semantics) -- silently reintroducing the mid-read deletion hazard.
        Resolver construction is therefore gated on BOTH conditions."""
        manager = _make_manager(tmp_path, None)
        repo_path = tmp_path / "golden-repos" / "evolution"

        with ExitStack() as stack:
            mock_fusion, _, _ = _enter_semantic_manager_patches(stack)

            manager._execute_temporal_query(
                repo_path=repo_path,
                repository_alias="evolution",
                query_text="auth code",
                limit=10,
                min_score=None,
                time_range=None,
                time_range_all=True,
                golden_repo_alias="evolution",
            )

        _, kwargs = mock_fusion.call_args
        assert kwargs.get("resolver") is None
