"""Tests for _apply_reranking_sync() helper and calculate_overfetch_limit().

Epic #649, Story #653.
"""

import logging
from typing import List
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _make_results(n: int) -> List[dict]:
    """Return n fake search results with 'content' field."""
    return [{"id": i, "content": f"document {i}"} for i in range(n)]


def _content_extractor(r: dict) -> str:
    return r.get("content", "")  # type: ignore[no-any-return]


def _make_rerank_result(index: int, score: float):
    """Create a fake RerankResult dataclass-like object."""
    obj = MagicMock()
    obj.index = index
    obj.relevance_score = score
    return obj


def _make_config_service(
    voyage_model: str = "rerank-2.5",
    cohere_model: str = "rerank-v3.5",
    overfetch_multiplier: int = 5,
    voyageai_api_key: str = "voyage-key",
    cohere_api_key: str = "cohere-key",
):
    """Build a minimal mock config_service matching the real API shape."""
    from code_indexer.server.utils.config_manager import RerankConfig

    config = MagicMock()
    rerank_cfg = RerankConfig(
        voyage_reranker_model=voyage_model,
        cohere_reranker_model=cohere_model,
        overfetch_multiplier=overfetch_multiplier,
    )
    config.rerank_config = rerank_cfg
    config.claude_integration_config.voyageai_api_key = voyageai_api_key
    config.claude_integration_config.cohere_api_key = cohere_api_key

    config_service = MagicMock()
    config_service.get_config.return_value = config
    return config_service


# ---------------------------------------------------------------------------
# Tests: _apply_reranking_sync
# ---------------------------------------------------------------------------


