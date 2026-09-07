"""Bug #1804 -- MCP search_code's parallel-strategy path must degrade
gracefully (epic #485) when every dispatched embedding provider is
unavailable, instead of surfacing `success: false`.

Fix: `_execute_tracked_search` (global-repo path) and `query_user_
repositories` (activated-repo path) both forward a `_provider_completeness_out`
out-param into `_perform_search` (semantic_query_manager.py). When the
underlying dispatch raises because every provider failed, it has ALREADY
populated that out-param with `completeness="providers_unavailable"` and a
`provider_errors` dict before raising (see
test_bug1804_provider_completeness_marker.py /
test_bug1804_completeness_plumbing.py at the manager layer). Both
`_search_global_repo` and `_search_activated_repo` catch the exception,
check the out-param, and -- ONLY when it carries the marker -- return a
graceful `success: true, results: [], completeness: "providers_unavailable"`
envelope (Rule 13 anti-silent-failure: the marker keeps this distinguishable
from a genuine zero-match query) instead of propagating the failure. A
failure WITHOUT the marker (e.g. "repository not found") must still
propagate unchanged.
"""

import json as _json
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole


def _make_user() -> User:
    user = MagicMock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    user.has_permission = MagicMock(return_value=True)
    user.max_results_per_query = None
    return user


def _both_providers_failed_side_effect(**kwargs):
    completeness_out = kwargs.get("_provider_completeness_out")
    if completeness_out is not None:
        completeness_out["completeness"] = "providers_unavailable"
        completeness_out["provider_errors"] = {
            "voyage-ai": "503 Service Unavailable",
            "cohere": "503 Service Unavailable",
        }
    raise Exception(
        "Semantic search failed for repository 'markupsafe-global' -- "
        "every dispatched embedding provider failed: voyage-ai: 503; cohere: 503"
    )


def _unrelated_failure_side_effect(**kwargs):
    # A real failure that has NOTHING to do with provider unavailability --
    # the out-param stays untouched, exactly as an unrelated SemanticQueryError
    # (e.g. "repository not found") would leave it.
    raise Exception("Repository 'markupsafe-global' not found for user 'testuser'")


def _assert_graceful_degradation(result: Dict[str, Any]) -> None:
    """Shared contract assertion: an MCP response degraded gracefully due to
    total provider unavailability -- success:true, empty results, and the
    completeness marker + per-provider error detail present."""
    parsed = _json.loads(result["content"][0]["text"])
    assert parsed["success"] is True, (
        f"Expected graceful success:true degradation, got: {parsed!r}"
    )
    assert parsed["results"]["results"] == []
    assert (
        parsed["results"]["query_metadata"]["completeness"] == "providers_unavailable"
    )
    provider_errors = parsed["results"]["query_metadata"]["provider_errors"]
    assert "voyage-ai" in provider_errors
    assert "cohere" in provider_errors


def _make_rerank_meta() -> dict:
    return {
        "reranker_used": False,
        "reranker_provider": None,
        "rerank_time_ms": 0,
        "reranker_status": {"status": "disabled"},
    }


def _enter_global_search_helper_patches(stack: ExitStack) -> None:
    """Patch the pure helper functions `_search_global_repo` calls after
    dispatch (rerank, category enrichment) -- not exercised on the
    degradation path, but required for the propagate-unchanged test to
    reach the SUT without unrelated collaborators erroring out first."""
    stack.enter_context(
        patch(
            "code_indexer.server.mcp.handlers.search._apply_rerank_and_filter",
            side_effect=lambda results, params, req_limit, alias, user: (
                results,
                _make_rerank_meta(),
            ),
        )
    )
    stack.enter_context(
        patch(
            "code_indexer.server.mcp.handlers.search._load_category_map",
            return_value={},
        )
    )
    stack.enter_context(
        patch(
            "code_indexer.server.mcp.handlers.search._get_wiki_enabled_repos",
            return_value=set(),
        )
    )
    stack.enter_context(
        patch("code_indexer.server.mcp.handlers.search._enrich_results_with_category")
    )
    stack.enter_context(
        patch(
            "code_indexer.server.mcp.handlers.search._compute_effective_limit",
            side_effect=lambda req, user: req,
        )
    )
    stack.enter_context(
        patch(
            "code_indexer.server.mcp.handlers.search._compute_rerank_limit",
            side_effect=lambda params, req, eff: eff,
        )
    )


