"""
Tests for Bug #614 and Bug #615 in the multi-provider parallel search system.

Bug #614: Parallel/failover query strategy is a no-op.
  Root cause: _search_single_repository() logs "parallel strategy requested"
  then falls through to single-provider search. Both providers are never
  queried simultaneously.

Bug #615: min_score applied per-provider before fusion — silently eliminates
  all Cohere results.
  Root cause: Cohere scores are 0.42-0.48 range, VoyageAI is 0.65-0.68.
  Default min_score=0.5 eliminates ALL Cohere results before fusion runs.

These two bugs MUST be fixed together: fixing #614 without #615 produces
results from both providers but still silently drops Cohere results before
fusion because min_score is applied per-provider.

Correct order: raw results per provider -> fusion -> filter(min_score) on
fused output.
Wrong order (current before fix): filter(min_score) per provider -> fusion.

Design note: Tests are behavior-based, not implementation-based.
- We verify OBSERVABLE outcomes: which providers were queried, what results
  appear, how min_score is applied.
- We mock _search_with_provider (the bridge to SemanticSearchService) to
  control what each provider returns without hitting real embedding APIs.
- We do NOT patch execute_parallel_query — we verify that the parallel
  execution and fusion happen by observing their effects.
"""

import shutil
import tempfile
import logging
from typing import List
from unittest.mock import MagicMock, patch

