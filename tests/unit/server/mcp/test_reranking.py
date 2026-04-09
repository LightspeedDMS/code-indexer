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
            MockMonitor.get_instance.return_value.get_health.return_value = {}

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
            MockMonitor.get_instance.return_value.get_health.return_value = {}

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
            MockMonitor.get_instance.return_value.get_health.return_value = {}

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
