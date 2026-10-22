"""
Unit tests for Bug #337: regex_search on cidx-meta filters unauthorized repo
description content for non-admin users.

AC1: Non-admin users calling regex_search on cidx-meta only see matches from
     authorized repo files.
AC2: Admin users retain full regex_search access to all cidx-meta files.
AC3: Unit tests verify regex_search cidx-meta filtering.

The served catalog is a real directory holding every matched file and the
alias universe is a real SQLite global-repos registry; only ripgrep and the
server configuration are replaced.
"""

import asyncio
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


from code_indexer.server.mcp.handlers import handle_regex_search

from .conftest import extract_mcp_data

REGISTERED = ("repo-a", "repo-b", "repo-c", "cidx-meta")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_regex_match(
    file_path: str, line_content: str = "description content"
) -> MagicMock:
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
    result.read_capped = False
    result.search_engine = "ripgrep"
    result.search_time_ms = 5
    return result


def _run(coro):
    """Run a coroutine synchronously on a private event loop, then release it.

    handle_regex_search offloads reranking via
    ``loop.run_in_executor(None, ...)``, which starts the loop's default
    ThreadPoolExecutor -- a NON-daemon ``asyncio_N`` thread. Shutting that
    executor down and closing the loop before returning keeps the thread
    from outliving the test session (Bug #1800 guard), exactly as
    production's ``handle_regex_search_sync`` does via ``asyncio.run``.
    ``asyncio.run`` itself is not used here because it resets the thread's
    current event loop to None, which would change behaviour for any later
    test that calls ``asyncio.get_event_loop()``.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            loop.close()


def _registry(db_path: Path) -> Any:
    """A real SQLite global-repos registry naming every test repository."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import GlobalReposSqliteBackend

    DatabaseSchema(str(db_path)).initialize_database()
    registry = GlobalReposSqliteBackend(str(db_path))
    for alias in REGISTERED:
        registry.register_repo(
            alias_name=f"{alias}-global",
            repo_name=alias,
            repo_url=None,
            index_path=f"/golden/{alias}",
        )
    return registry


def _served_directory(root: Path, search_result) -> Path:
    """The served repository: every matched file, its match on line 1."""
    for match in search_result.matches:
        target = root / match.file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{match.line_content}\n", encoding="utf-8")
    return root


@contextmanager
def _patch_regex_search_infrastructure(
    tmp_path: Path, repo_alias: str, search_result, access_filtering_service=None
):
    """
    Context manager that patches the infrastructure handle_regex_search needs,
    isolating from the real server state.

    Patches:
    - _get_golden_repos_dir: returns a path that does not exist
    - _resolve_repo_path: returns a real directory holding the matched files
    - _get_access_filtering_service: returns provided service or None
    - server_app_state: a state whose registry is a real SQLite registry
    - RegexSearchService: returns an AsyncMock with the given result
    - _get_wiki_enabled_repos: returns empty set
    - _apply_regex_payload_truncation: identity function
    - get_config_service: returns a mock config with timeout/workers
    """
    mock_service = AsyncMock()
    mock_service.search.return_value = search_result
    repo_path = _served_directory(tmp_path / repo_alias, search_result)
    state = SimpleNamespace(
        backend_registry=SimpleNamespace(
            global_repos=_registry(tmp_path / "registry.db")
        )
    )
    handlers = "code_indexer.server.mcp.handlers"
    with ExitStack() as stack:
        for target, kwargs in (
            (f"{handlers}._get_golden_repos_dir", {"return_value": "/fake/golden"}),
            (f"{handlers}._resolve_repo_path", {"return_value": str(repo_path)}),
            (
                f"{handlers}._get_access_filtering_service",
                {"return_value": access_filtering_service},
            ),
            (
                f"{handlers}.search.regex_search.server_app_state",
                {"return_value": state},
            ),
            (
                "code_indexer.global_repos.regex_search.RegexSearchService",
                {"return_value": mock_service},
            ),
            (f"{handlers}._get_wiki_enabled_repos", {"return_value": set()}),
            (
                f"{handlers}._apply_regex_payload_truncation",
                {"side_effect": lambda x, **kw: x},
            ),
        ):
            stack.enter_context(patch(target, **kwargs))
        mock_cfg = stack.enter_context(
            patch("code_indexer.server.services.config_service.get_config_service")
        )
        config = mock_cfg.return_value.get_config.return_value
        config.search_limits_config.timeout_seconds = 30
        config.background_jobs_config.subprocess_max_workers = 2
        yield


def _search(tmp_path: Path, alias: str, matches, service, user, pattern: str):
    search_result = make_search_result(matches)
    with _patch_regex_search_infrastructure(tmp_path, alias, search_result, service):
        result = _run(
            handle_regex_search({"repository_alias": alias, "pattern": pattern}, user)
        )
    return extract_mcp_data(result)


ALL_FOUR = ("repo-a.md", "repo-b.md", "repo-c.md", "README.md")


