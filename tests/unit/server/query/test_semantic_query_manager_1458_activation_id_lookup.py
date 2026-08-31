"""query_user_repositories() computes activation_id for the ACTIVATED-repo
branch (Story #1458 AC11) via ActivatedRepoManager.get_activation_id(), and
passes None for golden/-global repos (which have no activation_id concept).

Real SemanticQueryManager.query_user_repositories() -> _search_single_
repository() dispatch chain runs for real; only the external collaborator
SemanticSearchService.search_repository_path() is monkeypatched (the SAME
established pattern already used successfully in
test_semantic_query_manager_1458_activation_id.py), to isolate "was
activation_id computed and threaded correctly through the real dispatch
loop" from "does the full embedding+HNSW pipeline work" (covered
elsewhere).
"""

from unittest.mock import MagicMock, patch

from code_indexer.server.models.api_models import SemanticSearchResponse
from code_indexer.server.query.semantic_query_manager import SemanticQueryManager


class TestQueryUserRepositoriesActivationIdLookup:
    def test_activated_repo_branch_looks_up_and_forwards_activation_id(self):
        activated_repo_manager = MagicMock()
        activated_repo_manager.list_activated_repositories.return_value = [
            {
                "user_alias": "my-repo",
                "golden_repo_alias": "golden-x",
                # No "is_global" key, no "repo_path" key -> the plain
                # activated-repo else-branch.
            }
        ]
        activated_repo_manager.get_activated_repo_path.return_value = (
            "/activated/testuser/my-repo"
        )
        activated_repo_manager.get_activation_id.return_value = (
            "looked-up-activation-id"
        )

        manager = SemanticQueryManager(
            activated_repo_manager=activated_repo_manager,
            background_job_manager=MagicMock(),
        )

        captured_kwargs = {}

        def fake_search_repository_path(self, **kwargs):
            captured_kwargs.update(kwargs)
            return SemanticSearchResponse(query="q", results=[], total=0)

        with patch(
            "code_indexer.server.services.search_service.SemanticSearchService.search_repository_path",
            fake_search_repository_path,
        ):
            manager.query_user_repositories(
                username="testuser",
                query_text="test",
                limit=5,
                query_strategy="primary_only",
            )

        activated_repo_manager.get_activation_id.assert_called_once_with(
            "testuser", "my-repo"
        )
        assert captured_kwargs.get("activation_id") == "looked-up-activation-id"

    def test_repo_path_provided_branch_passes_none_activation_id(self):
        activated_repo_manager = MagicMock()
        activated_repo_manager.list_activated_repositories.return_value = [
            {
                "user_alias": "direct-path-repo",
                "repo_path": "/some/direct/path",
            }
        ]

        manager = SemanticQueryManager(
            activated_repo_manager=activated_repo_manager,
            background_job_manager=MagicMock(),
        )

        captured_kwargs = {}

        def fake_search_repository_path(self, **kwargs):
            captured_kwargs.update(kwargs)
            return SemanticSearchResponse(query="q", results=[], total=0)

        with patch(
            "code_indexer.server.services.search_service.SemanticSearchService.search_repository_path",
            fake_search_repository_path,
        ):
            manager.query_user_repositories(
                username="testuser",
                query_text="test",
                limit=5,
                query_strategy="primary_only",
            )

        activated_repo_manager.get_activation_id.assert_not_called()
        assert captured_kwargs.get("activation_id") is None


class TestActivationIdMissingLookupBypassesSharedCache:
    """Codex round-6 HIGH finding #9: the migration adding activation_id
    (039) is correctly additive/nullable, but a NULL activation_id
    returned by a genuine activated-repo lookup (e.g. an activation
    created by an OLD node during a rolling upgrade, before activation_id
    generation existed) was previously forwarded as bare None -- and
    FilesystemVectorStore._activation_scoped_cache_key() treats None as
    'no activation_id component', so TWO DIFFERENT activations at the
    SAME path with NULL activation_id derive the IDENTICAL cache key.
    Fix: a reader must bypass/skip the shared cache entirely (a unique,
    never-repeating per-call token) for an activated repo whose
    activation_id is missing, rather than treating NULL as a valid
    (collision-prone) key component."""

    def _run_query_and_capture_activation_id(self, activated_repo_manager) -> object:
        manager = SemanticQueryManager(
            activated_repo_manager=activated_repo_manager,
            background_job_manager=MagicMock(),
        )
        captured_kwargs = {}

        def fake_search_repository_path(self, **kwargs):
            captured_kwargs.update(kwargs)
            return SemanticSearchResponse(query="q", results=[], total=0)

        with patch(
            "code_indexer.server.services.search_service.SemanticSearchService.search_repository_path",
            fake_search_repository_path,
        ):
            manager.query_user_repositories(
                username="testuser",
                query_text="test",
                limit=5,
                query_strategy="primary_only",
            )
        return captured_kwargs.get("activation_id")

    def test_none_activation_id_from_a_real_lookup_is_never_forwarded_bare(
        self,
    ) -> None:
        activated_repo_manager = MagicMock()
        activated_repo_manager.list_activated_repositories.return_value = [
            {"user_alias": "my-repo", "golden_repo_alias": "golden-x"}
        ]
        activated_repo_manager.get_activated_repo_path.return_value = (
            "/activated/testuser/my-repo"
        )
        # The rolling-upgrade edge case: a genuine activated-repo lookup
        # that resolves to NULL.
        activated_repo_manager.get_activation_id.return_value = None

        forwarded = self._run_query_and_capture_activation_id(activated_repo_manager)

        assert forwarded is not None, (
            "Bug: a NULL activation_id from a real activated-repo lookup "
            "was forwarded as bare None -- FilesystemVectorStore's cache "
            "key would treat this identically to 'no activation_id "
            "concept', causing a cross-activation cache collision with "
            "any OTHER activation that also has a NULL activation_id at "
            "the same path."
        )

    def test_two_separate_queries_with_missing_activation_id_get_distinct_bypass_tokens(
        self,
    ) -> None:
        """A FIXED placeholder string (e.g. always 'MISSING') would
        still collide between two different NULL-activation_id
        activations -- the bypass token must be unique PER QUERY."""
        activated_repo_manager = MagicMock()
        activated_repo_manager.list_activated_repositories.return_value = [
            {"user_alias": "my-repo", "golden_repo_alias": "golden-x"}
        ]
        activated_repo_manager.get_activated_repo_path.return_value = (
            "/activated/testuser/my-repo"
        )
        activated_repo_manager.get_activation_id.return_value = None

        first = self._run_query_and_capture_activation_id(activated_repo_manager)
        second = self._run_query_and_capture_activation_id(activated_repo_manager)

        assert first != second, (
            "Bug: two separate queries against a NULL-activation_id "
            "activation received the SAME bypass token -- a fixed "
            "placeholder still collides between two genuinely different "
            "NULL-activation_id activations at the same path."
        )
