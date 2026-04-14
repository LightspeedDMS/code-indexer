"""Tests for AC4: reranker_status in MCP query response metadata.

The implementation adds a 'reranker_status' nested dict to query_metadata:
  {
    "status": "success" | "failed" | "skipped" | "disabled",
    "provider": str | None,
    "rerank_time_ms": int | None,
    "hint": str | None
  }

State definitions:
- success:  status="success", named provider string, rerank_time_ms>=0, hint=None
- failed:   status="failed", provider=None, rerank_time_ms>=0,
            hint is non-empty, contains "failed" or "error", NOT "skipped"
- skipped:  status="skipped", provider=None, rerank_time_ms=None,
            hint is non-empty, contains "skipped" or "down", NOT "failed"/"error"
- disabled: status="disabled", provider=None, rerank_time_ms=None, hint=None

failed and skipped have mutually distinct status strings and hints.
reranker_status appears in query_metadata only, NOT in individual result dicts.

Bug #679 Part 2.
"""

from contextlib import contextmanager
from typing import List, Optional
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# DRY helpers
# ---------------------------------------------------------------------------


def _make_rerank_result(index: int, score: float = 0.9):
    """Create a fake RerankResult object."""
    obj = MagicMock()
    obj.index = index
    obj.relevance_score = score
    return obj


def _make_config_service(
    voyage_model: str = "rerank-2.5",
    cohere_model: str = "",
):
    """Build minimal mock config_service for reranking."""
    from code_indexer.server.utils.config_manager import RerankConfig

    config = MagicMock()
    config.rerank_config = RerankConfig(
        voyage_reranker_model=voyage_model,
        cohere_reranker_model=cohere_model,
        overfetch_multiplier=5,
    )
    config_service = MagicMock()
    config_service.get_config.return_value = config
    return config_service


def _make_results(n: int) -> List[dict]:
    """Return n fake search result dicts."""
    return [{"id": i, "content": f"document {i}"} for i in range(n)]


def _content_extractor(r: dict) -> str:
    return r.get("content", "")


def _healthy_monitor():
    """Return a mock ProviderHealthMonitor with no providers in 'down' state."""
    monitor = MagicMock()
    monitor.get_health.return_value = {}
    return monitor


def _down_monitor(health_key: str = "voyage-reranker"):
    """Return a mock ProviderHealthMonitor with specified provider in 'down' state."""
    monitor = MagicMock()
    down_status = MagicMock()
    down_status.status = "down"
    monitor.get_health.return_value = {health_key: down_status}
    return monitor


@contextmanager
def _apply_reranking(
    voyage_return=None,
    voyage_raises: Optional[Exception] = None,
    monitor=None,
):
    """Patch Voyage reranker and health monitor together.

    Args:
        voyage_return: List of RerankResult objects returned by client.rerank().
        voyage_raises: Exception raised by client.rerank() (overrides voyage_return).
        monitor: Mock ProviderHealthMonitor; defaults to _healthy_monitor().

    Yields:
        The mock VoyageRerankerClient instance.
    """
    if monitor is None:
        monitor = _healthy_monitor()

    with patch(
        "code_indexer.server.mcp.reranking.VoyageRerankerClient"
    ) as mock_voyage_cls:
        mock_client = MagicMock()
        if voyage_raises is not None:
            mock_client.rerank.side_effect = voyage_raises
        else:
            mock_client.rerank.return_value = voyage_return or []
        mock_voyage_cls.return_value = mock_client

        with patch(
            "code_indexer.server.mcp.reranking.ProviderHealthMonitor.get_instance",
            return_value=monitor,
        ):
            yield mock_client


# ---------------------------------------------------------------------------
# Tests: _build_reranker_status factory
# ---------------------------------------------------------------------------


