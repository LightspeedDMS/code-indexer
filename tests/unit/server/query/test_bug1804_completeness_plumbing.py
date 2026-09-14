"""Bug #1804 -- the `_provider_completeness_out` marker set by
`_search_single_repository`'s total-failure guards (see
test_bug1804_provider_completeness_marker.py) must survive being threaded
through `_perform_search`'s per-repo loop (which re-raises a brand-new plain
`Exception` from `repo_errors[-1]`) and through `query_user_repositories`
(which re-wraps into a brand-new `SemanticQueryError`). Both call sites must
accept and forward the out-param to `_search_single_repository` so the
caller's own dict object -- passed in before the call -- is populated by the
time the (still-raised, Bug #1760 contract unchanged) exception is caught.
"""

import logging
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from code_indexer.server.query.semantic_query_manager import SemanticQueryManager


@pytest.fixture
def manager():
    m = SemanticQueryManager.__new__(SemanticQueryManager)
    m.data_dir = "/fake/data"
    m.query_timeout_seconds = 30
    m.max_concurrent_queries_per_user = 5
    m.max_results_per_query = 100
    m._active_queries_per_user = {}
    m.logger = logging.getLogger(__name__)
    mock_arm = MagicMock()
    mock_arm.activated_repos_dir = "/fake/data/activated_repos"
    m.activated_repo_manager = mock_arm
    m.background_job_manager = MagicMock()
    m.query_tracker = None
    return m


def _install_marker_on_search_single_repository(monkeypatch, manager) -> None:
    """Stand in for `_search_single_repository` so this test proves the
    PLUMBING (out-param forwarded through `_perform_search`/
    `query_user_repositories`) independent of the real dispatch/health-
    monitor logic already covered by test_bug1804_provider_completeness_marker.py.
    """

    def _fake_search_single_repository(*args, **kwargs):
        completeness_out = kwargs.get("_provider_completeness_out")
        assert completeness_out is not None, (
            "_perform_search must forward _provider_completeness_out into "
            "_search_single_repository"
        )
        completeness_out["completeness"] = "providers_unavailable"
        completeness_out["provider_errors"] = {
            "voyage-ai": "503 Service Unavailable",
            "cohere": "503 Service Unavailable",
        }
        raise RuntimeError("every dispatched embedding provider failed")

    monkeypatch.setattr(
        manager, "_search_single_repository", _fake_search_single_repository
    )


class TestPerformSearchForwardsCompletenessOutParam:
    def test_perform_search_forwards_marker_to_caller_dict(self, manager, monkeypatch):
        _install_marker_on_search_single_repository(monkeypatch, manager)
        completeness_out: Dict[str, Any] = {}

        with pytest.raises(Exception):
            manager._perform_search(
                username="testuser",
                user_repos=[{"user_alias": "myrepo", "repo_path": "/fake/repo"}],
                query_text="authentication",
                limit=10,
                min_score=None,
                file_extensions=None,
                _provider_completeness_out=completeness_out,
            )

        assert completeness_out.get("completeness") == "providers_unavailable"
        assert "voyage-ai" in completeness_out.get("provider_errors", {})


class TestQueryUserRepositoriesForwardsCompletenessOutParam:
    def test_query_user_repositories_forwards_marker_to_caller_dict(
        self, manager, monkeypatch
    ):
        _install_marker_on_search_single_repository(monkeypatch, manager)
        monkeypatch.setattr(
            manager.activated_repo_manager,
            "list_activated_repositories",
            lambda username: [{"user_alias": "myrepo", "repo_path": "/fake/repo"}],
        )
        completeness_out: Dict[str, Any] = {}

        with pytest.raises(Exception):
            manager.query_user_repositories(
                username="testuser",
                query_text="authentication",
                repository_alias="myrepo",
                _provider_completeness_out=completeness_out,
            )

        assert completeness_out.get("completeness") == "providers_unavailable"
        assert "cohere" in completeness_out.get("provider_errors", {})
