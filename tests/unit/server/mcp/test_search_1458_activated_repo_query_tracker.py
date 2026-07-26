"""_search_activated_repo() wires QueryTracker ref-counting around its read
(Story #1458 AC13 gap (b) -- explicit prerequisite for the deactivation
refcount-aware drain).

Verified against the real code (Story #1458 AC13): the ONLY production
increment_ref/decrement_ref call site was `_execute_tracked_search` (used
solely by `_search_global_repo`, the `-global`/golden-repo branch) --
`_search_activated_repo` called `query_user_repositories` directly with NO
ref-counting at all. This closes that gap, using the SAME original-path-key
format (`{activated_repos_dir}/{username}/{repository_alias}`) the
deactivation drain polls (matching `_do_deactivate_single`'s `repo_dir`
construction exactly).

Real `QueryTracker` (not mocked) -- only `app_module`'s other collaborators
are mocked, mirroring the SAME established pattern already used by
test_bug1202_fts_dual_provider_preemption.py's MCP-handler-level tests.
"""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from code_indexer.global_repos.query_tracker import QueryTracker


class TestSearchActivatedRepoQueryTrackerWiring:
    def test_refcount_incremented_during_query_and_decremented_after(
        self, tmp_path: Path
    ):
        real_query_tracker = QueryTracker()
        activated_repos_dir = str(tmp_path / "activated-repos")
        expected_key = f"{activated_repos_dir}/testuser/my-repo"

        observed_refcount_during_call = {}

        def fake_query_user_repositories(**kwargs):
            observed_refcount_during_call["value"] = real_query_tracker.get_ref_count(
                expected_key
            )
            return {
                "results": [],
                "total_results": 0,
                "query_metadata": {
                    "query_text": "test",
                    "execution_time_ms": 1,
                    "repositories_searched": 1,
                    "timeout_occurred": False,
                },
            }

        with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
            mock_app.app.state = SimpleNamespace(query_tracker=real_query_tracker)
            mock_app.semantic_query_manager.query_user_repositories.side_effect = (
                fake_query_user_repositories
            )
            mock_app.activated_repo_manager = MagicMock()
            mock_app.activated_repo_manager.activated_repos_dir = activated_repos_dir

            from code_indexer.server.auth.user_manager import User, UserRole

            user = User(
                username="testuser",
                role=UserRole.ADMIN,
                email="test@example.com",
                password_hash="fakehash",
                created_at=datetime.now(timezone.utc),
            )

            with (
                patch(
                    "code_indexer.server.mcp.handlers.search._apply_rerank_and_filter",
                    return_value=(
                        [],
                        {
                            "reranker_used": False,
                            "reranker_provider": None,
                            "rerank_time_ms": 0,
                            "reranker_status": {"status": "disabled"},
                        },
                    ),
                ),
                patch(
                    "code_indexer.server.mcp.handlers.search._run_memory_retrieval",
                    return_value=None,
                ),
                patch(
                    "code_indexer.server.mcp.handlers.search._load_category_map",
                    return_value={},
                ),
                patch(
                    "code_indexer.server.mcp.handlers.search.get_config_service"
                ) as mock_cfg_svc,
            ):
                mock_mem_cfg = MagicMock()
                mock_mem_cfg.memory_retrieval_enabled = False
                mock_mem_cfg_obj = MagicMock()
                mock_mem_cfg_obj.memory_retrieval_config = mock_mem_cfg
                mock_cfg_svc.return_value.get_config.return_value = mock_mem_cfg_obj

                from code_indexer.server.mcp.handlers.search import (
                    _search_activated_repo,
                )

                _search_activated_repo(
                    {
                        "query_text": "test",
                        "repository_alias": "my-repo",
                        "limit": 5,
                    },
                    user,
                )

        # Refcount was >0 WHILE the query executed...
        assert observed_refcount_during_call.get("value") == 1
        # ...and is back to 0 after the query completes (finally-block decrement).
        assert real_query_tracker.get_ref_count(expected_key) == 0
