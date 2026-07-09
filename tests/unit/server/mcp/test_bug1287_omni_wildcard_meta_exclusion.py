"""
Unit tests for Bug #1287 Defect B, code-reviewer finding 2 — the omni `*`
wildcard fan-out path bypassed the fix applied to
SemanticQueryManager.query_user_repositories().

search_code(query, repository_alias="*", search_mode="fts") routes through
_omni_search_code() -> _expand_wildcard_patterns() -> MultiSearchService.
_expand_wildcard_patterns() builds its available-repo list from
_list_global_repos() (which includes "cidx-meta-global"), so a bare `*`
pattern matches it, and it is handed to MultiSearchService alongside real
repos. MultiSearchService then attempts an FTS lookup for cidx-meta-global,
which has no Tantivy index by design, producing per-repo WARNING/ERROR log
noise (REPO-GENERAL-024 / REPO-GENERAL-026) for a benign, by-design
condition -- exactly the noise Bug #1287 targets, just via a different code
path than the one already fixed in semantic_query_manager.py.

These tests prove:

  - _expand_wildcard_patterns(), when passed search_mode="fts" or "hybrid",
    excludes cidx-meta* bookkeeping repos from WILDCARD matches while still
    returning real repos matched by the same wildcard.
  - The exclusion does NOT apply to search_mode="semantic" (default) --
    "keep semantic-only * behavior unchanged" per the review.
  - The exclusion does NOT apply to LITERAL (non-wildcard) aliases -- an
    explicit ask for "cidx-meta-global" in a literal list still passes
    through unfiltered (fails loud downstream), matching the semantics
    already established in semantic_query_manager.py.
  - End-to-end through _omni_search_code(): a `*` fan-out with search_mode
    "fts" never hands cidx-meta-global to MultiSearchService, while real
    repos are still passed through for searching.

No mocking of the code under test (_expand_wildcard_patterns,
_omni_search_code, the anchored is_internal_meta_repo predicate). Only the
external collaborators (_list_global_repos, _get_golden_repos_dir,
_get_access_filtering_service, get_config_service, MultiSearchService) are
test doubles, per the existing test_wildcard_cap.py /
test_omni_search_truncation.py conventions in this package.
"""

import tempfile
from datetime import datetime
from typing import Any, List
from unittest.mock import MagicMock, Mock, patch

from code_indexer.server.auth.user_manager import User, UserRole

REAL_REPO_ALPHA = "repo-alpha-global"
REAL_REPO_BETA = "repo-beta-global"
META_REPO = "cidx-meta-global"
WILDCARD_PATTERN = "*-global"


