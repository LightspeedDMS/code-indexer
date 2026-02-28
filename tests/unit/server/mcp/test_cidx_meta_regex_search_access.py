"""
Unit tests for Bug #337: regex_search on cidx-meta filters unauthorized repo
description content for non-admin users.

AC1: Non-admin users calling regex_search on cidx-meta only see matches from
     authorized repo files.
AC2: Admin users retain full regex_search access to all cidx-meta files.
AC3: Unit tests verify regex_search cidx-meta filtering.

TDD: Tests written FIRST before implementation (red phase).
"""

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from code_indexer.server.mcp.handlers import handle_regex_search

from .conftest import extract_mcp_data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_regex_match(file_path: str, line_content: str = "description content") -> MagicMock:
    """Return a simulated RegexMatch dataclass-like object."""
    m = MagicMock()
    m.file_path = file_path
    m.line_number = 1
    m.column = 0
    m.line_content = line_content
    m.context_before = []
    m.context_after = []
    return m


def make_search_result(matches):
    """Return a simulated RegexSearchResult object."""
    result = MagicMock()
    result.matches = matches
    result.total_matches = len(matches)
    result.truncated = False
    result.search_engine = "ripgrep"
    result.search_time_ms = 5
    return result


def _run(coro):
    """Run a coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


@contextmanager
def _patch_regex_search_infrastructure(repo_alias: str, search_result, access_filtering_service=None):
    """
    Context manager that patches all required infrastructure for handle_regex_search
    unit tests, isolating from the real server state.

    Patches:
    - _get_golden_repos_dir: returns a fake path
    - _resolve_repo_path: returns a fake resolved path
    - _get_access_filtering_service: returns provided service or None
    - RegexSearchService: returns an AsyncMock with the given result
    - _get_wiki_enabled_repos: returns empty set
    - _apply_regex_payload_truncation: identity function
    - get_config_service: returns a mock config with timeout/workers
    """
    mock_service = AsyncMock()
    mock_service.search.return_value = search_result

    repo_path = f"/fake/path/{repo_alias}"

    with patch(
        "code_indexer.server.mcp.handlers._get_golden_repos_dir",
        return_value="/fake/golden-repos",
    ):
        with patch(
            "code_indexer.server.mcp.handlers._resolve_repo_path",
            return_value=repo_path,
        ):
            with patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_filtering_service,
            ):
                with patch(
                    "code_indexer.global_repos.regex_search.RegexSearchService",
                    return_value=mock_service,
                ):
                    with patch(
                        "code_indexer.server.mcp.handlers._get_wiki_enabled_repos",
                        return_value=set(),
                    ):
                        with patch(
                            "code_indexer.server.mcp.handlers._apply_regex_payload_truncation",
                            side_effect=lambda x: x,
                        ):
                            with patch(
                                "code_indexer.server.services.config_service.get_config_service"
                            ) as mock_cfg:
                                mock_cfg.return_value.get_config.return_value.search_limits_config.timeout_seconds = 30
                                mock_cfg.return_value.get_config.return_value.background_jobs_config.subprocess_max_workers = 2
                                yield


# ---------------------------------------------------------------------------
# AC1 + AC3: Non-admin user sees only authorized repo files in regex_search
# ---------------------------------------------------------------------------


class TestRegexSearchCidxMetaAccessFilteringNonAdmin:
    """AC1 + AC3: regex_search on cidx-meta filters unauthorized repo .md files."""

    def test_regular_user_sees_only_non_repo_md_matches(
        self, regular_user, access_filtering_service
    ):
        """
        regular_user belongs to 'users' group (cidx-meta only).
        Matches from repo-a.md, repo-b.md, repo-c.md must be hidden.
        Matches from README.md pass through.
        """
        all_matches = [
            make_regex_match("repo-a.md"),
            make_regex_match("repo-b.md"),
            make_regex_match("repo-c.md"),
            make_regex_match("README.md"),
        ]
        search_result = make_search_result(all_matches)

        with _patch_regex_search_infrastructure("cidx-meta", search_result, access_filtering_service):
            result = _run(
                handle_regex_search(
                    {"repository_alias": "cidx-meta", "pattern": "description"},
                    regular_user,
                )
            )

        data = extract_mcp_data(result)
        assert data["success"] is True
        returned_files = [m["file_path"] for m in data["matches"]]
        assert "repo-a.md" not in returned_files
        assert "repo-b.md" not in returned_files
        assert "repo-c.md" not in returned_files
        assert "README.md" in returned_files

    def test_power_user_sees_only_accessible_repo_matches(
        self, power_user, access_filtering_service
    ):
        """
        power_user belongs to 'powerusers' group (repo-a, repo-b, cidx-meta).
        Matches from repo-a.md and repo-b.md visible; repo-c.md hidden.
        """
        all_matches = [
            make_regex_match("repo-a.md"),
            make_regex_match("repo-b.md"),
            make_regex_match("repo-c.md"),
            make_regex_match("README.md"),
        ]
        search_result = make_search_result(all_matches)

        with _patch_regex_search_infrastructure("cidx-meta", search_result, access_filtering_service):
            result = _run(
                handle_regex_search(
                    {"repository_alias": "cidx-meta", "pattern": "description"},
                    power_user,
                )
            )

        data = extract_mcp_data(result)
        assert data["success"] is True
        returned_files = [m["file_path"] for m in data["matches"]]
        assert "repo-a.md" in returned_files
        assert "repo-b.md" in returned_files
        assert "README.md" in returned_files
        assert "repo-c.md" not in returned_files

    def test_regular_user_cidx_meta_global_alias_also_filtered(
        self, regular_user, access_filtering_service
    ):
        """
        The cidx-meta-global alias (contains 'cidx-meta') must also be filtered
        for non-admin users.
        """
        all_matches = [
            make_regex_match("repo-a.md"),
            make_regex_match("README.md"),
        ]
        search_result = make_search_result(all_matches)

        with _patch_regex_search_infrastructure("cidx-meta-global", search_result, access_filtering_service):
            result = _run(
                handle_regex_search(
                    {"repository_alias": "cidx-meta-global", "pattern": "description"},
                    regular_user,
                )
            )

        data = extract_mcp_data(result)
        assert data["success"] is True
        returned_files = [m["file_path"] for m in data["matches"]]
        assert "repo-a.md" not in returned_files
        assert "README.md" in returned_files


# ---------------------------------------------------------------------------
# AC2: Admin user sees all regex_search results without filtering
# ---------------------------------------------------------------------------


class TestRegexSearchCidxMetaAccessFilteringAdmin:
    """AC2: Admin users retain full regex_search access to all cidx-meta files."""

    def test_admin_user_sees_all_matches(
        self, admin_user, access_filtering_service
    ):
        """
        admin_user belongs to 'admins' group.
        All matches are returned without filtering.
        """
        all_matches = [
            make_regex_match("repo-a.md"),
            make_regex_match("repo-b.md"),
            make_regex_match("repo-c.md"),
            make_regex_match("README.md"),
        ]
        search_result = make_search_result(all_matches)

        with _patch_regex_search_infrastructure("cidx-meta", search_result, access_filtering_service):
            result = _run(
                handle_regex_search(
                    {"repository_alias": "cidx-meta", "pattern": "description"},
                    admin_user,
                )
            )

        data = extract_mcp_data(result)
        assert data["success"] is True
        returned_files = [m["file_path"] for m in data["matches"]]
        assert "repo-a.md" in returned_files
        assert "repo-b.md" in returned_files
        assert "repo-c.md" in returned_files
        assert "README.md" in returned_files
        assert len(data["matches"]) == 4

    def test_non_cidx_meta_repo_not_filtered(
        self, regular_user, access_filtering_service
    ):
        """
        Non-cidx-meta repos must not be affected by this filtering logic.
        All matches pass through for any repo that is not cidx-meta.
        """
        all_matches = [
            make_regex_match("src/auth.py"),
            make_regex_match("src/secret.py"),
        ]
        search_result = make_search_result(all_matches)

        with _patch_regex_search_infrastructure("some-repo", search_result, access_filtering_service):
            result = _run(
                handle_regex_search(
                    {"repository_alias": "some-repo", "pattern": "auth"},
                    regular_user,
                )
            )

        data = extract_mcp_data(result)
        assert data["success"] is True
        assert len(data["matches"]) == 2

    def test_no_access_filtering_service_returns_all_matches(self, regular_user):
        """
        If access_filtering_service is not configured, all matches are returned.
        """
        all_matches = [
            make_regex_match("repo-a.md"),
            make_regex_match("repo-b.md"),
        ]
        search_result = make_search_result(all_matches)

        with _patch_regex_search_infrastructure("cidx-meta", search_result, access_filtering_service=None):
            result = _run(
                handle_regex_search(
                    {"repository_alias": "cidx-meta", "pattern": "description"},
                    regular_user,
                )
            )

        data = extract_mcp_data(result)
        assert data["success"] is True
        assert len(data["matches"]) == 2  # unfiltered


# ---------------------------------------------------------------------------
# Bug #337: total_matches reflects filtered count, not pre-filtered engine count
# ---------------------------------------------------------------------------


class TestRegexSearchTotalMatchesReflectsFilteredCount:
    """Bug #337: total_matches in response must equal len(matches) after filtering."""

    def test_total_matches_equals_filtered_count_for_non_admin(
        self, regular_user, access_filtering_service
    ):
        """
        When filtering removes matches, total_matches must reflect filtered count.
        regular_user has no access to repo-a.md, repo-b.md, repo-c.md.
        Engine returns 4 matches but only README.md passes through.
        total_matches must be 1, not 4.
        """
        all_matches = [
            make_regex_match("repo-a.md"),
            make_regex_match("repo-b.md"),
            make_regex_match("repo-c.md"),
            make_regex_match("README.md"),
        ]
        # Engine reports 4 total but user should only see 1
        search_result = make_search_result(all_matches)
        assert search_result.total_matches == 4

        with _patch_regex_search_infrastructure("cidx-meta", search_result, access_filtering_service):
            result = _run(
                handle_regex_search(
                    {"repository_alias": "cidx-meta", "pattern": "description"},
                    regular_user,
                )
            )

        data = extract_mcp_data(result)
        assert data["success"] is True
        assert len(data["matches"]) == 1
        assert data["total_matches"] == 1, (
            f"total_matches must equal filtered count (1), not pre-filtered engine count (4). "
            f"Got: {data['total_matches']}"
        )

    def test_total_matches_equals_match_count_when_no_filtering_applied(
        self, admin_user, access_filtering_service
    ):
        """
        For admin users (no filtering), total_matches must still equal len(matches).
        """
        all_matches = [
            make_regex_match("repo-a.md"),
            make_regex_match("repo-b.md"),
        ]
        search_result = make_search_result(all_matches)

        with _patch_regex_search_infrastructure("cidx-meta", search_result, access_filtering_service):
            result = _run(
                handle_regex_search(
                    {"repository_alias": "cidx-meta", "pattern": "description"},
                    admin_user,
                )
            )

        data = extract_mcp_data(result)
        assert data["success"] is True
        assert data["total_matches"] == len(data["matches"]) == 2