def _enter_global_repo_prereqs(
    stack: ExitStack, tmp_path: Path, alias: str
) -> MagicMock:
    """Apply all non-SUT patches for `_search_global_repo` on `stack` and
    return the mocked `_utils.app_module` for the caller to configure."""
    repo_entry = {"alias_name": alias, "repo_name": "myrepo"}
    target_path = str(tmp_path)

    stack.enter_context(
        patch(
            "code_indexer.server.mcp.handlers.search._resolve_global_repo_target",
            return_value=(repo_entry, target_path, None),
        )
    )
    _enter_global_search_helper_patches(stack)
    stack.enter_context(
        patch(
            "code_indexer.server.mcp.handlers.search._get_query_tracker",
            return_value=None,
        )
    )
    return stack.enter_context(
        patch("code_indexer.server.mcp.handlers._utils.app_module")
    )


class TestGlobalRepoDegradesGracefullyOnProviderUnavailable:
    def test_both_providers_failed_returns_success_true_with_marker(self, tmp_path):
        from code_indexer.server.mcp.handlers.search import _search_global_repo

        user = _make_user()
        params: Dict[str, Any] = {
            "query_text": "escape",
            "search_mode": "semantic",
            "query_strategy": "parallel",
            "limit": 10,
        }

        with ExitStack() as stack:
            mock_app = _enter_global_repo_prereqs(stack, tmp_path, "myrepo-global")
            mock_app.semantic_query_manager._perform_search.side_effect = (
                _both_providers_failed_side_effect
            )
            result = _search_global_repo(params, user, "myrepo-global")

        _assert_graceful_degradation(result)

    def test_unrelated_failure_still_propagates(self, tmp_path):
        """A failure with no completeness marker (e.g. repo not found) must
        NOT be swallowed into a false success."""
        from code_indexer.server.mcp.handlers.search import _search_global_repo

        user = _make_user()
        params: Dict[str, Any] = {
            "query_text": "escape",
            "search_mode": "semantic",
            "limit": 10,
        }

        with ExitStack() as stack:
            mock_app = _enter_global_repo_prereqs(stack, tmp_path, "myrepo-global")
            mock_app.semantic_query_manager._perform_search.side_effect = (
                _unrelated_failure_side_effect
            )
            with pytest.raises(Exception, match="not found"):
                _search_global_repo(params, user, "myrepo-global")


def _make_real_user() -> User:
    return User(
        username="testuser",
        role=UserRole.ADMIN,
        email="test@example.com",
        password_hash="fakehash",
        created_at=datetime.now(timezone.utc),
    )


def _enter_activated_repo_prereqs(stack: ExitStack, tmp_path: Path) -> MagicMock:
    """Apply all non-SUT patches for `_search_activated_repo` on `stack`
    (mirrors test_search_1458_activated_repo_query_tracker.py's established
    pattern) and return the mocked `_utils.app_module`."""
    mock_app = stack.enter_context(
        patch("code_indexer.server.mcp.handlers._utils.app_module")
    )
    mock_app.app.state = SimpleNamespace(query_tracker=None)
    mock_app.activated_repo_manager = MagicMock()
    mock_app.activated_repo_manager.activated_repos_dir = str(
        tmp_path / "activated-repos"
    )

    stack.enter_context(
        patch(
            "code_indexer.server.mcp.handlers.search._load_category_map",
            return_value={},
        )
    )
    mock_cfg_svc = stack.enter_context(
        patch("code_indexer.server.mcp.handlers.search.get_config_service")
    )
    mock_mem_cfg = MagicMock()
    mock_mem_cfg.memory_retrieval_enabled = False
    mock_mem_cfg_obj = MagicMock()
    mock_mem_cfg_obj.memory_retrieval_config = mock_mem_cfg
    mock_cfg_svc.return_value.get_config.return_value = mock_mem_cfg_obj
    return mock_app


class TestActivatedRepoDegradesGracefullyOnProviderUnavailable:
    def test_both_providers_failed_returns_success_true_with_marker(self, tmp_path):
        from code_indexer.server.mcp.handlers.search import _search_activated_repo

        user = _make_real_user()
        params: Dict[str, Any] = {
            "query_text": "escape",
            "repository_alias": "my-repo",
            "query_strategy": "parallel",
            "limit": 10,
        }

        with ExitStack() as stack:
            mock_app = _enter_activated_repo_prereqs(stack, tmp_path)
            mock_app.semantic_query_manager.query_user_repositories.side_effect = (
                _both_providers_failed_side_effect
            )
            result = _search_activated_repo(params, user)

        _assert_graceful_degradation(result)

    def test_unrelated_failure_still_propagates(self, tmp_path):
        from code_indexer.server.mcp.handlers.search import _search_activated_repo

        user = _make_real_user()
        params: Dict[str, Any] = {
            "query_text": "escape",
            "repository_alias": "my-repo",
            "limit": 10,
        }

        with ExitStack() as stack:
            mock_app = _enter_activated_repo_prereqs(stack, tmp_path)
            mock_app.semantic_query_manager.query_user_repositories.side_effect = (
                _unrelated_failure_side_effect
            )
            with pytest.raises(Exception, match="not found"):
                _search_activated_repo(params, user)
