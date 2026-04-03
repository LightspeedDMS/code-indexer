"""Regression guard: MCP search_code handler must default min_score to 0.3.

Bug: Cohere embed-v4.0 produces cosine similarity scores in the ~0.42-0.48
range for typical queries. The previous default of 0.5 silently eliminated
all Cohere results when the caller did not supply an explicit min_score.

Tests invoke the actual search_code handler, patch only the downstream
SemanticQueryManager, and assert that _perform_search receives min_score=0.3
when the caller omits min_score from params.
"""

import os
import shutil
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


from code_indexer.server.query.semantic_query_manager import QueryResult


def _make_fake_user():
    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username="testuser",
        role=UserRole.ADMIN,
        email="test@example.com",
        password_hash="fakehash",
        created_at=datetime.now(timezone.utc),
    )


def _fake_result():
    return QueryResult(
        file_path="src/auth.py",
        line_number=1,
        code_snippet="def auth(): pass",
        similarity_score=0.45,
        repository_alias="test-global",
        source_provider="cohere",
    )


def _invoke_search_code(params, user, mock_manager, base_dir):
    """Invoke search_code with all infrastructure patched out."""
    from code_indexer.server.mcp import handlers as h

    repo_dir = os.path.join(base_dir, "repo")
    os.makedirs(repo_dir, exist_ok=True)
    golden_dir = os.path.join(base_dir, "golden")
    os.makedirs(golden_dir, exist_ok=True)

    mock_alias_manager = MagicMock()
    mock_alias_manager.read_alias.return_value = repo_dir
    mock_repo_entry = {"alias_name": "test-global", "repo_name": "test"}

    with patch.object(h, "_list_global_repos", return_value=[mock_repo_entry]):
        with patch.object(h, "_get_golden_repos_dir", return_value=golden_dir):
            with patch(
                "code_indexer.global_repos.alias_manager.AliasManager",
                return_value=mock_alias_manager,
            ):
                with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
                    mock_app.semantic_query_manager = mock_manager
                    with patch.object(h, "_get_query_tracker", return_value=None):
                        with patch.object(
                            h, "_get_access_filtering_service", return_value=None
                        ):
                            with patch.object(
                                h, "_apply_payload_truncation", side_effect=lambda x: x
                            ):
                                with patch.object(
                                    h, "_is_temporal_query", return_value=False
                                ):
                                    with patch.object(
                                        h, "_get_wiki_enabled_repos", return_value=set()
                                    ):
                                        return h.search_code(params, user)


class TestMinScoreDefault:
    """search_code must default min_score to 0.3 when caller omits it."""

    def test_omitted_min_score_defaults_to_0_3(self):
        """When min_score is absent from params, _perform_search receives 0.3."""
        user = _make_fake_user()
        mock_manager = MagicMock()
        mock_manager._perform_search.return_value = [_fake_result()]

        params = {
            "query_text": "authentication",
            "repository_alias": "test-global",
        }

        base_dir = tempfile.mkdtemp()
        try:
            _invoke_search_code(params, user, mock_manager, base_dir)
        finally:
            shutil.rmtree(base_dir, ignore_errors=True)

        assert mock_manager._perform_search.called
        actual = mock_manager._perform_search.call_args.kwargs.get("min_score")
        assert actual == 0.3, (
            f"Expected min_score=0.3 (Cohere-compatible default), got {actual}. "
            "A default of 0.5 silently eliminates all Cohere embed-v4.0 results."
        )

    def test_explicit_min_score_overrides_default(self):
        """When caller provides min_score=0.1, that value reaches _perform_search."""
        user = _make_fake_user()
        mock_manager = MagicMock()
        mock_manager._perform_search.return_value = [_fake_result()]

        params = {
            "query_text": "authentication",
            "repository_alias": "test-global",
            "min_score": "0.1",
        }

        base_dir = tempfile.mkdtemp()
        try:
            _invoke_search_code(params, user, mock_manager, base_dir)
        finally:
            shutil.rmtree(base_dir, ignore_errors=True)

        assert mock_manager._perform_search.called
        actual = mock_manager._perform_search.call_args.kwargs.get("min_score")
        assert actual == 0.1, (
            f"Explicit min_score=0.1 must reach _perform_search, got {actual}"
        )

    def test_default_is_not_0_5(self):
        """Regression: 0.5 must not be the fallback — it kills all Cohere results."""
        user = _make_fake_user()
        mock_manager = MagicMock()
        mock_manager._perform_search.return_value = [_fake_result()]

        params = {
            "query_text": "authentication",
            "repository_alias": "test-global",
        }

        base_dir = tempfile.mkdtemp()
        try:
            _invoke_search_code(params, user, mock_manager, base_dir)
        finally:
            shutil.rmtree(base_dir, ignore_errors=True)

        actual = mock_manager._perform_search.call_args.kwargs.get("min_score")
        assert actual != 0.5, (
            "min_score defaulted to 0.5 — this eliminates all Cohere embed-v4.0 "
            "results (max score ~0.479). Default must be 0.3."
        )