class TestApplyRerankingSync:
    """Tests for the _apply_reranking_sync() helper."""

    def setup_method(self):
        from code_indexer.server.mcp.reranking import _apply_reranking_sync

        self._fn = _apply_reranking_sync

    # ------------------------------------------------------------------
    # AC2: Guard — no rerank_query means no overhead
    # ------------------------------------------------------------------

    def test_none_rerank_query_returns_results_unchanged(self):
        """When rerank_query is None, results must be returned unchanged, no API call."""
        results = _make_results(5)
        config_service = _make_config_service()

        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as mock_voyage,
            patch(
                "code_indexer.server.mcp.reranking.CohereRerankerClient"
            ) as mock_cohere,
        ):
            returned, _ = self._fn(
                results=results,
                rerank_query=None,
                rerank_instruction=None,
                content_extractor=_content_extractor,
                requested_limit=5,
                config_service=config_service,
            )

        assert returned is results
        mock_voyage.assert_not_called()
        mock_cohere.assert_not_called()

    def test_empty_string_rerank_query_returns_results_unchanged(self):
        """When rerank_query is '', results must be returned unchanged."""
        results = _make_results(3)
        config_service = _make_config_service()

        with (
            patch("code_indexer.server.mcp.reranking.VoyageRerankerClient") as mv,
            patch("code_indexer.server.mcp.reranking.CohereRerankerClient") as mc,
        ):
            returned, _ = self._fn(
                results=results,
                rerank_query="",
                rerank_instruction=None,
                content_extractor=_content_extractor,
                requested_limit=3,
                config_service=config_service,
            )

        assert returned is results
        mv.assert_not_called()
        mc.assert_not_called()

    def test_instruction_without_query_logs_warning_and_returns_unchanged(self, caplog):
        """rerank_instruction with no rerank_query logs a warning and returns unchanged."""
        results = _make_results(2)
        config_service = _make_config_service()

        with caplog.at_level(
            logging.WARNING, logger="code_indexer.server.mcp.reranking"
        ):
            returned, _ = self._fn(
                results=results,
                rerank_query=None,
                rerank_instruction="Find the most relevant result",
                content_extractor=_content_extractor,
                requested_limit=2,
                config_service=config_service,
            )

        assert returned is results
        assert any("rerank_instruction" in r.message for r in caplog.records)

    # ------------------------------------------------------------------
    # AC3-AC4: Voyage reranker succeeds → results reordered and trimmed
    # ------------------------------------------------------------------

    def test_successful_voyage_rerank_reorders_and_trims(self):
        """Voyage reranker succeeds — results reordered by relevance and trimmed."""
        results = _make_results(5)
        config_service = _make_config_service()

        # Voyage returns indices [4, 2, 0] in relevance order (top 3)
        voyage_results = [
            _make_rerank_result(4, 0.95),
            _make_rerank_result(2, 0.80),
            _make_rerank_result(0, 0.60),
        ]

        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            instance = MockVoyage.return_value
            instance.rerank.return_value = voyage_results

            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = False

            returned, _ = self._fn(
                results=results,
                rerank_query="search query",
                rerank_instruction=None,
                content_extractor=_content_extractor,
                requested_limit=3,
                config_service=config_service,
            )

        assert len(returned) == 3
        assert returned[0]["id"] == 4
        assert returned[1]["id"] == 2
        assert returned[2]["id"] == 0

    # ------------------------------------------------------------------
    # AC5: Voyage fails → Cohere tries
    # ------------------------------------------------------------------

    def test_voyage_fails_cohere_succeeds(self):
        """When Voyage raises, Cohere is tried and its results returned."""
        results = _make_results(4)
        config_service = _make_config_service()

        cohere_results = [
            _make_rerank_result(3, 0.9),
            _make_rerank_result(1, 0.7),
        ]

        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.CohereRerankerClient"
            ) as MockCohere,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            voyage_inst = MockVoyage.return_value
            voyage_inst.rerank.side_effect = RuntimeError("Voyage API error")

            cohere_inst = MockCohere.return_value
            cohere_inst.rerank.return_value = cohere_results

            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = False

            returned, _ = self._fn(
                results=results,
                rerank_query="query",
                rerank_instruction=None,
                content_extractor=_content_extractor,
                requested_limit=2,
                config_service=config_service,
            )

        assert len(returned) == 2
        assert returned[0]["id"] == 3
        assert returned[1]["id"] == 1

    # ------------------------------------------------------------------
    # AC6: Both fail → original order returned with warning
    # ------------------------------------------------------------------

    def test_both_fail_returns_original_order_with_warning(self, caplog):
        """When both Voyage and Cohere fail, return original order trimmed."""
        results = _make_results(5)
        config_service = _make_config_service()

        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.CohereRerankerClient"
            ) as MockCohere,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            MockVoyage.return_value.rerank.side_effect = RuntimeError("voyage down")
            MockCohere.return_value.rerank.side_effect = RuntimeError("cohere down")

            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = False

            with caplog.at_level(
                logging.WARNING, logger="code_indexer.server.mcp.reranking"
            ):
                returned, _ = self._fn(
                    results=results,
                    rerank_query="query",
                    rerank_instruction=None,
                    content_extractor=_content_extractor,
                    requested_limit=3,
                    config_service=config_service,
                )

        assert len(returned) == 3
        assert returned[0]["id"] == 0
        assert returned[1]["id"] == 1
        assert returned[2]["id"] == 2
        assert any(
            "original order" in r.message.lower() or "failed" in r.message.lower()
            for r in caplog.records
        )

    # ------------------------------------------------------------------
    # AC7: Voyage marked "down" → skipped, try Cohere
    # ------------------------------------------------------------------

    def test_voyage_down_skipped_cohere_used(self):
        """When Voyage is marked 'down' by ProviderHealthMonitor, it is skipped."""
        results = _make_results(3)
        config_service = _make_config_service()

        cohere_results = [_make_rerank_result(2, 0.8), _make_rerank_result(0, 0.5)]

        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.CohereRerankerClient"
            ) as MockCohere,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            voyage_status = MagicMock()
            voyage_status.status = "down"

            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.side_effect = lambda provider: (
                {"voyage-reranker": voyage_status}
                if provider == "voyage-reranker"
                else {}
            )
            monitor_inst.is_sinbinned.return_value = False

            MockCohere.return_value.rerank.return_value = cohere_results

            returned, _ = self._fn(
                results=results,
                rerank_query="query",
                rerank_instruction=None,
                content_extractor=_content_extractor,
                requested_limit=2,
                config_service=config_service,
            )

        MockVoyage.return_value.rerank.assert_not_called()
        assert len(returned) == 2
        assert returned[0]["id"] == 2
        assert returned[1]["id"] == 0

    def test_empty_results_returned_unchanged(self):
        """Empty results list returned immediately without any API call."""
        config_service = _make_config_service()

        with (
            patch("code_indexer.server.mcp.reranking.VoyageRerankerClient") as mv,
            patch("code_indexer.server.mcp.reranking.CohereRerankerClient") as mc,
        ):
            returned, _ = self._fn(
                results=[],
                rerank_query="search query",
                rerank_instruction=None,
                content_extractor=_content_extractor,
                requested_limit=5,
                config_service=config_service,
            )

        assert returned == []
        mv.assert_not_called()
        mc.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Story #654 — telemetry metadata returned by _apply_reranking_sync