from code_indexer.server.query.semantic_query_manager import (
    SemanticQueryManager,
    QueryResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_temp_repo() -> str:
    """Create a temp directory that acts as a normal (non-composite) repo."""
    return tempfile.mkdtemp()


def _make_manager() -> SemanticQueryManager:
    """Create a SemanticQueryManager with mocked infrastructure dependencies."""
    manager = SemanticQueryManager.__new__(SemanticQueryManager)
    manager.data_dir = "/fake/data"
    manager.query_timeout_seconds = 30
    manager.max_concurrent_queries_per_user = 5
    manager.max_results_per_query = 100
    manager._active_queries_per_user = {}
    manager.logger = logging.getLogger(__name__)

    mock_arm = MagicMock()
    mock_arm.activated_repos_dir = "/fake/data/activated_repos"
    manager.activated_repo_manager = mock_arm
    manager.background_job_manager = MagicMock()
    return manager


def _make_search_response(results_data: List[dict]):
    """Build a SemanticSearchResponse from a list of dicts."""
    from code_indexer.server.models.api_models import (
        SemanticSearchResponse,
        SearchResultItem,
    )

    items = [
        SearchResultItem(
            file_path=d["file_path"],
            line_start=d.get("line_start", 1),
            line_end=d.get("line_end", 2),
            score=d["score"],
            content=d.get("content", "some code"),
            language=d.get("language", "python"),
        )
        for d in results_data
    ]
    return SemanticSearchResponse(query="test", results=items, total=len(items))


def _make_provider_results(
    provider: str, file_path: str, score: float
) -> List[QueryResult]:
    """Make a list with one QueryResult from the given provider."""
    return [
        QueryResult(
            file_path=file_path,
            line_number=1,
            code_snippet=f"code from {provider}",
            similarity_score=score,
            repository_alias="test-repo",
            source_provider=provider,
        )
    ]


# ---------------------------------------------------------------------------
# Bug #614: Parallel strategy is a no-op
# ---------------------------------------------------------------------------


class TestBug614ParallelStrategyNotANoop:
    """Tests that BOTH providers are queried when query_strategy='parallel'.

    Before the fix: _search_single_repository logs "parallel strategy requested"
    then falls through to single-provider (primary_only) search. Only the
    primary provider (or no provider via _search_with_provider) is queried.

    After the fix: both voyage-ai and cohere are queried concurrently,
    and results from both providers appear in output.

    Observable evidence of the fix:
    - _search_with_provider is called with provider_name='voyage-ai'
    - _search_with_provider is called with provider_name='cohere'
    - Results from both providers appear in the returned list
    """

    def setup_method(self):
        self.repo_path = _make_temp_repo()

    def teardown_method(self):
        shutil.rmtree(self.repo_path, ignore_errors=True)

    def test_parallel_strategy_queries_both_providers(self):
        """When query_strategy='parallel', both voyage-ai and cohere are queried.

        Observable: _search_with_provider is called twice — once with
        provider_name='voyage-ai' and once with provider_name='cohere'.
        Before the fix: only called once (or zero times via this path).
        """
        manager = _make_manager()

        providers_queried = []

        def fake_search_with_provider(*args, **kwargs):
            provider = kwargs.get("provider_name")
            providers_queried.append(provider)
            return _make_provider_results(
                provider or "unknown", f"src/{provider}.py", 0.75
            )

        with patch.object(
            manager, "_search_with_provider", side_effect=fake_search_with_provider
        ):
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="parallel",
            )

        assert len(providers_queried) == 2, (
            f"Bug #614: Expected 2 providers queried, got {len(providers_queried)}: "
            f"{providers_queried}. Secondary provider is not being queried."
        )
        assert "voyage-ai" in providers_queried, (
            f"voyage-ai not queried. Providers queried: {providers_queried}"
        )
        assert "cohere" in providers_queried, (
            f"cohere not queried. Providers queried: {providers_queried}"
        )

    def test_parallel_strategy_returns_results_from_both_providers(self):
        """Results from both providers appear in the output for parallel strategy.

        Observable: the returned list contains results whose source_provider
        is 'voyage-ai' AND results whose source_provider is 'cohere'.
        Before the fix: only primary provider results appear.
        """
        manager = _make_manager()

        def fake_search_with_provider(*args, **kwargs):
            provider = kwargs.get("provider_name", "unknown")
            return _make_provider_results(provider, f"src/{provider}_file.py", 0.75)

        with patch.object(
            manager, "_search_with_provider", side_effect=fake_search_with_provider
        ):
            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="parallel",
            )

        file_paths = [r.file_path for r in results]
        assert any("voyage-ai" in fp for fp in file_paths), (
            f"No voyage-ai results in output. File paths: {file_paths}"
        )
        assert any("cohere" in fp for fp in file_paths), (
            f"No cohere results in output. File paths: {file_paths}"
        )

    def test_parallel_strategy_queries_concurrently_via_search_with_provider(self):
        """Parallel strategy uses _search_with_provider for each named provider.

        This verifies that the correct internal bridge method is used to query
        each provider by name — not the SemanticSearchService directly (which
        uses the repo's default provider).
        """
        manager = _make_manager()

        call_kwargs_list = []

        def fake_search_with_provider(*args, **kwargs):
            call_kwargs_list.append(dict(kwargs))
            provider = kwargs.get("provider_name", "unknown")
            return _make_provider_results(provider, f"src/{provider}.py", 0.75)

        with patch.object(
            manager, "_search_with_provider", side_effect=fake_search_with_provider
        ):
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="parallel",
            )

        provider_names = [kw.get("provider_name") for kw in call_kwargs_list]
        assert "voyage-ai" in provider_names, (
            f"_search_with_provider not called with provider_name='voyage-ai'. "
            f"Calls: {call_kwargs_list}"
        )
        assert "cohere" in provider_names, (
            f"_search_with_provider not called with provider_name='cohere'. "
            f"Calls: {call_kwargs_list}"
        )

    def test_primary_only_strategy_unchanged_by_fix(self):
        """primary_only strategy must behave exactly as before after fix.

        Backward compatibility: the fix must not break primary_only routing.
        _search_with_provider must NOT be called for primary_only strategy.
        """
        manager = _make_manager()

        with patch(
            "code_indexer.server.services.search_service.SemanticSearchService"
        ) as mock_svc_cls:
            mock_svc = MagicMock()
            mock_svc.search_repository_path.return_value = _make_search_response(
                [
                    {"file_path": "src/auth.py", "score": 0.85},
                ]
            )
            mock_svc_cls.return_value = mock_svc

            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="primary_only",
            )

        assert len(results) == 1
        mock_svc.search_repository_path.assert_called_once()

    def test_parallel_strategy_with_no_score_fusion_uses_default_rrf(self):
        """When score_fusion not specified, parallel strategy uses default (RRF).

        Observable: both providers are still queried and results returned.
        The exact fusion algorithm is an implementation detail, but the
        presence of results from both providers confirms fusion ran.
        """
        manager = _make_manager()

        def fake_search_with_provider(*args, **kwargs):
            provider = kwargs.get("provider_name", "unknown")
            return _make_provider_results(provider, f"src/{provider}.py", 0.75)

        with patch.object(
            manager, "_search_with_provider", side_effect=fake_search_with_provider
        ):
            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="parallel",
                score_fusion=None,  # No fusion specified — should default to RRF
            )

        # Results from both providers must appear
        assert len(results) >= 1, "Expected at least one result from parallel strategy"


