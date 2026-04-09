"""Unit tests for Story #660: rerank_query/rerank_instruction on git_log.

Tests verify all acceptance criteria:
1. Tool schema includes rerank_query and rerank_instruction parameters.
2. When rerank_query=None, _apply_reranking_sync is not called (no overhead).
3. When rerank_query is provided, _apply_reranking_sync is called with
   message-based extractor and correct requested_limit.
4. Overfetch: fetch_limit = min(limit * 5, 200) when rerank_query is set.
5. Response shape preserved (commits_returned, total_commits, has_more,
   next_offset, offset, limit) plus new query_metadata field.
6. query_metadata contains reranker_used, reranker_provider, rerank_time_ms.
7. Provider fallback reflected in metadata (Cohere when Voyage fails).

NOTE: The ACTIVE git_log handler is the second registration in
HANDLER_REGISTRY (overrides the older handle_git_log). It uses
git_operations_service.git_log() which returns commits in
{"commit_hash", "author", "date", "message"} format.

Story #660, Epic #649.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.mcp.tools import TOOL_REGISTRY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Module path where git_log handler lives (for patching)
_HANDLER_MOD = "code_indexer.server.mcp.handlers._legacy"


def _fake_user():
    """Return a mock User with username 'testuser'."""
    from code_indexer.server.auth.user_manager import User

    user = MagicMock(spec=User)
    user.username = "testuser"
    return user


def _make_service_commits(count=2):
    """Build commit dicts in the format git_operations_service.git_log returns."""
    commits = []
    for i in range(count):
        commits.append(
            {
                "commit_hash": f"abc{i:04d}def456",
                "author": "Dev",
                "date": "2024-01-01 10:00:00 +0000",
                "message": f"fix: commit number {i}",
            }
        )
    return commits


def _make_service_result(commits=None, total_commits=None, offset=0, limit=50):
    """Build a dict matching git_operations_service.git_log return shape."""
    c = commits or _make_service_commits(2)
    total = total_commits if total_commits is not None else len(c)
    has_more = (offset + len(c)) < total
    return {
        "commits": c,
        "commits_returned": len(c),
        "total_commits": total,
        "has_more": has_more,
        "next_offset": (offset + len(c)) if has_more else None,
        "offset": offset,
        "limit": limit,
    }


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


def _default_rerank_meta(used=False, provider=None, time_ms=0):
    return {
        "reranker_used": used,
        "reranker_provider": provider,
        "rerank_time_ms": time_ms,
        "rerank_hint": None,
    }


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def git_log_svc_mock():
    """Patch _resolve_git_repo_path and git_operations_service for git_log."""
    fake_result = _make_service_result()
    with (
        patch(
            f"{_HANDLER_MOD}._resolve_git_repo_path",
            return_value=("/fake/repo", None),
        ),
        patch(f"{_HANDLER_MOD}.git_operations_service") as mock_svc,
    ):
        mock_svc.git_log.return_value = fake_result
        yield mock_svc


@pytest.fixture
def git_log_svc_with_rerank_mock():
    """Patch repo path, git_operations_service, and get_config_service."""
    fake_result = _make_service_result()
    cfg_svc = _make_config_service_with_rerank()
    with (
        patch(
            f"{_HANDLER_MOD}._resolve_git_repo_path",
            return_value=("/fake/repo", None),
        ),
        patch(f"{_HANDLER_MOD}.git_operations_service") as mock_svc,
        patch(
            f"{_HANDLER_MOD}.get_config_service",
            return_value=cfg_svc,
        ),
    ):
        mock_svc.git_log.return_value = fake_result
        yield mock_svc


# ---------------------------------------------------------------------------
# PART 1: Tool schema -- rerank_query and rerank_instruction present
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("param_name", ["rerank_query", "rerank_instruction"])
class TestGitLogToolSchema:
    """git_log tool schema must expose rerank_query and rerank_instruction."""

    def test_param_exists_in_properties(self, param_name):
        tool = TOOL_REGISTRY["git_log"]
        props = tool["inputSchema"]["properties"]
        assert param_name in props, (
            f"git_log is missing '{param_name}' in inputSchema.properties"
        )

    def test_param_is_string_type(self, param_name):
        tool = TOOL_REGISTRY["git_log"]
        props = tool["inputSchema"]["properties"]
        assert props[param_name]["type"] == "string"

    def test_param_is_optional(self, param_name):
        tool = TOOL_REGISTRY["git_log"]
        required = tool["inputSchema"].get("required", [])
        assert param_name not in required


class TestGitLogOutputSchema:
    """git_log output schema must expose query_metadata."""

    def test_query_metadata_in_output_schema(self):
        tool = TOOL_REGISTRY["git_log"]
        output_props = tool.get("outputSchema", {}).get("properties", {})
        assert "query_metadata" in output_props


# ---------------------------------------------------------------------------
# PART 2: Reranking guard -- no overhead when rerank_query is absent
# ---------------------------------------------------------------------------


class TestGitLogNoRerankOverhead:
    """When rerank_query is absent, _apply_reranking_sync must not be called."""

    def test_no_rerank_query_skips_reranking(self, git_log_svc_mock):
        from code_indexer.server.mcp.handlers._legacy import git_log

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync"
        ) as mock_rerank:
            args = {"repository_alias": "test-repo", "limit": 10}
            git_log(args, _fake_user())
        mock_rerank.assert_not_called()

    def test_empty_rerank_query_skips_reranking(self, git_log_svc_mock):
        from code_indexer.server.mcp.handlers._legacy import git_log

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync"
        ) as mock_rerank:
            args = {"repository_alias": "test-repo", "rerank_query": "", "limit": 10}
            git_log(args, _fake_user())
        mock_rerank.assert_not_called()

    def test_no_rerank_query_returns_identical_results(self, git_log_svc_mock):
        """Without rerank_query, results match current behavior with full response shape."""
        from code_indexer.server.mcp.handlers._legacy import git_log

        commits = [
            {
                "commit_hash": "aaa",
                "author": "Dev",
                "date": "2024-01-01",
                "message": "first",
            },
            {
                "commit_hash": "bbb",
                "author": "Dev",
                "date": "2024-01-02",
                "message": "second",
            },
        ]
        git_log_svc_mock.git_log.return_value = _make_service_result(
            commits=commits, limit=10
        )

        args = {"repository_alias": "test-repo", "limit": 10}
        response = git_log(args, _fake_user())

        payload = json.loads(response["content"][0]["text"])
        assert payload["success"] is True
        assert payload["commits"][0]["message"] == "first"
        assert payload["commits"][1]["message"] == "second"
        assert payload["commits_returned"] == 2
        assert payload["total_commits"] == 2
        assert payload["has_more"] is False
        assert payload["next_offset"] is None
        assert payload["offset"] == 0
        assert payload["limit"] == 10


# ---------------------------------------------------------------------------
# PART 3: Reranking wiring -- _apply_reranking_sync called correctly
# ---------------------------------------------------------------------------


class TestGitLogRerankingWiring:
    """git_log calls _apply_reranking_sync with message-based extractor."""

    def test_rerank_called_with_commit_extractor(self, git_log_svc_with_rerank_mock):
        from code_indexer.server.mcp.handlers._legacy import git_log

        commits = [
            {
                "commit_hash": "abc123",
                "author": "Dev",
                "date": "2024-01-01",
                "message": "fix: auth session expired",
            }
        ]
        git_log_svc_with_rerank_mock.git_log.return_value = _make_service_result(
            commits=commits
        )

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            return_value=(
                commits,
                _default_rerank_meta(used=True, provider="voyage", time_ms=10),
            ),
        ) as mock_rerank:
            args = {
                "repository_alias": "test-repo",
                "rerank_query": "commits that fixed authentication session bugs",
                "rerank_instruction": "Focus on session or token handling",
                "limit": 10,
            }
            git_log(args, _fake_user())

        mock_rerank.assert_called_once()
        call_kwargs = mock_rerank.call_args[1]

        assert (
            call_kwargs["rerank_query"]
            == "commits that fixed authentication session bugs"
        )
        assert call_kwargs["rerank_instruction"] == "Focus on session or token handling"

        # Verify content extractor uses message field
        extractor = call_kwargs["content_extractor"]
        assert (
            extractor({"message": "fix: auth session expired"})
            == "fix: auth session expired"
        )
        assert extractor({"message": "chore: bump"}) == "chore: bump"
        assert extractor({"message": ""}) == ""
        assert extractor({}) == ""

    def test_rerank_requested_limit_matches_user_limit(
        self, git_log_svc_with_rerank_mock
    ):
        from code_indexer.server.mcp.handlers._legacy import git_log

        git_log_svc_with_rerank_mock.git_log.return_value = _make_service_result()

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            return_value=(_make_service_commits(1), _default_rerank_meta()),
        ) as mock_rerank:
            args = {
                "repository_alias": "test-repo",
                "rerank_query": "auth session commits",
                "limit": 7,
            }
            git_log(args, _fake_user())

        call_kwargs = mock_rerank.call_args[1]
        assert call_kwargs["requested_limit"] == 7


# ---------------------------------------------------------------------------
# PART 4: Overfetch -- service called with 5x limit when rerank_query set
# ---------------------------------------------------------------------------


class TestGitLogOverfetch:
    """When rerank_query is set, service.git_log must be called with overfetched limit."""

    def test_service_called_with_overfetch_limit(self, git_log_svc_with_rerank_mock):
        """service.git_log receives limit=min(requested*5, 200) when rerank_query set."""
        from code_indexer.server.mcp.handlers._legacy import git_log

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            return_value=([{}], _default_rerank_meta()),
        ):
            args = {
                "repository_alias": "test-repo",
                "rerank_query": "auth session commits",
                "limit": 10,
            }
            git_log(args, _fake_user())

        call_kwargs = git_log_svc_with_rerank_mock.git_log.call_args[1]
        assert call_kwargs["limit"] == 50, (
            f"Expected limit=50 (10*5), got {call_kwargs['limit']}"
        )

    def test_overfetch_capped_at_200(self, git_log_svc_with_rerank_mock):
        from code_indexer.server.mcp.handlers._legacy import git_log

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            return_value=([{}], _default_rerank_meta()),
        ):
            args = {
                "repository_alias": "test-repo",
                "rerank_query": "auth session commits",
                "limit": 100,
            }
            git_log(args, _fake_user())

        call_kwargs = git_log_svc_with_rerank_mock.git_log.call_args[1]
        assert call_kwargs["limit"] == 200, (
            f"Expected limit=200 (capped), got {call_kwargs['limit']}"
        )

    def test_no_overfetch_without_rerank_query(self, git_log_svc_mock):
        from code_indexer.server.mcp.handlers._legacy import git_log

        args = {"repository_alias": "test-repo", "limit": 10}
        git_log(args, _fake_user())

        call_kwargs = git_log_svc_mock.git_log.call_args[1]
        assert call_kwargs["limit"] == 10


# ---------------------------------------------------------------------------
# PART 5: Response query_metadata telemetry fields
# ---------------------------------------------------------------------------


class TestGitLogQueryMetadata:
    """Response must include query_metadata with reranker telemetry."""

    def _run_with_rerank_meta(self, rerank_meta: dict) -> dict:
        from code_indexer.server.mcp.handlers._legacy import git_log

        commits = _make_service_commits(1)
        fake_result = _make_service_result(commits=commits, limit=5)

        with (
            patch(
                f"{_HANDLER_MOD}._resolve_git_repo_path",
                return_value=("/fake/repo", None),
            ),
            patch(f"{_HANDLER_MOD}.git_operations_service") as mock_svc,
            patch(
                f"{_HANDLER_MOD}.get_config_service",
                return_value=_make_config_service_with_rerank(),
            ),
            patch(
                "code_indexer.server.mcp.reranking._apply_reranking_sync",
                return_value=(commits, rerank_meta),
            ),
        ):
            mock_svc.git_log.return_value = fake_result
            args = {
                "repository_alias": "test-repo",
                "rerank_query": "auth session commits",
                "limit": 5,
            }
            response = git_log(args, _fake_user())

        result: dict = json.loads(response["content"][0]["text"])
        return result

    def test_query_metadata_present_when_reranking_active(self):
        rerank_meta = _default_rerank_meta(used=True, provider="voyage", time_ms=12)
        payload = self._run_with_rerank_meta(rerank_meta)
        assert payload["success"] is True
        assert "query_metadata" in payload

    def test_query_metadata_contains_required_fields(self):
        rerank_meta = _default_rerank_meta(used=True, provider="voyage", time_ms=12)
        payload = self._run_with_rerank_meta(rerank_meta)
        qm = payload["query_metadata"]
        assert "reranker_used" in qm
        assert "reranker_provider" in qm
        assert "rerank_time_ms" in qm

    def test_query_metadata_voyage_provider_values(self):
        rerank_meta = _default_rerank_meta(used=True, provider="voyage", time_ms=15)
        payload = self._run_with_rerank_meta(rerank_meta)
        qm = payload["query_metadata"]
        assert qm["reranker_used"] is True
        assert qm["reranker_provider"] == "voyage"
        assert qm["rerank_time_ms"] == 15

    def test_query_metadata_not_used_values(self):
        rerank_meta = _default_rerank_meta(used=False, provider=None, time_ms=0)
        payload = self._run_with_rerank_meta(rerank_meta)
        qm = payload["query_metadata"]
        assert qm["reranker_used"] is False
        assert qm["reranker_provider"] is None
        assert qm["rerank_time_ms"] == 0

    def test_no_rerank_query_includes_query_metadata_with_used_false(
        self, git_log_svc_mock
    ):
        """Without rerank_query, response still has query_metadata with reranker_used=False."""
        from code_indexer.server.mcp.handlers._legacy import git_log

        git_log_svc_mock.git_log.return_value = _make_service_result(limit=10)
        args = {"repository_alias": "test-repo", "limit": 10}
        response = git_log(args, _fake_user())

        payload = json.loads(response["content"][0]["text"])
        assert payload["success"] is True
        assert "query_metadata" in payload
        assert payload["query_metadata"]["reranker_used"] is False
        assert payload["commits_returned"] == 2
        assert payload["total_commits"] == 2
        assert payload["next_offset"] is None


# ---------------------------------------------------------------------------
# PART 6: Provider fallback -- Voyage fails, Cohere used
# ---------------------------------------------------------------------------


class TestGitLogProviderFallback:
    """Reranking provider fallback behavior in git_log handler."""

    def test_cohere_fallback_reflected_in_metadata(self, git_log_svc_with_rerank_mock):
        from code_indexer.server.mcp.handlers._legacy import git_log

        commits = _make_service_commits(1)
        git_log_svc_with_rerank_mock.git_log.return_value = _make_service_result(
            commits=commits
        )
        cohere_meta = _default_rerank_meta(used=True, provider="cohere", time_ms=20)

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            return_value=(commits, cohere_meta),
        ):
            args = {
                "repository_alias": "test-repo",
                "rerank_query": "auth session commits",
                "limit": 5,
            }
            response = git_log(args, _fake_user())

        payload = json.loads(response["content"][0]["text"])
        qm = payload["query_metadata"]
        assert qm["reranker_used"] is True
        assert qm["reranker_provider"] == "cohere"
        assert qm["rerank_time_ms"] == 20