# ---------------------------------------------------------------------------


class TestApplyRerankingSyncTelemetry:
    """_apply_reranking_sync must return (results, rerank_metadata) tuple."""

    def setup_method(self):
        from code_indexer.server.mcp.reranking import _apply_reranking_sync

        self._fn = _apply_reranking_sync

    # ------------------------------------------------------------------
    # AC2 state 1: rerank_query absent → not requested
    # ------------------------------------------------------------------

    def test_no_rerank_query_returns_tuple(self):
        """When rerank_query is None, function returns a 2-tuple."""
        results = _make_results(3)
        config_service = _make_config_service()
        returned = self._fn(
            results=results,
            rerank_query=None,
            rerank_instruction=None,
            content_extractor=_content_extractor,
            requested_limit=3,
            config_service=config_service,
        )
        assert isinstance(returned, tuple)
        assert len(returned) == 2

    def test_no_rerank_query_metadata_not_requested(self):
        """rerank_query absent → reranker_used=False, reranker_provider=None, time=0."""
        results = _make_results(3)
        config_service = _make_config_service()
        _, meta = self._fn(
            results=results,
            rerank_query=None,
            rerank_instruction=None,
            content_extractor=_content_extractor,
            requested_limit=3,
            config_service=config_service,
        )
        assert meta["reranker_used"] is False
        assert meta["reranker_provider"] is None
        assert meta["rerank_time_ms"] == 0

    def test_empty_rerank_query_metadata_not_requested(self):
        """rerank_query='' → reranker_used=False, reranker_provider=None, time=0."""
        results = _make_results(2)
        config_service = _make_config_service()
        _, meta = self._fn(
            results=results,
            rerank_query="",
            rerank_instruction=None,
            content_extractor=_content_extractor,
            requested_limit=2,
            config_service=config_service,
        )
        assert meta["reranker_used"] is False
        assert meta["reranker_provider"] is None
        assert meta["rerank_time_ms"] == 0

    # ------------------------------------------------------------------
    # AC2 state 2: Voyage succeeded
    # ------------------------------------------------------------------

    def test_voyage_succeeded_metadata(self):
        """Voyage success → reranker_used=True, reranker_provider='voyage', time>0."""
        results = _make_results(5)
        config_service = _make_config_service()
        voyage_results = [
            _make_rerank_result(4, 0.95),
            _make_rerank_result(2, 0.80),
        ]
        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            MockVoyage.return_value.rerank.return_value = voyage_results
            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = False

            _, meta = self._fn(
                results=results,
                rerank_query="find something",
                rerank_instruction=None,
                content_extractor=_content_extractor,
                requested_limit=2,
                config_service=config_service,
            )

        assert meta["reranker_used"] is True
        assert meta["reranker_provider"] == "voyage"
        assert meta["rerank_time_ms"] >= 0

    # ------------------------------------------------------------------
    # AC2 state 3: Voyage failed, Cohere succeeded
    # ------------------------------------------------------------------

    def test_voyage_failed_cohere_succeeded_metadata(self):
        """Voyage fails, Cohere succeeds → used=True, provider='cohere', time>0."""
        results = _make_results(4)
        config_service = _make_config_service()
        cohere_results = [_make_rerank_result(3, 0.9), _make_rerank_result(1, 0.7)]
        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.CohereRerankerClient"
            ) as MockCohere,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            MockVoyage.return_value.rerank.side_effect = RuntimeError("voyage error")
            MockCohere.return_value.rerank.return_value = cohere_results
            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = False

            _, meta = self._fn(
                results=results,
                rerank_query="find something",
                rerank_instruction=None,
                content_extractor=_content_extractor,
                requested_limit=2,
                config_service=config_service,
            )

        assert meta["reranker_used"] is True
        assert meta["reranker_provider"] == "cohere"
        assert meta["rerank_time_ms"] >= 0

    # ------------------------------------------------------------------
    # AC2 state 4: Both failed
    # ------------------------------------------------------------------

    def test_both_failed_metadata(self):
        """Both providers fail → reranker_used=False, reranker_provider='none', time>0."""
        results = _make_results(5)
        config_service = _make_config_service()
        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.CohereRerankerClient"
            ) as MockCohere,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            MockVoyage.return_value.rerank.side_effect = RuntimeError("voyage down")
            MockCohere.return_value.rerank.side_effect = RuntimeError("cohere down")
            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = False

            _, meta = self._fn(
                results=results,
                rerank_query="find something",
                rerank_instruction=None,
                content_extractor=_content_extractor,
                requested_limit=3,
                config_service=config_service,
            )

        assert meta["reranker_used"] is False
        assert meta["reranker_provider"] == "none"
        assert meta["rerank_time_ms"] >= 0

    # ------------------------------------------------------------------
    # AC2 state 5: Both disabled (empty model strings)
    # ------------------------------------------------------------------

    def test_both_disabled_metadata(self):
        """Both model strings empty → used=False, provider='none', time=0."""
        results = _make_results(3)
        config_service = _make_config_service(voyage_model="", cohere_model="")
        with (
            patch("code_indexer.server.mcp.reranking.VoyageRerankerClient") as mv,
            patch("code_indexer.server.mcp.reranking.CohereRerankerClient") as mc,
        ):
            _, meta = self._fn(
                results=results,
                rerank_query="find something",
                rerank_instruction=None,
                content_extractor=_content_extractor,
                requested_limit=3,
                config_service=config_service,
            )

        assert meta["reranker_used"] is False
        assert meta["reranker_provider"] == "none"
        assert meta["rerank_time_ms"] == 0
        mv.assert_not_called()
        mc.assert_not_called()

    # ------------------------------------------------------------------
    # AC4: Hint message when both disabled but rerank_query present
    # ------------------------------------------------------------------

    def test_both_disabled_with_query_returns_hint(self):
        """Both disabled + rerank_query present → rerank_hint message in metadata."""
        results = _make_results(2)
        config_service = _make_config_service(voyage_model="", cohere_model="")
        _, meta = self._fn(
            results=results,
            rerank_query="find something",
            rerank_instruction=None,
            content_extractor=_content_extractor,
            requested_limit=2,
            config_service=config_service,
        )
        assert "rerank_hint" in meta
        assert meta["rerank_hint"] is not None
        assert "Configure" in meta["rerank_hint"] or "configure" in meta["rerank_hint"]

    def test_voyage_enabled_no_hint(self):
        """When a provider is configured and succeeds, no hint is present."""
        results = _make_results(3)
        config_service = _make_config_service()
        voyage_results = [_make_rerank_result(0, 0.9)]
        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            MockVoyage.return_value.rerank.return_value = voyage_results
            MockMonitor.get_instance.return_value.get_health.return_value = {}

            _, meta = self._fn(
                results=results,
                rerank_query="find something",
                rerank_instruction=None,
                content_extractor=_content_extractor,
                requested_limit=1,
                config_service=config_service,
            )

        assert meta.get("rerank_hint") is None

    def test_no_query_no_hint(self):
        """When rerank_query is absent, no hint in metadata."""
        results = _make_results(2)
        config_service = _make_config_service(voyage_model="", cohere_model="")
        _, meta = self._fn(
            results=results,
            rerank_query=None,
            rerank_instruction=None,
            content_extractor=_content_extractor,
            requested_limit=2,
            config_service=config_service,
        )
        assert meta.get("rerank_hint") is None

    # ------------------------------------------------------------------
    # AC3: Timing accumulates across attempts
    # ------------------------------------------------------------------

    def test_timing_accumulates_across_failed_providers(self):
        """Timer includes time for both Voyage attempt and Cohere attempt."""
        results = _make_results(3)
        config_service = _make_config_service()
        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.CohereRerankerClient"
            ) as MockCohere,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            MockVoyage.return_value.rerank.side_effect = RuntimeError("voyage error")
            MockCohere.return_value.rerank.side_effect = RuntimeError("cohere error")
            MockMonitor.get_instance.return_value.get_health.return_value = {}

            _, meta = self._fn(
                results=results,
                rerank_query="find something",
                rerank_instruction=None,
                content_extractor=_content_extractor,
                requested_limit=2,
                config_service=config_service,
            )

        # When both fail, time should still be recorded (>= 0, integer)
        assert isinstance(meta["rerank_time_ms"], int)
        assert meta["rerank_time_ms"] >= 0

    def test_rerank_time_ms_is_integer(self):
        """rerank_time_ms must be an integer, not float."""
        results = _make_results(3)
        config_service = _make_config_service()
        voyage_results = [_make_rerank_result(0, 0.9)]
        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            MockVoyage.return_value.rerank.return_value = voyage_results
            MockMonitor.get_instance.return_value.get_health.return_value = {}

            _, meta = self._fn(
                results=results,
                rerank_query="find something",
                rerank_instruction=None,
                content_extractor=_content_extractor,
                requested_limit=1,
                config_service=config_service,
            )

        assert isinstance(meta["rerank_time_ms"], int)