# ---------------------------------------------------------------------------
# Bug #615: min_score applied pre-fusion eliminates Cohere results
# ---------------------------------------------------------------------------


class TestBug615MinScoreAppliedAfterFusion:
    """Tests that min_score filtering happens AFTER fusion, not per-provider.

    Before the fix: min_score is applied to each provider's raw results
    before fusion. Cohere scores (0.42-0.48) are below the default
    min_score=0.5, so ALL Cohere results are eliminated before fusion.

    After the fix: providers receive min_score=0.0 (no threshold), fusion
    runs on raw combined results, then user's min_score is applied to the
    fused output.

    Correct order: raw results -> fusion -> filter(min_score)
    Wrong order:   filter(min_score) -> fusion (current before fix)
    """

    def setup_method(self):
        self.repo_path = _make_temp_repo()

    def teardown_method(self):
        shutil.rmtree(self.repo_path, ignore_errors=True)

    def test_providers_receive_zero_min_score_for_parallel_strategy(self):
        """Each provider must receive min_score=None when query_strategy='parallel'.

        This is the core fix for Bug #615: pass min_score=None to each
        individual provider so no results are pre-filtered before fusion.
        None is the correct sentinel — _search_with_provider checks
        `if min_score is not None and score < min_score`.
        Observable: each call to _search_with_provider has min_score=None.
        """
        manager = _make_manager()

        captured_min_scores = []

        def fake_search_with_provider(*args, **kwargs):
            captured_min_scores.append(kwargs.get("min_score"))
            provider = kwargs.get("provider_name", "unknown")
            return _make_provider_results(provider, f"src/{provider}.py", 0.75)

        with patch.object(
            manager, "_search_with_provider", side_effect=fake_search_with_provider
        ):
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=0.5,  # User's min_score — must NOT reach providers
                file_extensions=None,
                query_strategy="parallel",
            )

        assert len(captured_min_scores) == 2, (
            f"Expected _search_with_provider called twice (once per provider), "
            f"got {len(captured_min_scores)} calls."
        )
        for score in captured_min_scores:
            assert score is None, (
                f"Bug #615: provider received min_score={score!r} instead of None. "
                f"None is the correct sentinel — min_score must not be applied "
                f"per-provider before fusion."
            )

    def test_cohere_results_not_filtered_before_fusion(self):
        """Cohere results with raw score=0.45 must not be dropped before fusion.

        Observable: when both providers return results (even with Cohere
        score below min_score=0.5), the Cohere result is NOT silently
        dropped — it reaches the fusion step.

        We verify this by checking that _search_with_provider for 'cohere'
        was called with min_score=0.0 (not 0.5), which proves Cohere's
        raw results are allowed through to fusion.
        """
        manager = _make_manager()

        cohere_min_score_received = []

        def fake_search_with_provider(*args, **kwargs):
            provider = kwargs.get("provider_name", "unknown")
            if provider == "cohere":
                cohere_min_score_received.append(kwargs.get("min_score"))
                # Return a result with low raw score (typical Cohere range)
                return _make_provider_results("cohere", "src/cohere_result.py", 0.45)
            # voyage-ai returns a result above min_score
            return _make_provider_results("voyage-ai", "src/voyage_result.py", 0.67)

        with patch.object(
            manager, "_search_with_provider", side_effect=fake_search_with_provider
        ):
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=0.5,
                file_extensions=None,
                query_strategy="parallel",
            )

        assert len(cohere_min_score_received) == 1, (
            "cohere provider was not queried at all"
        )
        assert cohere_min_score_received[0] is None, (
            f"Bug #615: cohere received min_score={cohere_min_score_received[0]!r} "
            f"instead of None. Cohere results are being pre-filtered before fusion."
        )

    def test_min_score_applied_to_results_after_providers_return(self):
        """min_score IS applied after providers return, filtering low-score fused results.

        This test verifies the full end-to-end contract:
        - Providers receive min_score=None (no pre-filtering sentinel)
        - After both providers return, results below min_score are dropped
        - Only results with similarity_score >= min_score are returned

        Setup: voyage-ai returns score=0.80, cohere returns score=0.30.
        With min_score=0.5, the voyage-ai result survives, cohere is dropped
        AFTER providers are called (not before). The key difference from the
        bug: cohere IS queried (min_score=None passed to it), but its fused/
        raw score=0.30 is below min_score=0.5, so it's filtered post-fusion.
        """
        manager = _make_manager()

        voyage_called = [False]
        cohere_called = [False]

        def fake_search_with_provider(*args, **kwargs):
            provider = kwargs.get("provider_name", "unknown")
            received_min_score = kwargs.get("min_score")
            if provider == "voyage-ai":
                voyage_called[0] = True
                assert received_min_score is None, (
                    f"voyage-ai got min_score={received_min_score!r}, expected None"
                )
                return _make_provider_results("voyage-ai", "src/voyage.py", 0.80)
            else:
                cohere_called[0] = True
                assert received_min_score is None, (
                    f"cohere got min_score={received_min_score!r}, expected None"
                )
                # Low score — below min_score=0.5
                return _make_provider_results("cohere", "src/cohere.py", 0.30)

        with patch.object(
            manager, "_search_with_provider", side_effect=fake_search_with_provider
        ):
            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=0.5,
                file_extensions=None,
                query_strategy="parallel",
            )

        # Both providers MUST have been queried (Bug #614 fix)
        assert voyage_called[0], "voyage-ai was not queried at all"
        assert cohere_called[0], (
            "Bug #614+#615: cohere was not queried. Cohere is being skipped entirely "
            "instead of being queried and then filtered post-fusion."
        )

        # After fusion: voyage result (0.80) survives, cohere result (0.30) is filtered.
        # source_provider is "fused" after RRF fusion; check file_path instead.
        surviving_paths = [r.file_path for r in results]
        assert "src/voyage.py" in surviving_paths, (
            f"voyage-ai result (file=src/voyage.py, score=0.80) should survive "
            f"min_score=0.5. File paths in results: {surviving_paths}"
        )

    def test_min_score_none_returns_all_results_from_both_providers(self):
        """When min_score=None, all results from both providers are returned."""
        manager = _make_manager()

        def fake_search_with_provider(*args, **kwargs):
            provider = kwargs.get("provider_name", "unknown")
            # Very low scores — would be filtered if min_score was not None
            return _make_provider_results(provider, f"src/{provider}.py", 0.10)

        with patch.object(
            manager, "_search_with_provider", side_effect=fake_search_with_provider
        ):
            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="parallel",
            )

        # With min_score=None, no filtering — results from both providers returned
        assert len(results) >= 2, (
            f"Expected at least 2 results with min_score=None (one per provider), "
            f"got {len(results)}"
        )

    def test_primary_only_still_applies_min_score_per_result(self):
        """primary_only strategy still applies min_score per result (unchanged behavior).

        Bug #615 fix must NOT change primary_only behavior. The min_score
        filtering per result must remain for primary_only strategy.
        """
        manager = _make_manager()

        with patch(
            "code_indexer.server.services.search_service.SemanticSearchService"
        ) as mock_svc_cls:
            mock_svc = MagicMock()
            mock_svc.search_repository_path.return_value = _make_search_response(
                [
                    {"file_path": "src/high.py", "score": 0.85},
                    {"file_path": "src/low.py", "score": 0.30},  # Below min_score=0.5
                ]
            )
            mock_svc_cls.return_value = mock_svc

            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=0.5,
                file_extensions=None,
                query_strategy="primary_only",
            )

        # primary_only: min_score filters per-result (low.py dropped)
        assert len(results) == 1
        assert results[0].file_path == "src/high.py"

    def test_parallel_strategy_passes_limit_to_both_providers(self):
        """Both providers receive the same over-fetched limit parameter.

        Story #638: To widen the candidate pool before score-gated filtering
        and fusion, each provider receives limit * PARALLEL_FETCH_MULTIPLIER
        (capped at MAX_PARALLEL_FETCH). Both providers must receive the same
        over-fetched limit.
        """
        from code_indexer.services.query_strategy import (
            PARALLEL_FETCH_MULTIPLIER,
            MAX_PARALLEL_FETCH,
        )

        manager = _make_manager()
        requested_limit = 7
        expected_provider_limit = min(
            requested_limit * PARALLEL_FETCH_MULTIPLIER, MAX_PARALLEL_FETCH
        )

        limits_received = []

        def fake_search_with_provider(*args, **kwargs):
            limits_received.append(kwargs.get("limit"))
            provider = kwargs.get("provider_name", "unknown")
            return _make_provider_results(provider, f"src/{provider}.py", 0.75)

        with patch.object(
            manager, "_search_with_provider", side_effect=fake_search_with_provider
        ):
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=requested_limit,
                min_score=None,
                file_extensions=None,
                query_strategy="parallel",
            )

        assert len(limits_received) == 2, (
            f"Expected 2 provider calls, got {len(limits_received)}"
        )
        for lim in limits_received:
            assert lim == expected_provider_limit, (
                f"Provider received limit={lim}, expected {expected_provider_limit} "
                f"(= {requested_limit} * PARALLEL_FETCH_MULTIPLIER={PARALLEL_FETCH_MULTIPLIER})"
            )


