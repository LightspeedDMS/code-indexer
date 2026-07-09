"""
Bug #1119: Omni wildcard search with repository_alias="*" fails for users whose repos
are globally-activated (registered only as <name>-global).

Two failure modes:
1. repository_alias="*" (string) routes to _search_activated_repo instead of
   _omni_search_code. The wildcard expansion in _expand_wildcard_patterns is never
   reached because it is only called from the omni/list branch.

2. repository_alias=["fastapi", "pydantic"] (list of bare literals) routes correctly
   to _omni_search_code but _expand_wildcard_patterns passes the bare literals through
   unchanged. MultiSearchService._get_repository_path("fastapi") then calls
   backend_registry.global_repos.get_repo("fastapi") which returns None because the
   repo is stored as "fastapi-global".

Fix: In search_code and handle_regex_search, when repository_alias is a string
containing wildcard characters, wrap it as ["<pattern>"] so it routes to the omni
path where _expand_wildcard_patterns can expand it. In _expand_wildcard_patterns,
when a non-wildcard literal does not match any global repo directly, attempt bare-to-
global promotion (try appending "-global") if that form exists in global repos.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime
from typing import Any, Dict, List, cast
from unittest.mock import MagicMock, patch


from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_user(username: str = "admin") -> User:
    return User(
        username=username,
        role=UserRole.ADMIN,
        password_hash="dummy",
        created_at=datetime.now(),
    )


def _json_result(handler_response: Dict[str, Any]) -> Dict[str, Any]:
    """Decode the MCP content wrapper to the inner JSON dict."""
    text = handler_response["content"][0]["text"]
    return cast(Dict[str, Any], json.loads(text))


def _make_fake_global_repos(aliases: List[str]) -> List[Dict[str, Any]]:
    """Return fake global repo dicts for a list of alias names (already -global suffixed)."""
    return [{"alias_name": alias} for alias in aliases]


def _make_cap_config(cap: int = 50) -> MagicMock:
    mock_config_svc = MagicMock()
    mock_config_svc.get_config.return_value.multi_search_limits_config.omni_wildcard_expansion_cap = cap
    mock_config_svc.get_config.return_value.multi_search_limits_config.omni_max_repos_per_search = 50
    return mock_config_svc


# ---------------------------------------------------------------------------
# Test class 1: search_code routes "*" string to omni path
# ---------------------------------------------------------------------------


class TestSearchCodeWildcardStringRouting:
    """Bug #1119 root cause 1: repository_alias="*" (string) must route to
    the omni/wildcard expansion path, not to _search_activated_repo."""

    def test_star_string_does_not_call_search_activated_repo(self):
        """search_code with repository_alias="*" must NOT call _search_activated_repo.

        Before the fix, "*" is not a list and does not end with "-global",
        so it falls through to the else branch (_search_activated_repo) which
        fails with "Repository '*' not found for user 'admin'".
        """
        from code_indexer.server.mcp.handlers.search import search_code
        from code_indexer.server.mcp.handlers import _utils

        _make_fake_global_repos(
            ["fastapi-global", "pydantic-global", "starlette-global"]
        )

        params = {
            "query_text": "dependency injection",
            "repository_alias": "*",
        }

        # Track whether _search_activated_repo gets called
        search_activated_called = []

        def fake_search_activated(p, u):
            search_activated_called.append(True)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "success": False,
                                "error": "Repository '*' not found",
                                "results": [],
                            }
                        ),
                    }
                ]
            }

        def fake_omni_search(p, u):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"success": True, "results": []}),
                    }
                ]
            }

        with (
            patch(
                "code_indexer.server.mcp.handlers.search._search_activated_repo",
                side_effect=fake_search_activated,
            ),
            patch(
                "code_indexer.server.mcp.handlers.search._omni_search_code",
                side_effect=fake_omni_search,
            ),
            patch.object(
                _utils.app_module,
                "activated_repo_manager",
                MagicMock(user_has_activated_repo=MagicMock(return_value=False)),
            ),
            patch.object(
                _utils.app_module,
                "golden_repo_manager",
                MagicMock(is_globally_active=MagicMock(return_value=False)),
            ),
        ):
            result = search_code(params, _make_user())

        inner = _json_result(result)
        assert not search_activated_called, (
            "search_code with repository_alias='*' must NOT call _search_activated_repo. "
            f"Got result: {inner}"
        )
        assert inner.get("success") is True, (
            f"Expected success=True from omni path, got: {inner}"
        )

    def test_star_string_routes_to_omni_search_code(self):
        """search_code with repository_alias="*" must call _omni_search_code.

        The "*" must be wrapped as ["*"] before routing so that
        _expand_wildcard_patterns can expand it to the real -global aliases.
        """
        from code_indexer.server.mcp.handlers.search import search_code
        from code_indexer.server.mcp.handlers import _utils

        omni_called_with = []

        def fake_omni_search(p, u):
            omni_called_with.append(p.get("repository_alias"))
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"success": True, "results": []}),
                    }
                ]
            }

        params = {
            "query_text": "something",
            "repository_alias": "*",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers.search._omni_search_code",
                side_effect=fake_omni_search,
            ),
            patch.object(
                _utils.app_module,
                "activated_repo_manager",
                MagicMock(user_has_activated_repo=MagicMock(return_value=False)),
            ),
            patch.object(
                _utils.app_module,
                "golden_repo_manager",
                MagicMock(is_globally_active=MagicMock(return_value=False)),
            ),
        ):
            search_code(params, _make_user())

        assert omni_called_with, (
            "search_code with repository_alias='*' must call _omni_search_code. "
            "The '*' should be wrapped as ['*'] and routed to the omni path."
        )

    def test_question_mark_wildcard_string_routes_to_omni(self):
        """search_code with repository_alias="fastapi-?" must route to omni path.

        Any string containing wildcard chars (*, ?, [) should be wrapped as a list
        and routed to _omni_search_code.
        """
        from code_indexer.server.mcp.handlers.search import search_code
        from code_indexer.server.mcp.handlers import _utils

        omni_called = []

        def fake_omni_search(p, u):
            omni_called.append(True)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"success": True, "results": []}),
                    }
                ]
            }

        params = {
            "query_text": "something",
            "repository_alias": "fastapi-?",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers.search._omni_search_code",
                side_effect=fake_omni_search,
            ),
            patch.object(
                _utils.app_module,
                "activated_repo_manager",
                MagicMock(user_has_activated_repo=MagicMock(return_value=False)),
            ),
            patch.object(
                _utils.app_module,
                "golden_repo_manager",
                MagicMock(is_globally_active=MagicMock(return_value=False)),
            ),
        ):
            search_code(params, _make_user())

        assert omni_called, (
            "search_code with repository_alias='fastapi-?' must call _omni_search_code."
        )

    def test_non_wildcard_bare_string_still_uses_story1039_fallback(self):
        """search_code with a plain bare alias (no wildcards) still uses Story #1039 fallback.

        A plain bare alias like "fastapi" (no wildcard chars) must still try the
        bare-to-global promotion (Story #1039) before routing. This test verifies we
        don't accidentally break that code path by treating all non-list strings as
        wildcards.
        """
        from code_indexer.server.mcp.handlers.search import search_code
        from code_indexer.server.mcp.handlers import _utils

        global_search_called_with = []

        def fake_search_global(p, u, alias):
            global_search_called_with.append(alias)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"success": True, "results": []}),
                    }
                ]
            }

        params = {
            "query_text": "something",
            "repository_alias": "fastapi",  # bare, no wildcard
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers.search._search_global_repo",
                side_effect=fake_search_global,
            ),
            patch.object(
                _utils.app_module,
                "activated_repo_manager",
                MagicMock(user_has_activated_repo=MagicMock(return_value=False)),
            ),
            patch.object(
                _utils.app_module,
                "golden_repo_manager",
                MagicMock(is_globally_active=MagicMock(return_value=True)),
            ),
        ):
            search_code(params, _make_user())

        assert global_search_called_with == ["fastapi-global"], (
            f"Story #1039 fallback must promote bare 'fastapi' to 'fastapi-global'. "
            f"Got: {global_search_called_with}"
        )


# ---------------------------------------------------------------------------
# Test class 2: _expand_wildcard_patterns promotes bare literals
# ---------------------------------------------------------------------------


class TestExpandWildcardPatternsBareLiteralPromotion:
    """Bug #1119 root cause 2: when a literal bare alias like "fastapi" is passed
    to _expand_wildcard_patterns and the global repos only have "fastapi-global",
    the function must promote "fastapi" -> "fastapi-global" instead of passing
    the bare name through unchanged."""

    def _run_expand(
        self,
        patterns: List[str],
        fake_repos: List[dict],
        cap: int = 50,
    ) -> Any:
        """Run _expand_wildcard_patterns with all external dependencies patched."""
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
                    return_value=_make_cap_config(cap),
                ),
            ):
                return _expand_wildcard_patterns(patterns, _make_user())

    def test_bare_literal_promotes_to_global_when_global_form_exists(self):
        """_expand_wildcard_patterns must promote "fastapi" -> "fastapi-global"
        when global repos contain "fastapi-global" but NOT "fastapi".

        Before the fix, "fastapi" is passed through unchanged as a literal,
        then MultiSearchService._get_repository_path("fastapi") fails.
        """
        fake_repos = _make_fake_global_repos(
            ["fastapi-global", "pydantic-global", "starlette-global"]
        )

        result = self._run_expand(["fastapi"], fake_repos)

        assert isinstance(result, list), f"Expected list, got {type(result)}"
        assert "fastapi-global" in result, (
            f"Expected 'fastapi-global' in result (bare literal must be promoted). "
            f"Got: {result}"
        )
        assert "fastapi" not in result, (
            f"Bare 'fastapi' must NOT appear in result when 'fastapi-global' exists. "
            f"Got: {result}"
        )

    def test_multiple_bare_literals_all_promoted(self):
        """All bare literals that have a -global form must be promoted."""
        fake_repos = _make_fake_global_repos(
            ["fastapi-global", "pydantic-global", "starlette-global", "uvicorn-global"]
        )

        result = self._run_expand(["fastapi", "pydantic", "starlette"], fake_repos)

        assert isinstance(result, list), f"Expected list, got {type(result)}"
        assert set(result) == {
            "fastapi-global",
            "pydantic-global",
            "starlette-global",
        }, f"All bare literals must be promoted to -global form. Got: {result}"

    def test_bare_literal_without_global_form_is_kept_as_is(self):
        """If a bare literal has no -global counterpart, it must be passed through unchanged.

        The downstream MultiSearchService will handle the not-found case.
        We must not silently drop repos from the fan-out.
        """
        fake_repos = _make_fake_global_repos(["pydantic-global"])

        result = self._run_expand(["no-such-repo"], fake_repos)

        assert isinstance(result, list)
        assert "no-such-repo" in result, (
            f"Bare literal with no -global form must be kept unchanged. Got: {result}"
        )

    def test_wildcard_star_expands_to_all_global_repos(self):
        """'*' wildcard must expand to ALL global repos (existing behaviour, not regressed)."""
        fake_repos = _make_fake_global_repos(
            ["fastapi-global", "pydantic-global", "starlette-global"]
        )

        result = self._run_expand(["*"], fake_repos)

        assert isinstance(result, list), f"Expected list, got {type(result)}"
        assert set(result) == {
            "fastapi-global",
            "pydantic-global",
            "starlette-global",
        }, f"'*' must expand to all global repos. Got: {result}"

    def test_already_global_suffixed_literal_is_not_double_suffixed(self):
        """A literal "fastapi-global" passed explicitly must not become "fastapi-global-global"."""
        fake_repos = _make_fake_global_repos(["fastapi-global"])

        result = self._run_expand(["fastapi-global"], fake_repos)

        assert isinstance(result, list)
        assert result == ["fastapi-global"], (
            f"Already-global alias must not be double-suffixed. Got: {result}"
        )
        assert "fastapi-global-global" not in result

    def test_mixed_wildcards_and_bare_literals_both_resolved(self):
        """Mix of wildcards and bare literals must all resolve correctly."""
        fake_repos = _make_fake_global_repos(
            ["fastapi-global", "pydantic-global", "starlette-global"]
        )

        result = self._run_expand(["fastapi", "starlette-*"], fake_repos)

        assert isinstance(result, list)
        assert "fastapi-global" in result, (
            f"Bare literal 'fastapi' must be promoted to 'fastapi-global'. Got: {result}"
        )
        assert "starlette-global" in result, (
            f"Wildcard 'starlette-*' must match 'starlette-global'. Got: {result}"
        )


# ---------------------------------------------------------------------------
# Test class 3: end-to-end — search_code with "*" expands to global repos
# ---------------------------------------------------------------------------


class TestSearchCodeStarExpandsToGlobalRepos:
    """Integration test: search_code with repository_alias="*" for a user whose
    repos are all globally-activated must successfully fan out to those repos."""

    def test_star_alias_results_in_omni_search_over_global_repos(self):
        """search_code with repository_alias="*" must fan out to all global repos.

        This is the primary staging failure scenario: admin user whose repos are
        registered as fastapi-global, pydantic-global, etc. The "*" wildcard
        must expand to those aliases and pass them to MultiSearchService, not
        fail with 'Repository * not found'.
        """
        from code_indexer.server.mcp.handlers.search import search_code
        from code_indexer.server.mcp.handlers import _utils

        fake_repos = _make_fake_global_repos(
            ["fastapi-global", "pydantic-global", "starlette-global"]
        )

        omni_received_aliases = []

        def fake_omni_search(p, u):
            alias = p.get("repository_alias")
            if isinstance(alias, list):
                omni_received_aliases.extend(alias)
            else:
                omni_received_aliases.append(alias)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"success": True, "results": []}),
                    }
                ]
            }

        params = {
            "query_text": "dependency injection",
            "repository_alias": "*",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers.search._omni_search_code",
                side_effect=fake_omni_search,
            ),
            patch(
                "code_indexer.server.mcp.handlers._utils._list_global_repos",
                return_value=fake_repos,
            ),
            patch(
                "code_indexer.server.mcp.handlers._utils._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
            patch.object(
                _utils.app_module,
                "activated_repo_manager",
                MagicMock(user_has_activated_repo=MagicMock(return_value=False)),
            ),
            patch.object(
                _utils.app_module,
                "golden_repo_manager",
                MagicMock(is_globally_active=MagicMock(return_value=False)),
            ),
        ):
            result = search_code(params, _make_user())

        inner = _json_result(result)
        assert inner.get("success") is True, (
            f"search_code with repository_alias='*' must succeed. Got: {inner}. "
            f"The error 'Repository * not found' indicates the wildcard was routed "
            f"to _search_activated_repo instead of _omni_search_code."
        )
        assert not inner.get("error"), (
            f"Must not return an error. Got: {inner.get('error')}"
        )


# ---------------------------------------------------------------------------
# Test class 4: handle_regex_search routes "*" string to omni path (Bug #1119)
# ---------------------------------------------------------------------------


class TestRegexSearchWildcardStringRouting:
    """Bug #1119 gap: handle_regex_search with repository_alias="*" (string) must
    route to _omni_regex_search, not fall through to the single-repo path which
    fails with 'Repository * not found for user'."""

    def test_star_string_does_not_reach_single_repo_path(self):
        """handle_regex_search with repository_alias="*" must NOT reach the
        single-repo resolve path (_get_legacy()._resolve_repo_path).

        Before the fix, "*" is not a list, so the isinstance(list) guard at
        line ~1299 is False and execution falls through to single-repo logic
        which resolves '*' and returns 'Repository * not found'.
        """
        import asyncio
        from code_indexer.server.mcp.handlers.search import handle_regex_search
        from code_indexer.server.mcp.handlers import _utils

        resolve_called = []

        def fake_resolve(alias, golden_dir):
            resolve_called.append(alias)
            return None  # not found

        async def fake_omni_regex(a, u):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"success": True, "matches": []}),
                    }
                ]
            }

        args = {
            "repository_alias": "*",
            "pattern": "TODO",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers.search._omni_regex_search",
                side_effect=fake_omni_regex,
            ),
            patch(
                "code_indexer.server.mcp.handlers.search._get_legacy",
                return_value=MagicMock(
                    _resolve_repo_path=MagicMock(side_effect=fake_resolve)
                ),
            ),
            patch.object(
                _utils.app_module,
                "activated_repo_manager",
                MagicMock(user_has_activated_repo=MagicMock(return_value=False)),
            ),
            patch.object(
                _utils.app_module,
                "golden_repo_manager",
                MagicMock(is_globally_active=MagicMock(return_value=False)),
            ),
        ):
            result = asyncio.get_event_loop().run_until_complete(
                handle_regex_search(args, _make_user())
            )

        assert not resolve_called, (
            "handle_regex_search with repository_alias='*' must NOT call "
            "_resolve_repo_path (single-repo path). "
            f"resolve_called={resolve_called}, result={result}"
        )
        inner = _json_result(result)
        assert inner.get("success") is True, (
            f"Expected success=True from omni path. Got: {inner}"
        )

    def test_star_string_routes_to_omni_regex_search(self):
        """handle_regex_search with repository_alias="*" must call _omni_regex_search.

        The "*" must be wrapped as ["*"] before the isinstance(list) guard so
        _expand_wildcard_patterns can expand it to the real -global aliases.
        """
        import asyncio
        from code_indexer.server.mcp.handlers.search import handle_regex_search
        from code_indexer.server.mcp.handlers import _utils

        omni_called_with = []

        async def fake_omni_regex(a, u):
            omni_called_with.append(a.get("repository_alias"))
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"success": True, "matches": []}),
                    }
                ]
            }

        args = {
            "repository_alias": "*",
            "pattern": "TODO",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers.search._omni_regex_search",
                side_effect=fake_omni_regex,
            ),
            patch.object(
                _utils.app_module,
                "activated_repo_manager",
                MagicMock(user_has_activated_repo=MagicMock(return_value=False)),
            ),
            patch.object(
                _utils.app_module,
                "golden_repo_manager",
                MagicMock(is_globally_active=MagicMock(return_value=False)),
            ),
        ):
            asyncio.get_event_loop().run_until_complete(
                handle_regex_search(args, _make_user())
            )

        assert omni_called_with, (
            "handle_regex_search with repository_alias='*' must call _omni_regex_search. "
            "The '*' should be wrapped as ['*'] and routed to the omni path."
        )

    def test_non_wildcard_bare_string_still_uses_story1039_fallback(self):
        """handle_regex_search with a plain bare alias (no wildcards) still uses
        Story #1039 bare-to-global fallback unaffected by the wildcard guard."""
        import asyncio
        from code_indexer.server.mcp.handlers.search import handle_regex_search
        from code_indexer.server.mcp.handlers import _utils

        resolved_aliases = []

        def fake_resolve(alias, golden_dir):
            resolved_aliases.append(alias)
            return None  # simulate not found; we only care about what alias was tried

        args = {
            "repository_alias": "fastapi",  # bare, no wildcard
            "pattern": "TODO",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers.search._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers.search._get_legacy",
                return_value=MagicMock(
                    _resolve_repo_path=MagicMock(side_effect=fake_resolve)
                ),
            ),
            patch(
                "code_indexer.server.mcp.handlers.search.api_metrics_service.increment_regex_search"
            ),
            patch.object(
                _utils.app_module,
                "activated_repo_manager",
                MagicMock(user_has_activated_repo=MagicMock(return_value=False)),
            ),
            patch.object(
                _utils.app_module,
                "golden_repo_manager",
                MagicMock(is_globally_active=MagicMock(return_value=True)),
            ),
        ):
            asyncio.get_event_loop().run_until_complete(
                handle_regex_search(args, _make_user())
            )

        # The Story #1039 fallback promotes "fastapi" -> "fastapi-global"; then
        # single-repo path is taken and _resolve_repo_path is called with "fastapi-global".
        assert resolved_aliases and resolved_aliases[0] == "fastapi-global", (
            f"Story #1039 fallback must promote 'fastapi' to 'fastapi-global' before "
            f"single-repo path. Got resolved_aliases={resolved_aliases}"
        )
