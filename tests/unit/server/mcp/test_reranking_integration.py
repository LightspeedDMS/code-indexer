"""Unit tests for Story #653: rerank_query/rerank_instruction params on 4 tools.

Tests verify:
1. Tool schemas for search_code, regex_search, git_search_commits, git_search_diffs
   include rerank_query and rerank_instruction parameters.
2. When rerank_query=None, _apply_reranking_sync is not called (no overhead).
3. When rerank_query is provided, _apply_reranking_sync is called with the correct
   content extractor for each handler.

Epic #649, Story #653.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.mcp.tools import TOOL_REGISTRY


@contextmanager
def _patched_global_repo_env(rerank_meta: dict, fake_result):
    """Context manager encapsulating all patches for global-repo search_code tests.

    Args:
        rerank_meta: Dict returned by the patched _apply_reranking_sync.
        fake_result: Mock result object returned by _perform_search.

    Yields:
        mock_utils: The patched _utils module so callers can inspect call args.
    """
    with (
        patch(
            "code_indexer.server.mcp.handlers._legacy._get_golden_repos_dir",
            return_value="/fake/golden",
        ),
        patch(
            "code_indexer.server.mcp.handlers._legacy._list_global_repos",
            return_value=[
                {
                    "alias_name": "test-repo-global",
                    "repo_name": "test-repo",
                    "repo_url": "https://example.com/test.git",
                }
            ],
        ),
        patch(
            "code_indexer.global_repos.alias_manager.AliasManager"
        ) as mock_alias_mgr_cls,
        patch("code_indexer.server.mcp.handlers._legacy._utils") as mock_utils,
        patch(
            "code_indexer.server.mcp.handlers._legacy.get_config_service",
            return_value=_make_config_service_with_rerank(),
        ),
        patch(
            "code_indexer.server.mcp.handlers._legacy._get_access_filtering_service",
            return_value=None,
        ),
        patch(
            "code_indexer.server.mcp.handlers._legacy._get_query_tracker",
            return_value=None,
        ),
        patch(
            "code_indexer.server.mcp.handlers._legacy._get_wiki_enabled_repos",
            return_value=set(),
        ),
        patch("pathlib.Path.exists", return_value=True),
        patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            return_value=([fake_result.to_dict()], rerank_meta),
        ),
    ):
        mock_alias_mgr_cls.return_value.read_alias.return_value = (
            "/fake/golden/test-repo-global"
        )
        mock_utils.app_module.semantic_query_manager._perform_search.return_value = [
            fake_result
        ]
        mock_utils.app_module.golden_repo_manager = None
        yield mock_utils


# ---------------------------------------------------------------------------
# PART 1: Tool schema tests — rerank_query and rerank_instruction present
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    ["search_code", "regex_search", "git_search_commits", "git_search_diffs"],
)
@pytest.mark.parametrize("param_name", ["rerank_query", "rerank_instruction"])
class TestRerankParamsInToolSchemas:
    """All 4 tools must expose rerank_query and rerank_instruction in their schemas."""

    def test_param_exists_in_properties(self, tool_name, param_name):
        """Parameter must be present in inputSchema.properties."""
        tool = TOOL_REGISTRY[tool_name]
        props = tool["inputSchema"]["properties"]
        assert param_name in props, (
            f"Tool '{tool_name}' is missing '{param_name}' in inputSchema.properties"
        )

    def test_param_is_string_type(self, tool_name, param_name):
        """Parameter must be declared as type string."""
        tool = TOOL_REGISTRY[tool_name]
        props = tool["inputSchema"]["properties"]
        assert props[param_name]["type"] == "string", (
            f"Tool '{tool_name}' property '{param_name}' must be type string"
        )

    def test_param_is_optional(self, tool_name, param_name):
        """Parameter must be optional (not in required list)."""
        tool = TOOL_REGISTRY[tool_name]
        required = tool["inputSchema"].get("required", [])
        assert param_name not in required, (
            f"Tool '{tool_name}' property '{param_name}' must not be required"
        )


# ---------------------------------------------------------------------------
# PART 2: Reranking guard — no overhead when rerank_query is absent
# ---------------------------------------------------------------------------


class TestRerankingGuardNoOverhead:
    """_apply_reranking_sync must not be called when rerank_query is absent."""

    def test_none_rerank_query_skips_reranking(self):
        """When rerank_query=None, _apply_reranking_sync must not be invoked."""
        from code_indexer.server.mcp.reranking import _apply_reranking_sync

        results = [{"content": "doc A"}, {"content": "doc B"}]

        def extractor(r):
            return r.get("content", "")

        config_service = MagicMock()
        config_service.get_config.return_value = MagicMock(rerank_config=None)

        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as mock_voyage,
            patch(
                "code_indexer.server.mcp.reranking.CohereRerankerClient"
            ) as mock_cohere,
        ):
            returned, _ = _apply_reranking_sync(
                results=results,
                rerank_query=None,
                rerank_instruction=None,
                content_extractor=extractor,
                requested_limit=10,
                config_service=config_service,
            )

        assert returned is results
        mock_voyage.assert_not_called()
        mock_cohere.assert_not_called()

    def test_empty_rerank_query_skips_reranking(self):
        """When rerank_query='', _apply_reranking_sync must not invoke providers."""
        from code_indexer.server.mcp.reranking import _apply_reranking_sync

        results = [{"content": "doc A"}]

        def extractor(r):
            return r.get("content", "")

        config_service = MagicMock()
        config_service.get_config.return_value = MagicMock(rerank_config=None)

        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as mock_voyage,
            patch(
                "code_indexer.server.mcp.reranking.CohereRerankerClient"
            ) as mock_cohere,
        ):
            returned, _ = _apply_reranking_sync(
                results=results,
                rerank_query="",
                rerank_instruction=None,
                content_extractor=extractor,
                requested_limit=10,
                config_service=config_service,
            )

        assert returned is results
        mock_voyage.assert_not_called()
        mock_cohere.assert_not_called()


# ---------------------------------------------------------------------------
# PART 3: Handler wiring — _apply_reranking_sync called with correct extractor
# ---------------------------------------------------------------------------


def _make_config_service_with_rerank():
    """Build a mock config_service with rerank config populated."""
    from code_indexer.server.utils.config_manager import RerankConfig

    rerank_cfg = RerankConfig(
        voyage_reranker_model="rerank-2.5",
        cohere_reranker_model="rerank-v3.5",
        overfetch_multiplier=5,
    )
    config = MagicMock()
    config.rerank_config = rerank_cfg
    svc = MagicMock()
    svc.get_config.return_value = config
    return svc


class TestSearchCodeHandlerRerankingWiring:
    """search_code handler passes code content extractor to _apply_reranking_sync."""

    def test_rerank_called_on_global_repo_path(self):
        """search_code (global repo path) calls _apply_reranking_sync with correct extractor.

        When rerank_query is provided and results contain 'content' or 'code_snippet',
        the content extractor must extract those fields.
        """
        from code_indexer.server.mcp.handlers._legacy import search_code
        from code_indexer.server.auth.user_manager import User

        user = MagicMock(spec=User)
        user.username = "testuser"

        # Build a search result that has 'content' field (semantic result format)
        fake_result = MagicMock()
        fake_result.to_dict.return_value = {
            "file_path": "src/foo.py",
            "content": "def foo(): return 42",
            "similarity_score": 0.9,
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._legacy._get_golden_repos_dir",
                return_value="/fake/golden",
            ),
            patch(
                "code_indexer.server.mcp.handlers._legacy._list_global_repos",
                return_value=[
                    {
                        "alias_name": "test-repo-global",
                        "repo_name": "test-repo",
                        "repo_url": "https://example.com/test.git",
                    }
                ],
            ),
            patch(
                "code_indexer.global_repos.alias_manager.AliasManager"
            ) as mock_alias_mgr_cls,
            patch("code_indexer.server.mcp.handlers._legacy._utils") as mock_utils,
            patch(
                "code_indexer.server.mcp.handlers._legacy.get_config_service",
                return_value=_make_config_service_with_rerank(),
            ),
            patch(
                "code_indexer.server.mcp.handlers._legacy._get_access_filtering_service",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._legacy._get_query_tracker",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._legacy._get_wiki_enabled_repos",
                return_value=set(),
            ),
            patch(
                "pathlib.Path.exists",
                return_value=True,
            ),
            patch(
                "code_indexer.server.mcp.reranking._apply_reranking_sync",
                return_value=(
                    [fake_result.to_dict()],
                    {
                        "reranker_used": False,
                        "reranker_provider": None,
                        "rerank_time_ms": 0,
                        "rerank_hint": None,
                    },
                ),
            ) as mock_rerank,
        ):
            # Set up alias manager to return a valid path
            mock_alias_mgr = MagicMock()
            mock_alias_mgr.read_alias.return_value = "/fake/golden/test-repo-global"
            mock_alias_mgr_cls.return_value = mock_alias_mgr

            # Set up semantic_query_manager to return a fake result
            mock_utils.app_module.semantic_query_manager._perform_search.return_value = [
                fake_result
            ]
            mock_utils.app_module.golden_repo_manager = None

            params = {
                "repository_alias": "test-repo-global",
                "query_text": "foo function",
                "rerank_query": "find the foo function implementation",
                "rerank_instruction": "Focus on implementation files",
                "limit": 5,
            }

            search_code(params, user)

        # _apply_reranking_sync must have been called
        mock_rerank.assert_called_once()
        call_args = mock_rerank.call_args

        # Verify the content extractor works correctly on the result format
        extractor = (
            call_args[1]["content_extractor"] if call_args[1] else call_args[0][3]
        )
        sample = {"content": "def foo(): pass", "code_snippet": "ignored"}
        assert extractor(sample) == "def foo(): pass"
        sample_fallback = {"code_snippet": "def bar(): pass"}
        assert extractor(sample_fallback) == "def bar(): pass"


class TestGitSearchDiffsHandlerRerankingWiring:
    """handle_git_search_diffs passes diff extractor to _apply_reranking_sync."""

    def test_rerank_called_with_diff_snippet_extractor(self):
        """handle_git_search_diffs calls _apply_reranking_sync with diff_snippet extractor."""
        from code_indexer.server.mcp.handlers._legacy import handle_git_search_diffs
        from code_indexer.server.auth.user_manager import User

        user = MagicMock(spec=User)
        user.username = "testuser"

        fake_match = MagicMock()
        fake_match.hash = "abc123def456"
        fake_match.short_hash = "abc123d"
        fake_match.author_name = "Dev"
        fake_match.author_date = "2024-01-01"
        fake_match.subject = "add feature"
        fake_match.files_changed = ["src/foo.py"]
        fake_match.diff_snippet = "+def foo(): return 42"

        fake_result = MagicMock()
        fake_result.search_term = "foo"
        fake_result.is_regex = False
        fake_result.matches = [fake_match]
        fake_result.total_matches = 1
        fake_result.truncated = False
        fake_result.search_time_ms = 10.0

        with (
            patch(
                "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
                return_value=("/fake/repo", None),
            ),
            patch(
                "code_indexer.server.mcp.handlers._legacy.GitOperationsService"
            ) as mock_git_svc_cls,
            patch(
                "code_indexer.server.mcp.reranking._apply_reranking_sync",
                return_value=(
                    [{"diff_snippet": "+def foo(): return 42"}],
                    {
                        "reranker_used": False,
                        "reranker_provider": None,
                        "rerank_time_ms": 0,
                        "rerank_hint": None,
                    },
                ),
            ) as mock_rerank,
        ):
            mock_git_svc = MagicMock()
            mock_git_svc.search_diffs.return_value = fake_result
            mock_git_svc_cls.return_value = mock_git_svc

            args = {
                "repository_alias": "test-repo-global",
                "search_string": "foo",
                "is_regex": False,
                "rerank_query": "find foo additions",
                "limit": 5,
            }

            handle_git_search_diffs(args, user)

        mock_rerank.assert_called_once()
        call_args = mock_rerank.call_args
        # Extract the content_extractor argument (positional or keyword)
        if call_args[1] and "content_extractor" in call_args[1]:
            extractor = call_args[1]["content_extractor"]
        else:
            extractor = call_args[0][3]

        sample = {"diff_snippet": "+def foo(): return 42"}
        assert extractor(sample) == "+def foo(): return 42"
        sample_empty = {"subject": "no snippet"}
        assert extractor(sample_empty) == "no snippet"


class TestGitSearchCommitsHandlerRerankingWiring:
    """_omni_git_search_commits passes subject+body extractor to _apply_reranking_sync."""

    def test_rerank_called_with_commit_extractor(self):
        """_omni_git_search_commits calls _apply_reranking_sync with combined subject+body extractor."""
        from code_indexer.server.mcp.handlers._legacy import _omni_git_search_commits
        from code_indexer.server.auth.user_manager import User
        import json as json_module

        user = MagicMock(spec=User)
        user.username = "testuser"

        # Build a fake single-repo result that _omni would call handle_git_search_commits for
        fake_single_response = {
            "content": [
                {
                    "type": "text",
                    "text": json_module.dumps(
                        {
                            "success": True,
                            "query": "fix",
                            "is_regex": False,
                            "matches": [
                                {
                                    "subject": "fix: auth bug",
                                    "body": "Details here.",
                                    "hash": "abc123",
                                    "short_hash": "abc1",
                                    "author_name": "Dev",
                                    "author_email": "dev@example.com",
                                    "author_date": "2024-01-01",
                                    "match_highlights": [],
                                }
                            ],
                            "total_matches": 1,
                            "truncated": False,
                            "search_time_ms": 5.0,
                        }
                    ),
                }
            ]
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._legacy._expand_wildcard_patterns",
                return_value=["test-repo-global"],
            ),
            patch(
                "code_indexer.server.mcp.handlers._legacy.handle_git_search_commits",
                return_value=fake_single_response,
            ),
            patch(
                "code_indexer.server.mcp.handlers._legacy._get_access_filtering_service",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.reranking._apply_reranking_sync",
                return_value=(
                    [
                        {
                            "subject": "fix: auth bug",
                            "body": "Details here.",
                            "source_repo": "test-repo-global",
                        }
                    ],
                    {
                        "reranker_used": False,
                        "reranker_provider": None,
                        "rerank_time_ms": 0,
                        "rerank_hint": None,
                    },
                ),
            ) as mock_rerank,
        ):
            args = {
                "repository_alias": ["test-repo-global"],
                "query": "fix",
                "is_regex": False,
                "rerank_query": "find auth bug fix commits",
                "limit": 5,
            }

            _omni_git_search_commits(args, user)

        mock_rerank.assert_called_once()
        call_args = mock_rerank.call_args
        if call_args[1] and "content_extractor" in call_args[1]:
            extractor = call_args[1]["content_extractor"]
        else:
            extractor = call_args[0][3]

        sample = {"subject": "fix: auth bug", "body": "Details here."}
        assert extractor(sample) == "fix: auth bug Details here."
        sample_no_body = {"subject": "chore: bump", "body": ""}
        assert extractor(sample_no_body) == "chore: bump"


# ---------------------------------------------------------------------------
# PART 5: Story #653 AC3 — overfetch limit wired in handlers
# ---------------------------------------------------------------------------


class TestAC3OverfetchLimitGlobalPath:
    """search_code (global repo path) must query with overfetched limit when reranking."""

    def test_perform_search_called_with_overfetch_limit_when_rerank_query_set(self):
        """_perform_search must receive limit=requested*overfetch_multiplier (5*5=25)."""
        from code_indexer.server.mcp.handlers._legacy import search_code

        fake = _fake_search_result()
        params = {
            "repository_alias": "test-repo-global",
            "query_text": "foo function",
            "rerank_query": "find the foo function",
            "limit": 5,
        }
        with _patched_global_repo_env(_default_rerank_meta(), fake) as mock_utils:
            search_code(params, _fake_user())

        call_args = (
            mock_utils.app_module.semantic_query_manager._perform_search.call_args
        )
        actual_limit = call_args[1].get(
            "limit", call_args[0][3] if len(call_args[0]) > 3 else None
        )
        assert actual_limit == 25, (
            f"Expected _perform_search called with limit=25 (5*5), got {actual_limit}"
        )

    def test_perform_search_uses_requested_limit_without_rerank_query(self):
        """Without rerank_query, _perform_search must receive the plain requested limit."""
        from code_indexer.server.mcp.handlers._legacy import search_code

        fake = _fake_search_result()
        no_rerank_meta = {
            "reranker_used": False,
            "reranker_provider": None,
            "rerank_time_ms": 0,
            "rerank_hint": None,
        }
        params = {
            "repository_alias": "test-repo-global",
            "query_text": "foo function",
            "limit": 5,
        }
        with _patched_global_repo_env(no_rerank_meta, fake) as mock_utils:
            search_code(params, _fake_user())

        call_args = (
            mock_utils.app_module.semantic_query_manager._perform_search.call_args
        )
        actual_limit = call_args[1].get(
            "limit", call_args[0][3] if len(call_args[0]) > 3 else None
        )
        assert actual_limit == 5, (
            f"Expected _perform_search called with limit=5 (no reranking), got {actual_limit}"
        )


# ---------------------------------------------------------------------------
# Named constants for AC3 / overfetch tests
# ---------------------------------------------------------------------------

_TEST_SIMILARITY_SCORE = 0.9
_TEST_RERANK_TIME_MS = 5


def _fake_user():
    """Return a mock User with username 'testuser'."""
    from code_indexer.server.auth.user_manager import User

    user = MagicMock(spec=User)
    user.username = "testuser"
    return user


def _fake_search_result():
    """Return a mock result object with a .to_dict() returning a standard dict."""
    result = MagicMock()
    result.to_dict.return_value = {
        "file_path": "src/foo.py",
        "content": "def foo(): return 42",
        "similarity_score": _TEST_SIMILARITY_SCORE,
    }
    return result


def _default_rerank_meta():
    """Return a minimal rerank_metadata dict for tests that need reranking active."""
    return {
        "reranker_used": True,
        "reranker_provider": "voyage",
        "rerank_time_ms": _TEST_RERANK_TIME_MS,
        "rerank_hint": None,
    }


# ---------------------------------------------------------------------------
# PART 4: Story #654 — telemetry fields in query_metadata (search_code global path)
# ---------------------------------------------------------------------------


def _run_search_code_with_rerank_meta(rerank_meta: dict) -> dict:
    """Run search_code handler with a patched _apply_reranking_sync returning rerank_meta.

    Returns the parsed JSON payload from the MCP response.
    """
    import json
    from code_indexer.server.mcp.handlers._legacy import search_code
    from code_indexer.server.auth.user_manager import User

    user = MagicMock(spec=User)
    user.username = "testuser"
    fake_result_dict = {
        "file_path": "src/foo.py",
        "content": "def foo(): return 42",
        "similarity_score": 0.9,
    }
    fake_result = MagicMock()
    fake_result.to_dict.return_value = fake_result_dict

    with (
        patch(
            "code_indexer.server.mcp.handlers._legacy._get_golden_repos_dir",
            return_value="/fake/golden",
        ),
        patch(
            "code_indexer.server.mcp.handlers._legacy._list_global_repos",
            return_value=[
                {
                    "alias_name": "test-repo-global",
                    "repo_name": "test-repo",
                    "repo_url": "https://example.com/test.git",
                }
            ],
        ),
        patch(
            "code_indexer.global_repos.alias_manager.AliasManager"
        ) as mock_alias_mgr_cls,
        patch("code_indexer.server.mcp.handlers._legacy._utils") as mock_utils,
        patch(
            "code_indexer.server.mcp.handlers._legacy.get_config_service",
            return_value=_make_config_service_with_rerank(),
        ),
        patch(
            "code_indexer.server.mcp.handlers._legacy._get_access_filtering_service",
            return_value=None,
        ),
        patch(
            "code_indexer.server.mcp.handlers._legacy._get_query_tracker",
            return_value=None,
        ),
        patch(
            "code_indexer.server.mcp.handlers._legacy._get_wiki_enabled_repos",
            return_value=set(),
        ),
        patch("pathlib.Path.exists", return_value=True),
        patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            return_value=([fake_result_dict], rerank_meta),
        ),
    ):
        mock_alias_mgr_cls.return_value.read_alias.return_value = (
            "/fake/golden/test-repo-global"
        )
        mock_utils.app_module.semantic_query_manager._perform_search.return_value = [
            fake_result
        ]
        mock_utils.app_module.golden_repo_manager = None
        params = {
            "repository_alias": "test-repo-global",
            "query_text": "foo",
            "rerank_query": "find foo",
            "limit": 5,
        }
        response = search_code(params, user)

    return json.loads(response["content"][0]["text"])


class TestSearchCodeHandlerTelemetryFields:
    """search_code handler must include reranker telemetry in query_metadata (Story #654)."""

    def test_query_metadata_contains_telemetry_fields(self):
        """query_metadata must contain reranker_used, reranker_provider, rerank_time_ms."""
        rerank_meta = {
            "reranker_used": True,
            "reranker_provider": "voyage",
            "rerank_time_ms": 12,
            "rerank_hint": None,
        }
        payload = _run_search_code_with_rerank_meta(rerank_meta)

        assert payload["success"] is True
        query_metadata = payload["results"].get("query_metadata", {})
        assert "reranker_used" in query_metadata, "query_metadata missing reranker_used"
        assert "reranker_provider" in query_metadata, (
            "query_metadata missing reranker_provider"
        )
        assert "rerank_time_ms" in query_metadata, (
            "query_metadata missing rerank_time_ms"
        )
        assert query_metadata["reranker_used"] is True
        assert query_metadata["reranker_provider"] == "voyage"
        assert query_metadata["rerank_time_ms"] == 12

    def test_query_metadata_no_reranking_shows_false(self):
        """When reranking not used, telemetry fields show not-requested state."""
        rerank_meta = {
            "reranker_used": False,
            "reranker_provider": None,
            "rerank_time_ms": 0,
            "rerank_hint": None,
        }
        payload = _run_search_code_with_rerank_meta(rerank_meta)

        query_metadata = payload["results"].get("query_metadata", {})
        assert query_metadata.get("reranker_used") is False
        assert query_metadata.get("reranker_provider") is None
        assert query_metadata.get("rerank_time_ms") == 0
