"""Unit tests for Story #658: rerank_query/rerank_instruction on git_file_history.

Tests verify:
1. Tool schema includes rerank_query and rerank_instruction parameters.
2. When rerank_query=None, _apply_reranking_sync is not called (no overhead).
3. When rerank_query is provided, _apply_reranking_sync is called with subject-only extractor.
4. Response includes query_metadata with reranker telemetry (no rerank_hint field).
5. Overfetch: fetch_limit = min(limit * 5, 200) when rerank_query is set.

Story #658, Epic #649.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.mcp.tools import TOOL_REGISTRY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_user():
    """Return a mock User with username 'testuser'."""
    from code_indexer.server.auth.user_manager import User

    user = MagicMock(spec=User)
    user.username = "testuser"
    return user


def _make_file_commit(
    hash_val="abc123def456",
    short_hash="abc123d",
    author_name="Dev",
    author_date="2024-01-01",
    subject="fix: auth bug",
    insertions=5,
    deletions=2,
    old_path=None,
):
    """Build a mock FileHistoryCommit."""
    commit = MagicMock()
    commit.hash = hash_val
    commit.short_hash = short_hash
    commit.author_name = author_name
    commit.author_date = author_date
    commit.subject = subject
    commit.insertions = insertions
    commit.deletions = deletions
    commit.old_path = old_path
    return commit


def _commit_to_dict(commit) -> dict:
    """Convert a mock FileHistoryCommit to the dict shape the handler produces."""
    return {
        "hash": commit.hash,
        "short_hash": commit.short_hash,
        "author_name": commit.author_name,
        "author_date": commit.author_date,
        "subject": commit.subject,
        "insertions": commit.insertions,
        "deletions": commit.deletions,
        "old_path": commit.old_path,
    }


def _make_file_history_result(commits=None, path="src/auth.py", truncated=False):
    """Build a mock FileHistoryResult."""
    result = MagicMock()
    result.path = path
    result.commits = commits or [_make_file_commit()]
    result.total_count = len(result.commits)
    result.truncated = truncated
    result.renamed_from = None
    return result


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
def git_svc_mock():
    """Patch _resolve_git_repo_path and GitOperationsService for all handler tests."""
    fake_result = _make_file_history_result()
    with (
        patch(
            "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
            return_value=("/fake/repo", None),
        ),
        patch(
            "code_indexer.server.mcp.handlers._legacy.GitOperationsService"
        ) as mock_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.get_file_history.return_value = fake_result
        mock_cls.return_value = mock_svc
        yield mock_svc


@pytest.fixture
def git_svc_with_rerank_mock():
    """Patch repo path, GitOperationsService, and get_config_service (rerank enabled)."""
    fake_result = _make_file_history_result()
    with (
        patch(
            "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
            return_value=("/fake/repo", None),
        ),
        patch(
            "code_indexer.server.mcp.handlers._legacy.GitOperationsService"
        ) as mock_cls,
        patch(
            "code_indexer.server.mcp.handlers._legacy.get_config_service",
            return_value=_make_config_service_with_rerank(),
        ),
    ):
        mock_svc = MagicMock()
        mock_svc.get_file_history.return_value = fake_result
        mock_cls.return_value = mock_svc
        yield mock_svc


# ---------------------------------------------------------------------------
# PART 1: Tool schema — rerank_query and rerank_instruction present
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("param_name", ["rerank_query", "rerank_instruction"])
class TestGitFileHistoryToolSchema:
    """git_file_history tool schema must expose rerank_query and rerank_instruction."""

    def test_param_exists_in_properties(self, param_name):
        """Parameter must be present in inputSchema.properties."""
        tool = TOOL_REGISTRY["git_file_history"]
        props = tool["inputSchema"]["properties"]
        assert param_name in props, (
            f"git_file_history is missing '{param_name}' in inputSchema.properties"
        )

    def test_param_is_string_type(self, param_name):
        """Parameter must be declared as type string."""
        tool = TOOL_REGISTRY["git_file_history"]
        props = tool["inputSchema"]["properties"]
        assert props[param_name]["type"] == "string", (
            f"git_file_history property '{param_name}' must be type string"
        )

    def test_param_is_optional(self, param_name):
        """Parameter must be optional (not in required list)."""
        tool = TOOL_REGISTRY["git_file_history"]
        required = tool["inputSchema"].get("required", [])
        assert param_name not in required, (
            f"git_file_history property '{param_name}' must not be required"
        )


class TestGitFileHistoryOutputSchema:
    """git_file_history output schema must expose query_metadata."""

    def test_query_metadata_in_output_schema(self):
        """query_metadata must be present in outputSchema.properties."""
        tool = TOOL_REGISTRY["git_file_history"]
        output_props = tool.get("outputSchema", {}).get("properties", {})
        assert "query_metadata" in output_props, (
            "git_file_history outputSchema missing 'query_metadata'"
        )


# ---------------------------------------------------------------------------
# PART 2: Reranking guard — no overhead when rerank_query is absent
# ---------------------------------------------------------------------------


class TestGitFileHistoryNoRerankOverhead:
    """When rerank_query is absent, _apply_reranking_sync must not be called."""

    def test_no_rerank_query_skips_reranking(self, git_svc_mock):
        """When rerank_query=None, _apply_reranking_sync must not be invoked."""
        from code_indexer.server.mcp.handlers._legacy import handle_git_file_history

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync"
        ) as mock_rerank:
            args = {
                "repository_alias": "test-repo",
                "path": "src/auth.py",
                "limit": 10,
            }
            handle_git_file_history(args, _fake_user())

        mock_rerank.assert_not_called()

    def test_empty_rerank_query_skips_reranking(self, git_svc_mock):
        """When rerank_query='', _apply_reranking_sync must not be invoked."""
        from code_indexer.server.mcp.handlers._legacy import handle_git_file_history

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync"
        ) as mock_rerank:
            args = {
                "repository_alias": "test-repo",
                "path": "src/auth.py",
                "rerank_query": "",
                "limit": 10,
            }
            handle_git_file_history(args, _fake_user())

        mock_rerank.assert_not_called()

    def test_no_rerank_query_returns_identical_results(self):
        """Without rerank_query, results match current behavior (original order)."""
        from code_indexer.server.mcp.handlers._legacy import handle_git_file_history

        commits = [
            _make_file_commit(subject="first commit"),
            _make_file_commit(subject="second commit"),
        ]
        fake_result = _make_file_history_result(commits=commits)

        with (
            patch(
                "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
                return_value=("/fake/repo", None),
            ),
            patch(
                "code_indexer.server.mcp.handlers._legacy.GitOperationsService"
            ) as mock_git_svc_cls,
        ):
            mock_git_svc = MagicMock()
            mock_git_svc.get_file_history.return_value = fake_result
            mock_git_svc_cls.return_value = mock_git_svc

            args = {
                "repository_alias": "test-repo",
                "path": "src/auth.py",
                "limit": 10,
            }
            response = handle_git_file_history(args, _fake_user())

        payload = json.loads(response["content"][0]["text"])
        assert payload["success"] is True
        assert payload["commits"][0]["subject"] == "first commit"
        assert payload["commits"][1]["subject"] == "second commit"


# ---------------------------------------------------------------------------
# PART 3: Reranking wiring — _apply_reranking_sync called with correct extractor
# ---------------------------------------------------------------------------


class TestGitFileHistoryRerankingWiring:
    """handle_git_file_history calls _apply_reranking_sync with subject-only extractor."""

    def test_rerank_called_with_commit_extractor(self, git_svc_with_rerank_mock):
        """When rerank_query is set, _apply_reranking_sync is invoked with subject-only extractor."""
        from code_indexer.server.mcp.handlers._legacy import handle_git_file_history

        commit = _make_file_commit(subject="fix: auth session expired")
        git_svc_with_rerank_mock.get_file_history.return_value = (
            _make_file_history_result(commits=[commit])
        )

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            return_value=(
                [_commit_to_dict(commit)],
                _default_rerank_meta(used=True, provider="voyage", time_ms=10),
            ),
        ) as mock_rerank:
            args = {
                "repository_alias": "test-repo",
                "path": "src/auth.py",
                "rerank_query": "commits that fixed authentication session bugs",
                "rerank_instruction": "Focus on session or token handling",
                "limit": 10,
            }
            handle_git_file_history(args, _fake_user())

        mock_rerank.assert_called_once()
        call_kwargs = mock_rerank.call_args[1]

        assert (
            call_kwargs["rerank_query"]
            == "commits that fixed authentication session bugs"
        )
        assert call_kwargs["rerank_instruction"] == "Focus on session or token handling"

        # Verify content extractor uses subject only (body is always None in FileHistoryCommit)
        extractor = call_kwargs["content_extractor"]
        assert (
            extractor({"subject": "fix: auth session expired"})
            == "fix: auth session expired"
        )
        assert extractor({"subject": "chore: bump"}) == "chore: bump"
        assert extractor({"subject": ""}) == ""
        assert extractor({}) == ""

    def test_rerank_requested_limit_matches_user_limit(self, git_svc_with_rerank_mock):
        """requested_limit passed to _apply_reranking_sync matches user-specified limit."""
        from code_indexer.server.mcp.handlers._legacy import handle_git_file_history

        commit = _make_file_commit()
        git_svc_with_rerank_mock.get_file_history.return_value = (
            _make_file_history_result(commits=[commit])
        )

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            return_value=([_commit_to_dict(commit)], _default_rerank_meta()),
        ) as mock_rerank:
            args = {
                "repository_alias": "test-repo",
                "path": "src/auth.py",
                "rerank_query": "auth session commits",
                "limit": 7,
            }
            handle_git_file_history(args, _fake_user())

        call_kwargs = mock_rerank.call_args[1]
        assert call_kwargs["requested_limit"] == 7


# ---------------------------------------------------------------------------
# PART 4: Overfetch — get_file_history called with 5x limit when rerank_query set
# ---------------------------------------------------------------------------


class TestGitFileHistoryOverfetch:
    """When rerank_query is set, get_file_history must be called with overfetched limit."""

    def test_get_file_history_called_with_overfetch_limit(
        self, git_svc_with_rerank_mock
    ):
        """get_file_history receives limit=min(requested*5, 200) when rerank_query set."""
        from code_indexer.server.mcp.handlers._legacy import handle_git_file_history

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            return_value=([{}], _default_rerank_meta()),
        ):
            args = {
                "repository_alias": "test-repo",
                "path": "src/auth.py",
                "rerank_query": "auth session commits",
                "limit": 10,
            }
            handle_git_file_history(args, _fake_user())

        call_kwargs = git_svc_with_rerank_mock.get_file_history.call_args[1]
        assert call_kwargs["limit"] == 50, (
            f"Expected get_file_history called with limit=50 (10*5), got {call_kwargs['limit']}"
        )

    def test_overfetch_capped_at_200(self, git_svc_with_rerank_mock):
        """Overfetch limit is capped at 200 even for large requested limits."""
        from code_indexer.server.mcp.handlers._legacy import handle_git_file_history

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            return_value=([{}], _default_rerank_meta()),
        ):
            args = {
                "repository_alias": "test-repo",
                "path": "src/auth.py",
                "rerank_query": "auth session commits",
                "limit": 100,  # 100 * 5 = 500, capped at 200
            }
            handle_git_file_history(args, _fake_user())

        call_kwargs = git_svc_with_rerank_mock.get_file_history.call_args[1]
        assert call_kwargs["limit"] == 200, (
            f"Expected get_file_history called with limit=200 (capped), got {call_kwargs['limit']}"
        )

    def test_no_overfetch_without_rerank_query(self, git_svc_mock):
        """Without rerank_query, get_file_history receives the plain requested limit."""
        from code_indexer.server.mcp.handlers._legacy import handle_git_file_history

        args = {
            "repository_alias": "test-repo",
            "path": "src/auth.py",
            "limit": 10,
        }
        handle_git_file_history(args, _fake_user())

        call_kwargs = git_svc_mock.get_file_history.call_args[1]
        assert call_kwargs["limit"] == 10, (
            f"Expected get_file_history called with limit=10 (no reranking), got {call_kwargs['limit']}"
        )


# ---------------------------------------------------------------------------
# PART 5: Response query_metadata telemetry fields
# ---------------------------------------------------------------------------


class TestGitFileHistoryQueryMetadata:
    """Response must include query_metadata with reranker telemetry."""

    def _run_with_rerank_meta(self, rerank_meta: dict) -> dict:
        """Run handle_git_file_history with patched reranking; return parsed payload."""
        from code_indexer.server.mcp.handlers._legacy import handle_git_file_history

        commit = _make_file_commit()
        fake_result = _make_file_history_result(commits=[commit])

        with (
            patch(
                "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
                return_value=("/fake/repo", None),
            ),
            patch(
                "code_indexer.server.mcp.handlers._legacy.GitOperationsService"
            ) as mock_git_svc_cls,
            patch(
                "code_indexer.server.mcp.handlers._legacy.get_config_service",
                return_value=_make_config_service_with_rerank(),
            ),
            patch(
                "code_indexer.server.mcp.reranking._apply_reranking_sync",
                return_value=([_commit_to_dict(commit)], rerank_meta),
            ),
        ):
            mock_git_svc = MagicMock()
            mock_git_svc.get_file_history.return_value = fake_result
            mock_git_svc_cls.return_value = mock_git_svc

            args = {
                "repository_alias": "test-repo",
                "path": "src/auth.py",
                "rerank_query": "auth session commits",
                "limit": 5,
            }
            response = handle_git_file_history(args, _fake_user())

        result: dict = json.loads(response["content"][0]["text"])
        return result

    def test_query_metadata_present_when_reranking_active(self):
        """When rerank_query is provided, response must contain query_metadata."""
        rerank_meta = _default_rerank_meta(used=True, provider="voyage", time_ms=12)
        payload = self._run_with_rerank_meta(rerank_meta)

        assert payload["success"] is True
        assert "query_metadata" in payload, "query_metadata missing from response"

    def test_query_metadata_contains_required_fields(self):
        """query_metadata must contain reranker_used, reranker_provider, rerank_time_ms."""
        rerank_meta = _default_rerank_meta(used=True, provider="voyage", time_ms=12)
        payload = self._run_with_rerank_meta(rerank_meta)

        qm = payload["query_metadata"]
        assert "reranker_used" in qm
        assert "reranker_provider" in qm
        assert "rerank_time_ms" in qm

    def test_query_metadata_voyage_provider_values(self):
        """When Voyage reranks successfully, metadata shows correct values."""
        rerank_meta = _default_rerank_meta(used=True, provider="voyage", time_ms=15)
        payload = self._run_with_rerank_meta(rerank_meta)

        qm = payload["query_metadata"]
        assert qm["reranker_used"] is True
        assert qm["reranker_provider"] == "voyage"
        assert qm["rerank_time_ms"] == 15

    def test_query_metadata_not_used_values(self):
        """When reranking not used, metadata shows not-active state."""
        rerank_meta = _default_rerank_meta(used=False, provider=None, time_ms=0)
        payload = self._run_with_rerank_meta(rerank_meta)

        qm = payload["query_metadata"]
        assert qm["reranker_used"] is False
        assert qm["reranker_provider"] is None
        assert qm["rerank_time_ms"] == 0

    def test_query_metadata_no_rerank_hint(self):
        """query_metadata must NOT contain rerank_hint — matches git_search_commits pattern."""
        rerank_meta = _default_rerank_meta(used=True, provider="voyage", time_ms=12)
        payload = self._run_with_rerank_meta(rerank_meta)

        qm = payload["query_metadata"]
        assert "rerank_hint" not in qm, (
            "rerank_hint must not appear in query_metadata (not in outputSchema)"
        )

    def test_no_rerank_query_includes_query_metadata_with_used_false(self):
        """Without rerank_query, response still includes query_metadata with reranker_used=False."""
        from code_indexer.server.mcp.handlers._legacy import handle_git_file_history

        commit = _make_file_commit()
        fake_result = _make_file_history_result(commits=[commit])

        with (
            patch(
                "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
                return_value=("/fake/repo", None),
            ),
            patch(
                "code_indexer.server.mcp.handlers._legacy.GitOperationsService"
            ) as mock_git_svc_cls,
        ):
            mock_git_svc = MagicMock()
            mock_git_svc.get_file_history.return_value = fake_result
            mock_git_svc_cls.return_value = mock_git_svc

            args = {
                "repository_alias": "test-repo",
                "path": "src/auth.py",
                "limit": 10,
            }
            response = handle_git_file_history(args, _fake_user())

        payload = json.loads(response["content"][0]["text"])
        assert payload["success"] is True
        assert "query_metadata" in payload
        assert payload["query_metadata"]["reranker_used"] is False


# ---------------------------------------------------------------------------
# PART 6: Provider fallback — Voyage fails, Cohere used
# ---------------------------------------------------------------------------


class TestGitFileHistoryProviderFallback:
    """Reranking provider fallback behavior in git_file_history handler."""

    def test_cohere_fallback_reflected_in_metadata(self, git_svc_with_rerank_mock):
        """When Voyage fails and Cohere is used, metadata shows cohere as provider."""
        from code_indexer.server.mcp.handlers._legacy import handle_git_file_history

        commit = _make_file_commit()
        git_svc_with_rerank_mock.get_file_history.return_value = (
            _make_file_history_result(commits=[commit])
        )
        cohere_meta = _default_rerank_meta(used=True, provider="cohere", time_ms=20)

        with patch(
            "code_indexer.server.mcp.reranking._apply_reranking_sync",
            return_value=([_commit_to_dict(commit)], cohere_meta),
        ):
            args = {
                "repository_alias": "test-repo",
                "path": "src/auth.py",
                "rerank_query": "auth session commits",
                "limit": 5,
            }
            response = handle_git_file_history(args, _fake_user())

        payload = json.loads(response["content"][0]["text"])
        qm = payload["query_metadata"]
        assert qm["reranker_used"] is True
        assert qm["reranker_provider"] == "cohere"
        assert qm["rerank_time_ms"] == 20