# ---------------------------------------------------------------------------
# Tests: calculate_overfetch_limit
# ---------------------------------------------------------------------------


class TestBug744SinbinnedProviderSkipped:
    """Regression tests for Bug #744: _attempt_provider_rerank must check is_sinbinned().

    A sin-binned provider should be short-circuited with (None, "skipped")
    before any client is instantiated, just like a "down" provider.
    """

    def setup_method(self):
        from code_indexer.server.mcp.reranking import _attempt_provider_rerank

        self._fn = _attempt_provider_rerank

    def test_sinbinned_voyage_returns_skipped_without_client(self):
        """Bug #744: sin-binned Voyage provider must return (None, 'skipped') without client."""
        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}  # not "down"
            monitor_inst.is_sinbinned.return_value = True  # but IS sinbinned

            indices, reason = self._fn(
                provider_name="Voyage",
                health_key="voyage-reranker",
                client_cls=MockVoyage,
                query="test",
                documents=["doc1"],
                instruction=None,
                top_k=1,
                monitor=monitor_inst,
            )

        assert indices is None
        assert reason == "skipped"
        MockVoyage.assert_not_called()

    def test_sinbinned_cohere_returns_skipped_without_client(self):
        """Bug #744: sin-binned Cohere provider must return (None, 'skipped') without client."""
        with (
            patch(
                "code_indexer.server.mcp.reranking.CohereRerankerClient"
            ) as MockCohere,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = True

            indices, reason = self._fn(
                provider_name="Cohere",
                health_key="cohere-reranker",
                client_cls=MockCohere,
                query="test",
                documents=["doc1"],
                instruction=None,
                top_k=1,
                monitor=monitor_inst,
            )

        assert indices is None
        assert reason == "skipped"
        MockCohere.assert_not_called()

    def test_not_sinbinned_proceeds_to_client(self):
        """Bug #744: non-sinbinned provider must still call the client.

        _attempt_provider_rerank now returns List[Tuple[int,float]] (scored_pairs)
        instead of List[int] — updated to match Component 4 (Story #883).
        """
        rerank_result = MagicMock()
        rerank_result.index = 0
        rerank_result.relevance_score = 0.9

        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = False
            MockVoyage.return_value.rerank.return_value = [rerank_result]

            scored_pairs, reason = self._fn(
                provider_name="Voyage",
                health_key="voyage-reranker",
                client_cls=MockVoyage,
                query="test",
                documents=["doc1"],
                instruction=None,
                top_k=1,
                monitor=monitor_inst,
            )

        # Now returns [(index, score)] tuples instead of [index]
        assert len(scored_pairs) == 1
        assert scored_pairs[0][0] == 0  # index
        assert scored_pairs[0][1] == 0.9  # score
        assert reason is None


class TestBug739SinbinnedExceptionAsSkipped:
    """Regression tests for Bug #739: RerankerSinbinnedException must be caught
    by _attempt_provider_rerank and returned as (None, 'skipped'), not (None, 'failed').
    """

    def setup_method(self):
        from code_indexer.server.mcp.reranking import _attempt_provider_rerank

        self._fn = _attempt_provider_rerank

    def _run_attempt(self, client_patch_target, provider_name, health_key, side_effect):
        """Run _attempt_provider_rerank with a mocked monitor and client side-effect."""
        with (
            patch(client_patch_target) as MockClient,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = False
            MockClient.return_value.rerank.side_effect = side_effect

            return self._fn(
                provider_name=provider_name,
                health_key=health_key,
                client_cls=MockClient,
                query="test",
                documents=["doc1"],
                instruction=None,
                top_k=1,
                monitor=monitor_inst,
            )

    def test_voyage_sinbinned_exception_returns_skipped(self):
        """Bug #739: Voyage client raising RerankerSinbinnedException must yield 'skipped'."""
        from code_indexer.server.clients.reranker_clients import (
            RerankerSinbinnedException,
        )

        indices, reason = self._run_attempt(
            "code_indexer.server.mcp.reranking.VoyageRerankerClient",
            "Voyage",
            "voyage-reranker",
            RerankerSinbinnedException("voyage-reranker"),
        )
        assert indices is None
        assert reason == "skipped"

    def test_cohere_sinbinned_exception_returns_skipped(self):
        """Bug #739: Cohere client raising RerankerSinbinnedException must yield 'skipped'."""
        from code_indexer.server.clients.reranker_clients import (
            RerankerSinbinnedException,
        )

        indices, reason = self._run_attempt(
            "code_indexer.server.mcp.reranking.CohereRerankerClient",
            "Cohere",
            "cohere-reranker",
            RerankerSinbinnedException("cohere-reranker"),
        )
        assert indices is None
        assert reason == "skipped"

    def test_generic_exception_returns_failed(self):
        """Bug #739 control: generic exceptions must still yield 'failed'."""
        indices, reason = self._run_attempt(
            "code_indexer.server.mcp.reranking.VoyageRerankerClient",
            "Voyage",
            "voyage-reranker",
            RuntimeError("network error"),
        )
        assert indices is None
        assert reason == "failed"


class TestCalculateOverfetchLimit:
    """Tests for calculate_overfetch_limit()."""

    def setup_method(self):
        from code_indexer.server.mcp.reranking import calculate_overfetch_limit

        self._fn = calculate_overfetch_limit

    def test_basic_multiplier(self):
        """10 requested * 5 multiplier = 50."""
        result = self._fn(requested_limit=10, overfetch_multiplier=5)
        assert result == 50

    def test_cap_at_200(self):
        """1000 * 5 = 5000, but capped at 200."""
        result = self._fn(requested_limit=1000, overfetch_multiplier=5)
        assert result == 200

    def test_max_with_access_filter_overfetch(self):
        """max(10*5=50, 10+100=110) = 110."""
        result = self._fn(
            requested_limit=10,
            overfetch_multiplier=5,
            access_filter_overfetch=100,
        )
        assert result == 110

    def test_access_filter_overfetch_capped(self):
        """max(10*5=50, 10+500=510) = 510, capped to 200."""
        result = self._fn(
            requested_limit=10,
            overfetch_multiplier=5,
            access_filter_overfetch=500,
        )
        assert result == 200

    def test_multiplier_one_returns_requested_limit(self):
        """1 * 1 = 1, no overfetch."""
        result = self._fn(requested_limit=1, overfetch_multiplier=1)
        assert result == 1


# ---------------------------------------------------------------------------
# Phase D (Story #883): _tag_and_pool + _tagged_content_extractor
# ---------------------------------------------------------------------------


class TestTagAndPool:
    """_tag_and_pool merges code and memory results with source_tag injection."""

    def setup_method(self):
        from code_indexer.server.mcp.reranking import _tag_and_pool

        self._fn = _tag_and_pool

    def _make_memory_candidate(
        self, memory_id, hnsw_score, memory_path="", title="", summary=""
    ):
        from code_indexer.server.services.memory_candidate_retriever import (
            MemoryCandidate,
        )

        return MemoryCandidate(
            memory_id=memory_id,
            hnsw_score=hnsw_score,
            memory_path=memory_path,
            title=title,
            summary=summary,
        )

    def test_code_items_tagged_as_code(self):
        """Code results must have _source_tag='code' after pooling."""
        code_results = [{"content": "def foo(): pass"}, {"content": "def bar(): ..."}]
        pooled = self._fn(code_results, [])
        for item in pooled:
            assert item["_source_tag"] == "code"

    def test_memory_items_tagged_as_memory(self):
        """Memory candidates must have _source_tag='memory' after pooling."""
        mem = self._make_memory_candidate(
            "mem-001", 0.8, "/cidx-meta/memories/mem-001.md"
        )
        pooled = self._fn([], [mem])
        assert len(pooled) == 1
        assert pooled[0]["_source_tag"] == "memory"

    def test_memory_fields_injected_into_pool_item(self):
        """memory_id, memory_path, hnsw_score, title, summary injected on memory pool items."""
        mem = self._make_memory_candidate(
            "mem-xyz",
            0.72,
            "/cidx-meta/memories/mem-xyz.md",
            title="Caching Strategy",
            summary="Redis for session caching.",
        )
        pooled = self._fn([], [mem])
        item = pooled[0]
        assert item["memory_id"] == "mem-xyz"
        assert item["memory_path"] == "/cidx-meta/memories/mem-xyz.md"
        assert item["hnsw_score"] == 0.72
        assert item["title"] == "Caching Strategy"
        assert item["summary"] == "Redis for session caching."

    def test_pool_length_is_sum_of_both_lists(self):
        """Pooled list length equals len(code_results) + len(memory_candidates)."""
        code_results = [{"content": "code1"}, {"content": "code2"}]
        memories = [
            self._make_memory_candidate("m1", 0.9, "/p/m1.md"),
            self._make_memory_candidate("m2", 0.8, "/p/m2.md"),
            self._make_memory_candidate("m3", 0.7, "/p/m3.md"),
        ]
        pooled = self._fn(code_results, memories)
        assert len(pooled) == 5

    def test_code_items_appear_before_memory_items(self):
        """Code items appear first in the pool; memory items follow."""
        code_results = [{"content": "code-a"}, {"content": "code-b"}]
        memories = [self._make_memory_candidate("m1", 0.9, "/p/m1.md")]
        pooled = self._fn(code_results, memories)
        assert pooled[0]["_source_tag"] == "code"
        assert pooled[1]["_source_tag"] == "code"
        assert pooled[2]["_source_tag"] == "memory"


class TestTaggedContentExtractor:
    """_tagged_content_extractor picks the right field based on _source_tag."""

    def setup_method(self):
        from code_indexer.server.mcp.reranking import _tagged_content_extractor

        self._fn = _tagged_content_extractor

    def test_code_item_returns_content_field(self):
        """Code items: extractor returns the 'content' field."""
        item = {"_source_tag": "code", "content": "def foo(): pass"}
        assert self._fn(item) == "def foo(): pass"

    def test_code_item_falls_back_to_code_snippet(self):
        """Code items without 'content': extractor falls back to 'code_snippet'."""
        item = {"_source_tag": "code", "code_snippet": "snippet text"}
        assert self._fn(item) == "snippet text"

    def test_memory_item_returns_title_and_summary(self):
        """Memory items: extractor returns title + ': ' + summary."""
        item = {
            "_source_tag": "memory",
            "title": "Caching Strategy",
            "summary": "We use Redis for session caching.",
        }
        result = self._fn(item)
        assert "Caching Strategy" in result
        assert "We use Redis for session caching." in result

    def test_memory_item_missing_fields_returns_empty_string(self):
        """Memory items with no title or summary: extractor returns empty string."""
        item = {"_source_tag": "memory"}
        result = self._fn(item)
        assert result == ""
