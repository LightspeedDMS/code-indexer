"""Bug #1767 -- a multi-repo query where ONE repo returns real results and
ANOTHER repo hard-fails must surface the partial failure to the caller,
not silently return success:true with no indication a repo was skipped.

Root cause: `SemanticQueryManager._perform_search`'s per-repo fan-out loop
only raises when EVERY repo failed (`if not all_results and repo_errors:
raise`). A partial failure (one repo succeeds, another hard-fails) is
recorded into the local `repo_errors` list, logged as a per-repo WARNING
([QUERY-MIGRATE-006]), and then silently dropped -- `query_user_repositories`
builds `{"results": [...], "total_results": N, "query_metadata": {...}}`
with no field anywhere indicating one of the queried repos contributed zero
results due to a real error rather than a genuine empty match.

This is the same masking class as Bug #1760's per-provider-dispatch fix
(commit 07202d16), one layer up: the per-repo fan-out aggregation in
`_perform_search`, not the per-repo provider-dispatch guard #1760 added
inside `_search_single_repository`.

Testing boundary: matches
`test_semantic_query_manager_1458_alias_less_fanout_tracking.py` (same
class of test, same module) exactly -- the REAL `_perform_search` ->
`_search_single_repository` dispatch runs end-to-end; only the genuinely
EXTERNAL `SemanticSearchService.search_repository_path` boundary (a
separate class in a separate module, which would otherwise need a real
indexed collection to run to completion) is faked. This isolates "does
`_perform_search`'s fan-out loop correctly aggregate/degrade across repos"
from "does the embedding+HNSW pipeline itself work" (proven elsewhere).

Fix contract (tested here):
1. A partial failure (one repo succeeds, one hard-fails) must NOT raise --
   existing graceful degradation for genuine multi-repo queries is
   preserved (results from the healthy repo are still returned).
2. The response's `query_metadata` must include a `degraded_repos` field
   listing the alias(es) of the repo(s) that failed, when at least one
   repo failed while at least one other succeeded.
3. A dedicated WARNING-level log ([QUERY-MIGRATE-013]) fires for the
   aggregate partial-failure event, distinct from the existing per-repo
   [QUERY-MIGRATE-006] log, and names the failed repo(s).
4. The all-repos-healthy case (no failures at all) is completely
   unaffected: `degraded_repos` must NOT appear in `query_metadata` at all
   (additive/backward-compatible -- existing consumers see no new key).
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.models.api_models import (
    SemanticSearchResponse,
    SearchResultItem,
)
from code_indexer.server.query.semantic_query_manager import SemanticQueryManager

_QUERY_TEXT = "find main function"
_QUERY_LIMIT = 10


@pytest.fixture
def activated_repos_dir(tmp_path):
    return str(tmp_path / "activated-repos")


@pytest.fixture
def activated_repo_manager_mock(activated_repos_dir):
    mock = MagicMock()
    mock.activated_repos_dir = activated_repos_dir
    mock.list_activated_repositories.return_value = [
        {
            "user_alias": "healthy-repo",
            "golden_repo_alias": "healthy-golden",
            "current_branch": "main",
            "activated_at": "2024-01-01T00:00:00Z",
            "last_accessed": "2024-01-01T00:00:00Z",
        },
        {
            "user_alias": "broken-repo",
            "golden_repo_alias": "broken-golden",
            "current_branch": "main",
            "activated_at": "2024-01-01T00:00:00Z",
            "last_accessed": "2024-01-01T00:00:00Z",
        },
    ]
    mock.get_activated_repo_path.side_effect = lambda username, user_alias: str(
        Path(activated_repos_dir) / username / user_alias
    )
    mock.get_activation_id.return_value = "activation-1"
    return mock


@pytest.fixture
def manager(tmp_path, activated_repo_manager_mock):
    return SemanticQueryManager(
        data_dir=str(tmp_path),
        activated_repo_manager=activated_repo_manager_mock,
        background_job_manager=MagicMock(),
    )


def _fake_search_repository_path(broken_alias):
    """Real `SemanticSearchService.search_repository_path`-compatible
    side_effect: raises for the repo whose path ends in `broken_alias`,
    returns one genuine result for every other repo. `broken_alias=None`
    makes every repo healthy."""

    def _search(self, *, repo_path, **kwargs):
        alias = Path(repo_path).name
        if alias == broken_alias:
            raise Exception("chunks.db: attempt to write a readonly database")
        return SemanticSearchResponse(
            query=_QUERY_TEXT,
            total=1,
            results=[
                SearchResultItem(
                    score=0.9,
                    file_path=f"{repo_path}/src/main.py",
                    line_start=1,
                    line_end=1,
                    content="def main():\n    pass",
                    language=None,
                    file_last_modified=None,
                    indexed_timestamp=None,
                )
            ],
        )

    return _search


def _run_query(manager, broken_alias, **kwargs):
    with patch(
        "code_indexer.server.services.search_service.SemanticSearchService.search_repository_path",
        _fake_search_repository_path(broken_alias),
    ):
        return manager.query_user_repositories(
            username="alice",
            query_text=_QUERY_TEXT,
            limit=_QUERY_LIMIT,
            query_strategy="primary_only",
            **kwargs,
        )


class TestBug1767PartialFailureDegradation:
    """Graceful-degradation and response-shape assertions."""

    def test_partial_repo_failure_does_not_raise_and_returns_healthy_results(
        self, manager
    ):
        """One repo hard-failing must not blow up the whole multi-repo
        query -- the healthy repo's real results are still returned."""
        response = _run_query(manager, broken_alias="broken-repo")

        assert len(response["results"]) == 1
        assert response["results"][0]["repository_alias"] == "healthy-repo"

    def test_partial_repo_failure_surfaces_degraded_repos_in_metadata(self, manager):
        """The defect: query_metadata must carry a degraded_repos indicator
        naming the repo(s) that failed, so the partial failure is visible
        to the caller instead of silently dropped."""
        response = _run_query(manager, broken_alias="broken-repo")

        metadata = response["query_metadata"]
        assert "degraded_repos" in metadata, (
            "query_metadata must surface a degraded_repos field when one repo "
            "hard-fails while another succeeds -- currently this partial "
            "failure is silently dropped (Bug #1767)"
        )
        assert metadata["degraded_repos"] == ["broken-repo"]