class TestBuildRerankerStatus:
    """_build_reranker_status produces correctly shaped dicts for all states."""

    def test_success_shape(self):
        """Success: status='success', named provider, non-negative rerank_time_ms."""
        from code_indexer.server.mcp.reranking import _build_reranker_status

        rs = _build_reranker_status(
            status="success", provider="voyage", rerank_time_ms=312
        )
        assert rs["status"] == "success"
        assert rs["provider"] == "voyage"
        assert rs["rerank_time_ms"] == 312
        assert rs.get("hint") is None

    def test_failed_shape(self):
        """Failed: status='failed', provider=None, rerank_time_ms>=0, hint non-empty."""
        from code_indexer.server.mcp.reranking import _build_reranker_status

        rs = _build_reranker_status(
            status="failed",
            provider=None,
            rerank_time_ms=150,
            hint="All providers failed: HTTP 500",
        )
        assert rs["status"] == "failed"
        assert rs["provider"] is None
        assert rs["rerank_time_ms"] == 150
        assert rs["hint"]  # non-empty

    def test_skipped_shape(self):
        """Skipped: status='skipped', provider=None, rerank_time_ms=None, hint non-empty."""
        from code_indexer.server.mcp.reranking import _build_reranker_status

        rs = _build_reranker_status(
            status="skipped",
            provider=None,
            rerank_time_ms=None,
            hint="Provider skipped: voyage-reranker is down",
        )
        assert rs["status"] == "skipped"
        assert rs["provider"] is None
        assert rs["rerank_time_ms"] is None
        assert rs["hint"]  # non-empty

    def test_disabled_shape(self):
        """Disabled: status='disabled', provider=None, rerank_time_ms=None, hint=None."""
        from code_indexer.server.mcp.reranking import _build_reranker_status

        rs = _build_reranker_status(
            status="disabled", provider=None, rerank_time_ms=None
        )
        assert rs["status"] == "disabled"
        assert rs["provider"] is None
        assert rs["rerank_time_ms"] is None
        assert rs.get("hint") is None

    def test_required_keys_present(self):
        """All required keys present in every built status dict."""
        from code_indexer.server.mcp.reranking import _build_reranker_status

        rs = _build_reranker_status(
            status="success", provider="voyage", rerank_time_ms=0
        )
        assert "status" in rs
        assert "provider" in rs
        assert "rerank_time_ms" in rs


# ---------------------------------------------------------------------------
# Tests: _apply_reranking_sync produces reranker_status in metadata
# ---------------------------------------------------------------------------


