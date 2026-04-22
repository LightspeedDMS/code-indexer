"""Tests for Phase A (Story #883) — rerank_score attachment by _apply_reranking_sync.

Component 4 of 14:
  _apply_reranking_sync must write results[i]["rerank_score"] = score on success
  so downstream memory retrieval can apply Cohere floor filters.

All 10 existing callers use positional tuple unpacking (results, meta = ...) and
never introspect rerank_score, so adding the key is non-breaking.

TDD: these tests are written BEFORE the implementation.
"""

from contextlib import contextmanager
from typing import List
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


def _make_results(n: int) -> List[dict]:
    """Return n fake search results with 'content' field."""
    return [{"id": i, "content": f"document {i}"} for i in range(n)]


def _content_extractor(r: dict) -> str:
    return r.get("content", "")


def _make_rerank_result(index: int, score: float):
    """Create a fake RerankResult dataclass-like object."""
    obj = MagicMock()
    obj.index = index
    obj.relevance_score = score
    return obj


def _make_config_service(
    voyage_model: str = "rerank-2.5",
    cohere_model: str = "rerank-v3.5",
):
    """Build a minimal mock config_service matching the real API shape."""
    from code_indexer.server.utils.config_manager import RerankConfig

    config = MagicMock()
    rerank_cfg = RerankConfig(
        voyage_reranker_model=voyage_model,
        cohere_reranker_model=cohere_model,
        overfetch_multiplier=5,
    )
    config.rerank_config = rerank_cfg
    config.claude_integration_config.voyageai_api_key = "voyage-key"
    config.claude_integration_config.cohere_api_key = "cohere-key"
    config_service = MagicMock()
    config_service.get_config.return_value = config
    return config_service


@contextmanager
def _voyage_success_context(voyage_rerank_results):
    """Patch Voyage to return given results; Cohere uncalled; monitor healthy."""
    with (
        patch("code_indexer.server.mcp.reranking.VoyageRerankerClient") as MockVoyage,
        patch("code_indexer.server.mcp.reranking.ProviderHealthMonitor") as MockMonitor,
    ):
        MockVoyage.return_value.rerank.return_value = voyage_rerank_results
        monitor_inst = MockMonitor.get_instance.return_value
        monitor_inst.get_health.return_value = {}
        monitor_inst.is_sinbinned.return_value = False
        yield


@contextmanager
def _cohere_fallback_context(cohere_rerank_results):
    """Patch Voyage to fail, Cohere to succeed; monitor healthy."""
    with (
        patch("code_indexer.server.mcp.reranking.VoyageRerankerClient") as MockVoyage,
        patch("code_indexer.server.mcp.reranking.CohereRerankerClient") as MockCohere,
        patch("code_indexer.server.mcp.reranking.ProviderHealthMonitor") as MockMonitor,
    ):
        MockVoyage.return_value.rerank.side_effect = RuntimeError("voyage down")
        MockCohere.return_value.rerank.return_value = cohere_rerank_results
        monitor_inst = MockMonitor.get_instance.return_value
        monitor_inst.get_health.return_value = {}
        monitor_inst.is_sinbinned.return_value = False
        yield


@contextmanager
def _both_fail_context():
    """Patch both Voyage and Cohere to fail; monitor healthy."""
    with (
        patch("code_indexer.server.mcp.reranking.VoyageRerankerClient") as MockVoyage,
        patch("code_indexer.server.mcp.reranking.CohereRerankerClient") as MockCohere,
        patch("code_indexer.server.mcp.reranking.ProviderHealthMonitor") as MockMonitor,
    ):
        MockVoyage.return_value.rerank.side_effect = RuntimeError("voyage down")
        MockCohere.return_value.rerank.side_effect = RuntimeError("cohere down")
        monitor_inst = MockMonitor.get_instance.return_value
        monitor_inst.get_health.return_value = {}
        monitor_inst.is_sinbinned.return_value = False
        yield


# ---------------------------------------------------------------------------
# Phase A tests: rerank_score attachment
# ---------------------------------------------------------------------------


