"""
Tests for Bug #1219: #1202 fix regressed MCP search_code for single -global repos.

Root cause: `_execute_tracked_search` at search.py:954 assigns the full 2-tuple
return value of `_perform_search` to `results` without unpacking.
`_search_global_repo` then unpacks the outer 3-tuple correctly, but `results`
still holds `(List[QueryResult], str)`.  When it does:
    `[r.to_dict() for r in results]`
it iterates the 2-tuple: the first element is a `list`, so `list.to_dict()` raises
`AttributeError: 'list' object has no attribute 'to_dict'`.

Fix: at search.py:954, unpack the 2-tuple from `_perform_search`, mirroring the
`isinstance(_raw, tuple)` guard in `query_user_repositories`.

Coverage gap: Bug #1202 tests only exercised `query_user_repositories` (activated-
repo path).  The `_execute_tracked_search` -> `_search_global_repo` path was not
tested.  These tests close that gap.

Strategy: patch ONLY `_utils.app_module.semantic_query_manager._perform_search` at
the leaf so the real `_execute_tracked_search` / `_search_global_repo` routing
runs.  Also patch `_resolve_global_repo_target` to avoid filesystem/DB lookups.
"""

from pathlib import Path
from typing import Any, Dict, List, cast
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers.search import _execute_tracked_search
from code_indexer.server.query.semantic_query_manager import QueryResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user() -> User:
    user = MagicMock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    user.has_permission = MagicMock(return_value=True)
    user.max_results_per_query = None
    return user


def _make_query_result(file_path: str = "src/auth.py") -> QueryResult:
    return QueryResult(
        file_path=file_path,
        line_number=1,
        code_snippet="def authenticate(): pass",
        similarity_score=0.9,
        repository_alias="myrepo-global",
        source_repo=None,
        source_provider="voyage-ai",
    )


def _perform_search_2tuple(results: List[QueryResult], strategy: str = "primary_only"):
    """Simulate the CURRENT _perform_search return value: 2-tuple (list, str)."""
    return (results, strategy)


# ---------------------------------------------------------------------------
# AC1 — Bug reproduction: _execute_tracked_search must unpack the 2-tuple
# ---------------------------------------------------------------------------


class TestAC1_ExecuteTrackedSearchUnpack:
    """
    _execute_tracked_search calls _perform_search which returns (List[QueryResult], str).
    It must unpack to List[QueryResult] before returning (results, ms, timed_out).

    BEFORE FIX: results returned from _execute_tracked_search is the 2-tuple.
    AFTER FIX:  results is the List[QueryResult].
    """

    def test_execute_tracked_search_returns_list_not_tuple(self, tmp_path):
        """
        RED: When _perform_search returns a 2-tuple, _execute_tracked_search must
        return the unwrapped list as its first element, NOT the 2-tuple.

        Before fix: first element of returned 3-tuple is itself a 2-tuple -> crash
        downstream when caller does `r.to_dict()`.
        After fix:  first element is List[QueryResult].
        """
        fake_results = [_make_query_result()]
        user = _make_user()

        params: Dict[str, Any] = {
            "query_text": "authenticate",
            "search_mode": "semantic",
        }
        mock_user_repos = [
            {
                "user_alias": "myrepo-global",
                "repo_path": str(tmp_path),
                "actual_repo_id": "myrepo",
            }
        ]

        # Patch _perform_search to return the 2-tuple as the real implementation does
        with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
            mock_app.semantic_query_manager._perform_search.return_value = (
                _perform_search_2tuple(fake_results, "primary_only")
            )

            results, execution_time_ms, timeout_occurred, effective_strategy = (
                _execute_tracked_search(params, user, mock_user_repos, limit=10)
            )

        # After fix: results must be the List[QueryResult], not a tuple
        assert isinstance(results, list), (
            f"Expected list, got {type(results)}: {results!r}. "
            "This reproduces Bug #1219 — _execute_tracked_search returned the "
            "raw 2-tuple from _perform_search without unpacking."
        )
        assert len(results) == 1
        # After fix: r.to_dict() must NOT raise
        result_dict = results[0].to_dict()
        assert result_dict["file_path"] == "src/auth.py"

    def test_execute_tracked_search_backward_compat_plain_list(self, tmp_path):
        """
        Backward compat: if a test patches _perform_search to return a plain list
        (not a tuple), _execute_tracked_search must still return the list, not crash.
        """
        fake_results = [_make_query_result("src/legacy.py")]
        user = _make_user()

        params: Dict[str, Any] = {
            "query_text": "legacy",
            "search_mode": "semantic",
        }
        mock_user_repos = [
            {
                "user_alias": "myrepo-global",
                "repo_path": str(tmp_path),
                "actual_repo_id": "myrepo",
            }
        ]

        with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
            # Plain list (not a tuple) — backward compat path
            mock_app.semantic_query_manager._perform_search.return_value = fake_results

            results, execution_time_ms, timeout_occurred, effective_strategy = (
                _execute_tracked_search(params, user, mock_user_repos, limit=10)
            )

        assert isinstance(results, list)
        assert len(results) == 1


# ---------------------------------------------------------------------------
# AC2 — _search_global_repo does not crash with 'list has no to_dict'
# ---------------------------------------------------------------------------


def _make_rerank_meta() -> dict:
    return {
        "reranker_used": False,
        "reranker_provider": None,
        "rerank_time_ms": 0,
        "reranker_status": {"status": "disabled"},
    }