# ---------------------------------------------------------------------------
# AC1 + AC3: Non-admin user sees only authorized repo files in regex_search
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestRegexSearchCidxMetaAccessFilteringNonAdmin:
    """AC1 + AC3: regex_search on cidx-meta filters unauthorized repo .md files."""

    def test_regular_user_sees_no_catalog_matches(
        self, tmp_path, regular_user, access_filtering_service
    ):
        """
        regular_user belongs to 'users' group (cidx-meta only).
        Matches from repo-a.md, repo-b.md, repo-c.md and README.md are hidden.
        """
        matches = [make_regex_match(p) for p in ALL_FOUR]
        data = _search(
            tmp_path,
            "cidx-meta",
            matches,
            access_filtering_service,
            regular_user,
            "description",
        )
        assert data["success"] is True
        returned_files = [m["file_path"] for m in data["matches"]]
        assert "repo-a.md" not in returned_files
        assert "repo-b.md" not in returned_files
        assert "repo-c.md" not in returned_files
        assert "README.md" not in returned_files

    def test_power_user_sees_only_accessible_repo_matches(
        self, tmp_path, power_user, access_filtering_service
    ):
        """
        power_user belongs to 'powerusers' group (repo-a, repo-b, cidx-meta).
        Matches from repo-a.md and repo-b.md visible; repo-c.md hidden.
        """
        matches = [make_regex_match(p) for p in ALL_FOUR]
        data = _search(
            tmp_path,
            "cidx-meta",
            matches,
            access_filtering_service,
            power_user,
            "description",
        )
        assert data["success"] is True
        returned_files = [m["file_path"] for m in data["matches"]]
        assert "repo-a.md" in returned_files
        assert "repo-b.md" in returned_files
        assert "README.md" not in returned_files
        assert "repo-c.md" not in returned_files

    def test_regular_user_cidx_meta_global_alias_also_filtered(
        self, tmp_path, regular_user, access_filtering_service
    ):
        """
        The cidx-meta-global alias (the catalog's registry entry) must also be
        filtered for non-admin users.
        """
        matches = [make_regex_match("repo-a.md"), make_regex_match("README.md")]
        data = _search(
            tmp_path,
            "cidx-meta-global",
            matches,
            access_filtering_service,
            regular_user,
            "description",
        )
        assert data["success"] is True
        returned_files = [m["file_path"] for m in data["matches"]]
        assert "repo-a.md" not in returned_files
        assert "README.md" not in returned_files


# ---------------------------------------------------------------------------
# AC2: Admin user sees all regex_search results without filtering
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestRegexSearchCidxMetaAccessFilteringAdmin:
    """AC2: Admin users retain full regex_search access to all cidx-meta files."""

    def test_admin_user_sees_all_matches(
        self, tmp_path, admin_user, access_filtering_service
    ):
        """
        admin_user belongs to 'admins' group.
        All matches are returned without filtering.
        """
        matches = [make_regex_match(p) for p in ALL_FOUR]
        data = _search(
            tmp_path,
            "cidx-meta",
            matches,
            access_filtering_service,
            admin_user,
            "description",
        )
        assert data["success"] is True
        returned_files = [m["file_path"] for m in data["matches"]]
        assert "repo-a.md" in returned_files
        assert "repo-b.md" in returned_files
        assert "repo-c.md" in returned_files
        assert "README.md" in returned_files
        assert len(data["matches"]) == 4

    def test_non_cidx_meta_repo_not_filtered(
        self, tmp_path, regular_user, access_filtering_service
    ):
        """
        Non-cidx-meta repos must not be affected by this filtering logic.
        All matches pass through for any repo that is not cidx-meta.
        """
        matches = [make_regex_match("src/auth.py"), make_regex_match("src/secret.py")]
        data = _search(
            tmp_path,
            "some-repo",
            matches,
            access_filtering_service,
            regular_user,
            "auth",
        )
        assert data["success"] is True
        assert len(data["matches"]) == 2

    def test_no_access_filtering_service_returns_no_matches(
        self, tmp_path, regular_user
    ):
        """
        If access_filtering_service is not configured, no match is returned.
        """
        matches = [make_regex_match("repo-a.md"), make_regex_match("repo-b.md")]
        data = _search(tmp_path, "cidx-meta", matches, None, regular_user, "desc")
        assert data["success"] is True
        assert data["matches"] == []


# ---------------------------------------------------------------------------
# Bug #337: total_matches reflects filtered count, not pre-filtered engine count
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestRegexSearchTotalMatchesReflectsFilteredCount:
    """Bug #337: total_matches in response must equal len(matches) after filtering."""

    def test_total_matches_equals_filtered_count_for_non_admin(
        self, tmp_path, power_user, access_filtering_service
    ):
        """
        When filtering removes matches, total_matches must reflect filtered count.
        power_user is granted repo-a and repo-b only. The engine returns 4
        matches; repo-c.md and README.md are hidden, so total_matches must be
        2, not 4.
        """
        matches = [make_regex_match(p) for p in ALL_FOUR]
        assert make_search_result(matches).total_matches == 4
        data = _search(
            tmp_path,
            "cidx-meta",
            matches,
            access_filtering_service,
            power_user,
            "description",
        )
        assert data["success"] is True
        assert len(data["matches"]) == 2
        assert data["total_matches"] == 2, (
            f"total_matches must equal filtered count (2), not pre-filtered engine count (4). "
            f"Got: {data['total_matches']}"
        )

    def test_total_matches_equals_match_count_when_no_filtering_applied(
        self, tmp_path, admin_user, access_filtering_service
    ):
        """
        For admin users (no filtering), total_matches must still equal len(matches).
        """
        matches = [make_regex_match("repo-a.md"), make_regex_match("repo-b.md")]
        data = _search(
            tmp_path,
            "cidx-meta",
            matches,
            access_filtering_service,
            admin_user,
            "description",
        )
        assert data["success"] is True
        assert data["total_matches"] == len(data["matches"]) == 2