class TestRerankScoreAttachment:
    """_apply_reranking_sync must attach rerank_score to each returned result dict."""

    def setup_method(self):
        from code_indexer.server.mcp.reranking import _apply_reranking_sync

        self._fn = _apply_reranking_sync

    def _call(self, results, query, limit, config_service):
        """Invoke _apply_reranking_sync with standard test parameters."""
        return self._fn(
            results=results,
            rerank_query=query,
            rerank_instruction=None,
            content_extractor=_content_extractor,
            requested_limit=limit,
            config_service=config_service,
        )

    # ------------------------------------------------------------------
    # AC: Voyage success → each returned result has rerank_score key
    # ------------------------------------------------------------------

    def test_rerank_score_attached_on_voyage_success(self):
        """Voyage success: each result in returned list must have rerank_score key."""
        results = _make_results(5)
        voyage_results = [
            _make_rerank_result(4, 0.95),
            _make_rerank_result(2, 0.80),
            _make_rerank_result(0, 0.60),
        ]

        with _voyage_success_context(voyage_results):
            returned, _ = self._call(results, "test query", 3, _make_config_service())

        assert len(returned) == 3
        for r in returned:
            assert "rerank_score" in r, f"Expected rerank_score in result: {r}"

    def test_rerank_score_value_matches_relevance_score_voyage(self):
        """Voyage: rerank_score value must match the provider's relevance_score."""
        results = _make_results(5)
        voyage_results = [
            _make_rerank_result(4, 0.95),
            _make_rerank_result(2, 0.80),
            _make_rerank_result(0, 0.60),
        ]

        with _voyage_success_context(voyage_results):
            returned, _ = self._call(results, "test query", 3, _make_config_service())

        assert returned[0]["rerank_score"] == 0.95
        assert returned[1]["rerank_score"] == 0.80
        assert returned[2]["rerank_score"] == 0.60

    # ------------------------------------------------------------------
    # AC: Cohere success → each returned result has rerank_score key
    # ------------------------------------------------------------------

    def test_rerank_score_attached_on_cohere_success(self):
        """Cohere success (Voyage failed): each result must have rerank_score key."""
        results = _make_results(4)
        cohere_results = [
            _make_rerank_result(3, 0.9),
            _make_rerank_result(1, 0.7),
        ]

        with _cohere_fallback_context(cohere_results):
            returned, _ = self._call(results, "test query", 2, _make_config_service())

        assert len(returned) == 2
        for r in returned:
            assert "rerank_score" in r, f"Expected rerank_score in result: {r}"

    def test_rerank_score_value_matches_relevance_score_cohere(self):
        """Cohere: rerank_score value must match the provider's relevance_score."""
        results = _make_results(4)
        cohere_results = [
            _make_rerank_result(3, 0.9),
            _make_rerank_result(1, 0.7),
        ]

        with _cohere_fallback_context(cohere_results):
            returned, _ = self._call(results, "test query", 2, _make_config_service())

        assert returned[0]["rerank_score"] == 0.9
        assert returned[1]["rerank_score"] == 0.7

    # ------------------------------------------------------------------
    # AC: rerank disabled / failed → no rerank_score attached
    # ------------------------------------------------------------------

    def test_rerank_score_not_attached_when_disabled(self):
        """When both providers disabled (empty models), no rerank_score added."""
        results = _make_results(3)
        config_service = _make_config_service(voyage_model="", cohere_model="")

        returned, _ = self._call(results, "test query", 3, config_service)

        for r in returned:
            assert "rerank_score" not in r, (
                f"rerank_score must not be attached when reranking is disabled: {r}"
            )

    def test_rerank_score_not_attached_when_both_fail(self):
        """When both providers fail, returned results must NOT have rerank_score."""
        results = _make_results(4)

        with _both_fail_context():
            returned, _ = self._call(results, "test query", 3, _make_config_service())

        for r in returned:
            assert "rerank_score" not in r, (
                f"rerank_score must not be attached when reranking fails: {r}"
            )

    # ------------------------------------------------------------------
    # AC: Existing caller positional-unpack contract preserved
    # ------------------------------------------------------------------

    def test_existing_callers_positional_unpack_still_works(self):
        """Adding rerank_score must not break existing callers: results, meta = fn(...)."""
        results = _make_results(5)
        voyage_results = [
            _make_rerank_result(0, 0.9),
            _make_rerank_result(1, 0.8),
        ]

        with _voyage_success_context(voyage_results):
            # Simulate existing caller pattern: positional unpack
            result_list, rerank_meta = self._call(
                results, "query", 2, _make_config_service()
            )

        assert isinstance(result_list, list)
        assert isinstance(rerank_meta, dict)
        # Existing keys still present (non-breaking)
        assert "reranker_used" in rerank_meta
        assert "reranker_provider" in rerank_meta
        assert "rerank_time_ms" in rerank_meta