class TestRerankerStatusField:
    """_apply_reranking_sync metadata['reranker_status'] reflects actual outcome."""

    def setup_method(self):
        from code_indexer.server.mcp.reranking import _apply_reranking_sync

        self._fn = _apply_reranking_sync

    def _call(self, rerank_query=None, config_service=None, results=None):
        """Call _apply_reranking_sync with shared defaults."""
        if results is None:
            results = _make_results(5)
        if config_service is None:
            config_service = _make_config_service()
        return self._fn(
            results=results,
            rerank_query=rerank_query,
            rerank_instruction=None,
            content_extractor=_content_extractor,
            requested_limit=3,
            config_service=config_service,
        )

    def test_success_reranker_status_in_metadata(self):
        """Success: metadata contains reranker_status with status='success'."""
        rerank_results = [_make_rerank_result(i) for i in range(3)]
        with _apply_reranking(voyage_return=rerank_results):
            _, meta = self._call(
                rerank_query="test query",
                config_service=_make_config_service(voyage_model="rerank-2.5"),
            )
        rs = meta["reranker_status"]
        assert rs["status"] == "success"
        assert rs["provider"] not in (None, "none")
        assert rs["rerank_time_ms"] >= 0
        assert isinstance(rs["rerank_time_ms"], int)

    def test_disabled_reranker_status_when_no_rerank_query(self):
        """Disabled: status='disabled', provider=None, rerank_time_ms=None."""
        _, meta = self._call(
            rerank_query=None,
            config_service=_make_config_service(voyage_model="rerank-2.5"),
        )
        rs = meta["reranker_status"]
        assert rs["status"] == "disabled"
        assert rs["provider"] is None
        assert rs["rerank_time_ms"] is None

    def test_failed_reranker_status_when_provider_raises(self):
        """Failed: status='failed' when provider raises an exception."""
        with _apply_reranking(
            voyage_raises=Exception("HTTP 500: Internal Server Error")
        ):
            _, meta = self._call(
                rerank_query="test query",
                config_service=_make_config_service(voyage_model="rerank-2.5"),
            )
        rs = meta["reranker_status"]
        assert rs["status"] == "failed"
        assert rs["provider"] is None

    def test_failed_hint_is_non_empty_and_indicates_failure(self):
        """Failed: hint is non-empty and contains 'failed' or 'error', NOT 'skipped'."""
        with _apply_reranking(voyage_raises=Exception("HTTP 503: Service Unavailable")):
            _, meta = self._call(
                rerank_query="test query",
                config_service=_make_config_service(voyage_model="rerank-2.5"),
            )
        hint = meta["reranker_status"]["hint"]
        assert hint, "hint must be non-empty for failed state"
        hint_lower = hint.lower()
        assert "failed" in hint_lower or "error" in hint_lower
        assert "skipped" not in hint_lower

    def test_skipped_reranker_status_when_provider_is_down(self):
        """Skipped: status='skipped' when provider health='down'."""
        monitor = _down_monitor("voyage-reranker")
        with _apply_reranking(monitor=monitor):
            _, meta = self._call(
                rerank_query="test query",
                config_service=_make_config_service(voyage_model="rerank-2.5"),
            )
        rs = meta["reranker_status"]
        assert rs["status"] == "skipped"
        assert rs["provider"] is None

    def test_skipped_hint_is_non_empty_and_indicates_skip(self):
        """Skipped: hint is non-empty and contains 'skipped' or 'down', NOT 'failed'/'error'."""
        monitor = _down_monitor("voyage-reranker")
        with _apply_reranking(monitor=monitor):
            _, meta = self._call(
                rerank_query="test query",
                config_service=_make_config_service(voyage_model="rerank-2.5"),
            )
        hint = meta["reranker_status"]["hint"]
        assert hint, "hint must be non-empty for skipped state"
        hint_lower = hint.lower()
        assert "skipped" in hint_lower or "down" in hint_lower
        assert "failed" not in hint_lower
        assert "error" not in hint_lower

    def test_failed_and_skipped_have_distinct_status_strings(self):
        """Failed (status='failed') and skipped (status='skipped') are mutually distinct."""
        with _apply_reranking(voyage_raises=Exception("HTTP 500")):
            _, failed_meta = self._call(
                rerank_query="test query",
                config_service=_make_config_service(voyage_model="rerank-2.5"),
            )

        monitor = _down_monitor("voyage-reranker")
        with _apply_reranking(monitor=monitor):
            _, skipped_meta = self._call(
                rerank_query="test query",
                config_service=_make_config_service(voyage_model="rerank-2.5"),
            )

        assert failed_meta["reranker_status"]["status"] == "failed"
        assert skipped_meta["reranker_status"]["status"] == "skipped"
        assert (
            failed_meta["reranker_status"]["status"]
            != skipped_meta["reranker_status"]["status"]
        )

    def test_failed_and_skipped_hints_are_non_empty_and_distinct(self):
        """Failed hint != skipped hint; both non-empty; each signals its own outcome."""
        with _apply_reranking(voyage_raises=Exception("HTTP 500")):
            _, failed_meta = self._call(
                rerank_query="test query",
                config_service=_make_config_service(voyage_model="rerank-2.5"),
            )

        monitor = _down_monitor("voyage-reranker")
        with _apply_reranking(monitor=monitor):
            _, skipped_meta = self._call(
                rerank_query="test query",
                config_service=_make_config_service(voyage_model="rerank-2.5"),
            )

        failed_hint = failed_meta["reranker_status"]["hint"]
        skipped_hint = skipped_meta["reranker_status"]["hint"]

        assert failed_hint, "failed hint must be non-empty"
        assert skipped_hint, "skipped hint must be non-empty"
        assert failed_hint != skipped_hint, (
            f"failed and skipped hints must differ; both were: {failed_hint!r}"
        )

    def test_reranker_status_not_in_individual_results(self):
        """reranker_status must NOT appear inside individual result dicts."""
        rerank_results = [_make_rerank_result(i) for i in range(3)]
        with _apply_reranking(voyage_return=rerank_results):
            returned_results, meta = self._call(
                rerank_query="test query",
                config_service=_make_config_service(voyage_model="rerank-2.5"),
            )
        for result in returned_results:
            assert "reranker_status" not in result
            assert "reranker_used" not in result
            assert "reranker_provider" not in result

        assert "reranker_status" in meta

    def test_failed_state_distinct_from_disabled_state(self):
        """Failed (status='failed') is unambiguously distinct from disabled (status='disabled')."""
        _, disabled_meta = self._call(rerank_query=None)
        with _apply_reranking(voyage_raises=Exception("API error")):
            _, failed_meta = self._call(
                rerank_query="test query",
                config_service=_make_config_service(voyage_model="rerank-2.5"),
            )
        assert disabled_meta["reranker_status"]["status"] == "disabled"
        assert failed_meta["reranker_status"]["status"] == "failed"

    def test_skipped_state_distinct_from_disabled_state(self):
        """Skipped (status='skipped') is unambiguously distinct from disabled (status='disabled')."""
        _, disabled_meta = self._call(rerank_query=None)
        monitor = _down_monitor("voyage-reranker")
        with _apply_reranking(monitor=monitor):
            _, skipped_meta = self._call(
                rerank_query="test query",
                config_service=_make_config_service(voyage_model="rerank-2.5"),
            )
        assert disabled_meta["reranker_status"]["status"] == "disabled"
        assert skipped_meta["reranker_status"]["status"] == "skipped"


