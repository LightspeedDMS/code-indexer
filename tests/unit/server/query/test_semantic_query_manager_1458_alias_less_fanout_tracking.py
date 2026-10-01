"""Alias-less (fan-out) queries must be refcount-tracked too (Story #1458
Codex HIGH finding, round 4).

Codex's finding: track_activated_repo_query() no-ops when repository_alias
is absent -- but alias-less requests still fan out across EVERY activated
repository and execute real per-repo searches (_perform_search's loop over
user_repos -> _search_single_repository). Those in-flight per-repo reads
were therefore completely invisible to deactivation's SAME-WORKER-PROCESS
bounded drain, which could purge an activated repo's chunks.db mid-query
even though a real read was in flight -- this is a same-process gap
DISTINCT from the already-accepted cross-worker limitation (issue #1475).

Fix scope: track EACH physical activated repo actually searched during the
fan-out, keyed by the per-repository identity resolved during execution
(repo_info["user_alias"]) -- NOT the (possibly absent) request-level
repository_alias.

Testing boundary: matches test_semantic_query_manager_1458_activation_id.py
(same story) exactly -- the REAL _perform_search -> _search_single_repository
dispatch runs end-to-end; only the genuinely EXTERNAL
SemanticSearchService.search_repository_path boundary (a separate class in
a separate module, needing a real indexed collection to run to completion)
is mocked, isolating "is the fan-out loop's tracking wrapper held during
each repo's search" from "does the full embedding+HNSW pipeline work"
(proven elsewhere).
"""

from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.models.api_models import SemanticSearchResponse
from code_indexer.server.query.semantic_query_manager import SemanticQueryManager

_TEST_QUERY_LIMIT = 10


class TestAliasLessFanOutQueryTracking:
    def test_each_physical_repo_is_refcount_tracked_during_its_own_search(
        self, tmp_path: Path
    ) -> None:
        real_query_tracker = QueryTracker()
        activated_repos_dir = str(tmp_path / "activated-repos")

        activated_repo_manager = MagicMock()
        activated_repo_manager.activated_repos_dir = activated_repos_dir
        activated_repo_manager.get_activated_repo_path.side_effect = (
            lambda username, alias: str(Path(activated_repos_dir) / username / alias)
        )
        activated_repo_manager.get_activation_id.return_value = "activation-1"

        manager = SemanticQueryManager(
            data_dir=str(tmp_path),
            activated_repo_manager=activated_repo_manager,
            background_job_manager=MagicMock(),
        )
        manager.set_query_tracker(real_query_tracker)

        user_repos = [
            {"user_alias": "repo-one", "username": "alice"},
            {"user_alias": "repo-two", "username": "alice"},
        ]

        observed: Dict[str, Any] = {}

        def fake_search_repository_path(self, *, repo_path, **kwargs):
            repository_alias = Path(repo_path).name
            expected_key = f"{activated_repos_dir}/alice/{repository_alias}"
            observed[repository_alias] = real_query_tracker.get_ref_count(expected_key)
            return SemanticSearchResponse(query="find auth", results=[], total=0)

        with patch(
            "code_indexer.server.services.search_service.SemanticSearchService.search_repository_path",
            fake_search_repository_path,
        ):
            manager._perform_search(
                "alice",
                user_repos,
                "find auth",
                _TEST_QUERY_LIMIT,
                None,
                None,
                query_strategy="primary_only",
            )

        # RED-phase expectation (this is the desired POST-FIX behavior --
        # currently fails because the fan-out loop does not track at all):
        # refcount was >0 WHILE each repo's own search executed...
        assert observed == {"repo-one": 1, "repo-two": 1}, (
            "Bug: alias-less fan-out queries were NOT refcount-tracked "
            "during their own per-repo search -- deactivation's drain "
            "would observe zero in-flight queries for a repo that is "
            "actually being read."
        )
        # ...and released after each repo's search completes.
        assert (
            real_query_tracker.get_ref_count(f"{activated_repos_dir}/alice/repo-one")
            == 0
        )
        assert (
            real_query_tracker.get_ref_count(f"{activated_repos_dir}/alice/repo-two")
            == 0
        )