class TestBug1767PartialFailureLoggingAndBackwardCompat:
    """Logging behavior and the healthy-case backward-compatibility guard."""

    def test_partial_repo_failure_logs_dedicated_aggregate_warning(
        self, manager, caplog
    ):
        """A dedicated [QUERY-MIGRATE-013] WARNING must fire for the
        aggregate partial-failure event -- distinct from the pre-existing
        per-repo [QUERY-MIGRATE-006] log -- and must name the failed
        repo(s)."""
        with caplog.at_level(logging.WARNING):
            _run_query(manager, broken_alias="broken-repo")

        matching = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "QUERY-MIGRATE-013" in record.message
        ]
        assert matching, (
            "expected a dedicated [QUERY-MIGRATE-013] aggregate WARNING for "
            "the partial multi-repo failure, distinct from the per-repo "
            "[QUERY-MIGRATE-006] log"
        )
        assert any("broken-repo" in record.message for record in matching), (
            "the aggregate WARNING must name the repo(s) that failed"
        )

    def test_all_repos_healthy_case_has_no_degraded_repos_key(self, manager):
        """Backward compatibility: when every repo succeeds, degraded_repos
        must not appear in query_metadata at all (purely additive fix)."""
        response = _run_query(manager, broken_alias=None)

        assert len(response["results"]) == 2
        assert "degraded_repos" not in response["query_metadata"]