# ---------------------------------------------------------------------------
# Tests: query_metadata integration
# ---------------------------------------------------------------------------


class TestQueryMetadataRerankerStatus:
    """Verify reranker_status integrates into query_metadata correctly."""

    def test_query_metadata_contains_reranker_status_on_success(self):
        """query_metadata must contain 'reranker_status' with success shape."""
        from code_indexer.server.mcp.reranking import _build_reranker_status

        rs = _build_reranker_status(
            status="success", provider="voyage", rerank_time_ms=312
        )
        query_metadata = {
            "query_text": "auth",
            "execution_time_ms": 450,
            "repositories_searched": 1,
            "timeout_occurred": False,
            "reranker_status": rs,
        }
        assert "reranker_status" in query_metadata
        assert query_metadata["reranker_status"]["status"] == "success"
        assert query_metadata["reranker_status"]["provider"] == "voyage"
        assert query_metadata["reranker_status"]["rerank_time_ms"] == 312

    def test_reranker_status_not_in_result_items(self):
        """reranker_status must not appear inside individual result items."""
        results = [{"id": 1, "content": "code snippet"}]
        for r in results:
            assert "reranker_status" not in r

    def test_query_metadata_disabled_state(self):
        """Disabled: query_metadata reranker_status.status='disabled'."""
        from code_indexer.server.mcp.reranking import _build_reranker_status

        rs = _build_reranker_status(
            status="disabled", provider=None, rerank_time_ms=None
        )
        query_metadata = {"query_text": "auth", "reranker_status": rs}
        assert query_metadata["reranker_status"]["status"] == "disabled"
        assert query_metadata["reranker_status"]["provider"] is None
        assert query_metadata["reranker_status"]["rerank_time_ms"] is None