def _make_user(username: str = "alice") -> User:
    return User(
        username=username,
        password_hash="hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


def _make_cap_config(cap: int = 50) -> MagicMock:
    mock_config_svc = MagicMock()
    mock_config_svc.get_config.return_value.multi_search_limits_config.omni_wildcard_expansion_cap = cap
    return mock_config_svc


def _fake_global_repos(aliases: List[str]) -> List[dict]:
    return [{"alias_name": alias} for alias in aliases]


def _run_expand(patterns: List[str], fake_repos: List[dict], **kwargs: Any):
    """Run the REAL _expand_wildcard_patterns with external deps patched."""
    from code_indexer.server.mcp.handlers._utils import _expand_wildcard_patterns

    with tempfile.TemporaryDirectory() as fake_golden_dir:
        with (
            patch(
                "code_indexer.server.mcp.handlers._utils._list_global_repos",
                return_value=fake_repos,
            ),
            patch(
                "code_indexer.server.mcp.handlers._utils._get_golden_repos_dir",
                return_value=fake_golden_dir,
            ),
            patch(
                "code_indexer.server.mcp.handlers._utils._get_access_filtering_service",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._utils.get_config_service",
                return_value=_make_cap_config(),
            ),
        ):
            return _expand_wildcard_patterns(patterns, _make_user(), **kwargs)


class TestExpandWildcardPatternsExcludesMetaRepoForFtsHybrid:
    """_expand_wildcard_patterns must exclude cidx-meta* from WILDCARD
    matches when search_mode requires an FTS index."""

    def test_fts_wildcard_excludes_meta_repo_keeps_real_repos(self):
        result = _run_expand(
            [WILDCARD_PATTERN],
            _fake_global_repos([META_REPO, REAL_REPO_ALPHA, REAL_REPO_BETA]),
            search_mode="fts",
        )
        assert META_REPO not in result, (
            f"cidx-meta-global must be excluded from an fts wildcard fan-out, got: {result}"
        )
        assert REAL_REPO_ALPHA in result and REAL_REPO_BETA in result, (
            f"Real repos must still be included in the fts wildcard fan-out, got: {result}"
        )

    def test_hybrid_wildcard_excludes_meta_repo_keeps_real_repos(self):
        result = _run_expand(
            [WILDCARD_PATTERN],
            _fake_global_repos([META_REPO, REAL_REPO_ALPHA, REAL_REPO_BETA]),
            search_mode="hybrid",
        )
        assert META_REPO not in result
        assert REAL_REPO_ALPHA in result and REAL_REPO_BETA in result

    def test_semantic_wildcard_still_includes_meta_repo(self):
        """Keep semantic-only `*` behavior unchanged -- cidx-meta must
        remain reachable for semantic/memory search."""
        result = _run_expand(
            [WILDCARD_PATTERN],
            _fake_global_repos([META_REPO, REAL_REPO_ALPHA]),
            search_mode="semantic",
        )
        assert META_REPO in result, (
            "semantic search_mode must NOT exclude cidx-meta-global from wildcard expansion"
        )

    def test_default_search_mode_is_semantic_and_unaffected(self):
        """Backward compatibility: calling without search_mode (existing
        call sites, e.g. _omni_regex_search) must behave exactly as before
        -- no filtering applied."""
        result = _run_expand(
            [WILDCARD_PATTERN],
            _fake_global_repos([META_REPO, REAL_REPO_ALPHA]),
        )
        assert META_REPO in result

    def test_literal_meta_repo_alias_not_excluded_by_fts_filter(self):
        """An explicit LITERAL alias for cidx-meta-global (not a wildcard
        match) must pass through unfiltered even under search_mode='fts' --
        exclusion applies only to matches produced by wildcard expansion,
        preserving 'explicit ask fails loud' semantics."""
        result = _run_expand(
            [META_REPO],
            _fake_global_repos([META_REPO, REAL_REPO_ALPHA]),
            search_mode="fts",
        )
        assert result == [META_REPO], (
            f"Literal cidx-meta-global alias must pass through unchanged, got: {result}"
        )


class TestOmniSearchCodeExcludesMetaRepoFromFanout:
    """End-to-end through _omni_search_code(): the `*` fan-out under fts
    search_mode must never hand cidx-meta-global to MultiSearchService."""

    def test_omni_star_fts_never_searches_cidx_meta_global(self):
        from code_indexer.server.mcp import handlers
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        captured_requests: List[Any] = []

        def fake_search(request):
            captured_requests.append(request)
            return MultiSearchResponse(
                results={
                    repo: [] for repo in request.repositories if repo != META_REPO
                },
                metadata=MultiSearchMetadata(
                    total_results=0,
                    total_repos_searched=len(request.repositories),
                    execution_time_ms=5,
                ),
                errors=None,
            )

        mock_service = Mock()
        mock_service.search = Mock(side_effect=fake_search)

        with (
            patch(
                "code_indexer.server.multi.multi_search_service.MultiSearchService"
            ) as mock_service_class,
            patch.object(handlers, "get_config_service") as mock_get_config,
            patch.object(
                handlers._utils,
                "_list_global_repos",
                return_value=_fake_global_repos(
                    [META_REPO, REAL_REPO_ALPHA, REAL_REPO_BETA]
                ),
            ),
            patch.object(
                handlers._utils, "_get_golden_repos_dir", return_value="/fake/golden"
            ),
            patch.object(
                handlers._utils, "_get_access_filtering_service", return_value=None
            ),
        ):
            mock_config_service = Mock()
            mock_limits = Mock()
            mock_limits.multi_search_max_workers = 4
            mock_limits.multi_search_timeout_seconds = 30
            mock_limits.omni_wildcard_expansion_cap = 50
            mock_config = Mock()
            mock_config.multi_search_limits_config = mock_limits
            mock_config_service.get_config.return_value = mock_config
            mock_get_config.return_value = mock_config_service

            mock_service_class.get_instance.return_value = mock_service

            params = {
                "repository_alias": [WILDCARD_PATTERN],
                "query_text": "test",
                "search_mode": "fts",
                "limit": 10,
            }
            handlers._omni_search_code(params, _make_user())

        assert len(captured_requests) == 1, (
            "MultiSearchService.search must be called exactly once"
        )
        searched_repos = captured_requests[0].repositories
        assert META_REPO not in searched_repos, (
            f"cidx-meta-global must never reach MultiSearchService for an fts fan-out, "
            f"got repositories={searched_repos}"
        )
        assert REAL_REPO_ALPHA in searched_repos and REAL_REPO_BETA in searched_repos, (
            f"Real repos must still be searched, got repositories={searched_repos}"
        )