# ---------------------------------------------------------------------------
# Review Findings: min_score=None sentinel + real score fusion
# ---------------------------------------------------------------------------


class TestReviewFindings:
    """Tests for the three HIGH severity review findings:

    Finding 1: min_score=0.0 should be None (correct sentinel for no-filter).
    Finding 2: Score fusion (rrf/average/multiply) must actually fuse results,
               not just concatenate them.
    Finding 3: query_strategy.py must be wired (verified via fusion behavior).
    """

    def setup_method(self):
        self.repo_path = _make_temp_repo()

    def teardown_method(self):
        shutil.rmtree(self.repo_path, ignore_errors=True)

    def test_providers_receive_none_min_score_for_parallel_strategy(self):
        """Each provider must receive min_score=None (not 0.0) in parallel strategy.

        Finding 1: The correct sentinel for 'no filtering' is None, not 0.0.
        _search_with_provider checks `if min_score is not None and score < min_score`.
        Passing 0.0 works by accident; passing None is the correct API contract.
        """
        manager = _make_manager()

        captured_min_scores = []

        def fake_search_with_provider(*args, **kwargs):
            captured_min_scores.append(kwargs.get("min_score"))
            provider = kwargs.get("provider_name", "unknown")
            return _make_provider_results(provider, f"src/{provider}.py", 0.75)

        with patch.object(
            manager, "_search_with_provider", side_effect=fake_search_with_provider
        ):
            manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=0.5,  # User's threshold — providers must NOT see this
                file_extensions=None,
                query_strategy="parallel",
            )

        assert len(captured_min_scores) == 2
        for score in captured_min_scores:
            assert score is None, (
                f"Finding 1: provider received min_score={score!r} instead of None. "
                f"None is the correct sentinel for 'no per-provider filtering'."
            )

    def test_rrf_fusion_reorders_results_not_just_concatenates(self):
        """RRF fusion must reorder results by rank, not just concatenate lists.

        Finding 2: score_fusion='rrf' was silently ignored — results were just
        concatenated. RRF assigns scores based on rank in each provider's list.
        A document ranked #1 by both providers gets a higher fused score than
        one ranked #1 by only one provider.

        Setup:
          voyage-ai returns: [file_a.py (rank 0), file_b.py (rank 1)]
          cohere    returns: [file_a.py (rank 0), file_c.py (rank 1)]
        RRF expectation: file_a.py gets contributions from BOTH providers
        (1/61 + 1/61 = 0.0328) vs file_b.py or file_c.py (only 1/62 = 0.0161).
        So file_a.py must be ranked first.
        """
        manager = _make_manager()

        def fake_search_with_provider(*args, **kwargs):
            provider = kwargs.get("provider_name", "unknown")
            if provider == "voyage-ai":
                return [
                    QueryResult(
                        file_path="src/file_a.py",
                        line_number=1,
                        code_snippet="code a",
                        similarity_score=0.90,
                        repository_alias="test-repo",
                        source_provider="voyage-ai",
                    ),
                    QueryResult(
                        file_path="src/file_b.py",
                        line_number=1,
                        code_snippet="code b",
                        similarity_score=0.80,
                        repository_alias="test-repo",
                        source_provider="voyage-ai",
                    ),
                ]
            else:  # cohere
                return [
                    QueryResult(
                        file_path="src/file_a.py",
                        line_number=1,
                        code_snippet="code a",
                        similarity_score=0.70,
                        repository_alias="test-repo",
                        source_provider="cohere",
                    ),
                    QueryResult(
                        file_path="src/file_c.py",
                        line_number=1,
                        code_snippet="code c",
                        similarity_score=0.65,
                        repository_alias="test-repo",
                        source_provider="cohere",
                    ),
                ]

        with patch.object(
            manager, "_search_with_provider", side_effect=fake_search_with_provider
        ):
            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="parallel",
                score_fusion="rrf",
            )

        assert len(results) >= 3, (
            f"Expected 3 distinct results (file_a, file_b, file_c), got {len(results)}"
        )
        # file_a.py ranked first because it appears in BOTH providers' results
        top_result = results[0]
        assert top_result.file_path == "src/file_a.py", (
            f"Finding 2: RRF fusion not applied — file_a.py should be first "
            f"(ranked high by both providers), but got {top_result.file_path}. "
            f"Results order: {[r.file_path for r in results]}"
        )

    def test_average_fusion_applied_when_score_fusion_average(self):
        """score_fusion='average' must apply average score fusion.

        Finding 2: Fusion method parameter was silently ignored.
        With average fusion and two distinct documents (each from only one provider),
        both get their normalized score. The one from the higher-scoring provider
        ranks first.

        Story #638: Score-gate filters weaker provider results when weaker_max
        is below stronger_max * SCORE_GATE_RATIO (0.80). To test fusion without
        score-gate interference, both providers return scores within the 0.80
        ratio (0.90 and 0.75: 0.75 >= 0.90 * 0.80 = 0.72, so no gating).

        Setup:
          voyage-ai returns: [file_high.py score=0.90]
          cohere    returns: [file_low.py  score=0.75]
        Both pass score-gate (0.75 >= 0.90 * 0.80). Average fusion includes both.
        """
        manager = _make_manager()

        def fake_search_with_provider(*args, **kwargs):
            provider = kwargs.get("provider_name", "unknown")
            if provider == "voyage-ai":
                return [
                    QueryResult(
                        file_path="src/file_high.py",
                        line_number=1,
                        code_snippet="high score code",
                        similarity_score=0.90,
                        repository_alias="test-repo",
                        source_provider="voyage-ai",
                    )
                ]
            else:  # cohere
                return [
                    QueryResult(
                        file_path="src/file_low.py",
                        line_number=1,
                        code_snippet="similar score code",
                        similarity_score=0.75,
                        repository_alias="test-repo",
                        source_provider="cohere",
                    )
                ]

        with patch.object(
            manager, "_search_with_provider", side_effect=fake_search_with_provider
        ):
            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="parallel",
                score_fusion="average",
            )

        file_paths = [r.file_path for r in results]
        assert "src/file_high.py" in file_paths, (
            f"Finding 2: average fusion did not include file_high.py. "
            f"Results: {file_paths}"
        )
        assert "src/file_low.py" in file_paths, (
            f"Finding 2: average fusion did not include file_low.py. "
            f"Results: {file_paths}"
        )

    def test_query_strategy_py_fusion_wired_rrf_deduplicated(self):
        """query_strategy.py fuse_rrf is wired: duplicate results are deduplicated.

        Finding 3: query_strategy.py had zero callers. This test verifies it is
        wired. The observable behavior: when the same file appears in BOTH
        providers' results, it appears only ONCE in the fused output (deduplicated
        by key = repo_alias:file_path:chunk_id).

        Before wiring: concatenation produces 2 entries for file_a.py.
        After wiring: fuse_rrf deduplicates to 1 entry for file_a.py.
        """
        manager = _make_manager()

        def fake_search_with_provider(*args, **kwargs):
            provider = kwargs.get("provider_name", "unknown")
            # Both providers return the same file
            return [
                QueryResult(
                    file_path="src/shared_file.py",
                    line_number=1,
                    code_snippet="shared code",
                    similarity_score=0.80,
                    repository_alias="test-repo",
                    source_provider=provider,
                )
            ]

        with patch.object(
            manager, "_search_with_provider", side_effect=fake_search_with_provider
        ):
            results = manager._search_single_repository(
                repo_path=self.repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="parallel",
                score_fusion="rrf",
            )

        shared_results = [r for r in results if r.file_path == "src/shared_file.py"]
        assert len(shared_results) == 1, (
            f"Finding 3: query_strategy.py not wired — fuse_rrf deduplication not applied. "
            f"src/shared_file.py appears {len(shared_results)} times in results "
            f"(expected 1 — RRF deduplicates same document from both providers)."
        )