def _patch_global_repo_prereqs(tmp_path: Path, alias: str = "myrepo-global"):
    """Build a context manager stack that patches all non-SUT dependencies."""
    repo_entry = {"alias_name": alias, "repo_name": "myrepo"}
    target_path = str(tmp_path)

    patches = [
        patch(
            "code_indexer.server.mcp.handlers.search._resolve_global_repo_target",
            return_value=(repo_entry, target_path, None),
        ),
        patch(
            "code_indexer.server.mcp.handlers.search._apply_rerank_and_filter",
            side_effect=lambda results, params, req_limit, alias, user: (
                results,
                _make_rerank_meta(),
            ),
        ),
        patch(
            "code_indexer.server.mcp.handlers.search._load_category_map",
            return_value={},
        ),
        patch(
            "code_indexer.server.mcp.handlers.search._get_wiki_enabled_repos",
            return_value=set(),
        ),
        patch(
            "code_indexer.server.mcp.handlers.search._enrich_results_with_category",
        ),
        patch(
            "code_indexer.server.mcp.handlers.search._compute_effective_limit",
            side_effect=lambda req, user: req,
        ),
        patch(
            "code_indexer.server.mcp.handlers.search._compute_rerank_limit",
            side_effect=lambda params, req, eff: eff,
        ),
        patch(
            "code_indexer.server.mcp.handlers.search._get_query_tracker",
            return_value=None,
        ),
    ]
    return patches


class TestAC2_SearchGlobalRepoNoToDict:
    """
    _search_global_repo must return a successful response with results.
    Before fix: crashes with AttributeError 'list' has no 'to_dict'.
    After fix: returns dict with results list.
    """

    def _run_global_search(self, tmp_path: Path, search_mode: str) -> Dict[str, Any]:
        """
        Exercise _search_global_repo with a mocked _perform_search that returns
        a 2-tuple (as the real implementation does post-#1202).
        """
        from code_indexer.server.mcp.handlers.search import _search_global_repo

        fake_results = [_make_query_result()]
        user = _make_user()
        params: Dict[str, Any] = {
            "query_text": "authenticate",
            "search_mode": search_mode,
            "limit": 10,
        }

        patches = _patch_global_repo_prereqs(tmp_path)

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
        ):
            # _perform_search returns the 2-tuple as the real implementation does
            mock_app.semantic_query_manager._perform_search.return_value = (
                fake_results,
                "primary_only",
            )
            result = _search_global_repo(params, user, "myrepo-global")

        return cast(Dict[str, Any], result)

    def test_semantic_mode_returns_results_not_crash(self, tmp_path):
        """
        BUG REPRODUCTION (semantic): before fix, raises AttributeError.
        After fix: returns dict with results list.
        """
        result = self._run_global_search(tmp_path, "semantic")
        assert isinstance(result, dict), f"Expected dict response, got {type(result)}"
        # The response is an MCP envelope; drill into it to find results
        assert "results" in str(result), (
            f"Expected 'results' key in response, got: {result!r}"
        )

    def test_fts_mode_returns_results_not_crash(self, tmp_path):
        """
        BUG REPRODUCTION (fts): before fix, raises AttributeError.
        After fix: returns dict with results list.
        """
        result = self._run_global_search(tmp_path, "fts")
        assert isinstance(result, dict)
        assert "results" in str(result)

    def test_hybrid_mode_returns_results_not_crash(self, tmp_path):
        """
        BUG REPRODUCTION (hybrid): before fix, raises AttributeError.
        After fix: returns dict with results list.
        """
        result = self._run_global_search(tmp_path, "hybrid")
        assert isinstance(result, dict)
        assert "results" in str(result)

    def test_response_contains_correct_file_path(self, tmp_path):
        """
        After fix, the single result must have the correct file_path from the
        QueryResult, not a truncated/mangled value from iterating a tuple.
        """
        from code_indexer.server.mcp.handlers.search import _search_global_repo

        fake_results = [_make_query_result("src/controllers/auth.py")]
        user = _make_user()
        params: Dict[str, Any] = {
            "query_text": "authenticate",
            "search_mode": "semantic",
            "limit": 10,
        }

        patches = _patch_global_repo_prereqs(tmp_path)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
        ):
            mock_app.semantic_query_manager._perform_search.return_value = (
                fake_results,
                "primary_only",
            )
            result = _search_global_repo(params, user, "myrepo-global")

        # Drill into the MCP envelope to validate file_path
        result_str = str(result)
        assert "src/controllers/auth.py" in result_str, (
            f"Expected file path in result, got: {result_str!r}"
        )


# ---------------------------------------------------------------------------
# AC3 — AC7 echo: effective_search_mode + effective_query_strategy in global path
# ---------------------------------------------------------------------------


class TestAC3_GlobalPathAC7Echo:
    """
    After the fix, the global path's query_metadata should include
    effective_search_mode and effective_query_strategy (AC7 parity).
    """

    def test_effective_strategy_in_query_metadata(self, tmp_path):
        """
        After fix, the query_metadata in the _search_global_repo response
        must include effective_search_mode and effective_query_strategy so
        clients get AC7 routing transparency on global-repo searches.
        """
        from code_indexer.server.mcp.handlers.search import _search_global_repo

        fake_results = [_make_query_result()]
        user = _make_user()
        params: Dict[str, Any] = {
            "query_text": "authenticate",
            "search_mode": "semantic",
            "limit": 10,
        }

        patches = _patch_global_repo_prereqs(tmp_path)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
        ):
            mock_app.semantic_query_manager._perform_search.return_value = (
                fake_results,
                "dual_provider_parallel",
            )
            result = _search_global_repo(params, user, "myrepo-global")

        result_str = str(result)
        # After AC7 wiring: effective_query_strategy must appear in response
        assert "effective_query_strategy" in result_str, (
            "AC7 echo missing: 'effective_query_strategy' not in global-repo response. "
            f"Got: {result_str!r}"
        )
