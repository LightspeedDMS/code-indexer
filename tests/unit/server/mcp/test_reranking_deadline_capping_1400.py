"""Story #1400 CRITICAL 5 dynamic half: reranking.py deadline-aware capping.

The foreground temporal waiter propagates a response_deadline into the
terminal rerank call so a slow/retrying reranker call can never blow the
outer protocol-level handler timeout. `_apply_reranking_sync` /
`_run_provider_chain` / `_attempt_provider_rerank` gain an optional
`deadline_monotonic` parameter:
  - caps each provider's HTTP client timeout to the remaining time before
    the deadline (never more than the configured timeout_seconds)
  - skips starting a second/third provider attempt entirely once the
    deadline has passed
  - caps execute_with_backoff's cumulative 429-retry sleep budget by
    remaining time

deadline_monotonic=None (the default) preserves byte-identical pre-#1400
behavior for every existing caller.
"""

import time
from typing import List
from unittest.mock import MagicMock, patch

import pytest


def _make_results(n: int) -> List[dict]:
    return [{"id": i, "content": f"document {i}"} for i in range(n)]


def _content_extractor(r: dict) -> str:
    return r.get("content", "")  # type: ignore[no-any-return]


def _make_rerank_result(index: int, score: float):
    obj = MagicMock()
    obj.index = index
    obj.relevance_score = score
    return obj


def _make_config_service(
    voyage_model: str = "rerank-2.5",
    cohere_model: str = "rerank-v3.5",
):
    from code_indexer.server.utils.config_manager import RerankConfig

    config = MagicMock()
    config.rerank_config = RerankConfig(
        voyage_reranker_model=voyage_model,
        cohere_reranker_model=cohere_model,
        overfetch_multiplier=5,
    )
    config.claude_integration_config.voyageai_api_key = "voyage-key"
    config.claude_integration_config.cohere_api_key = "cohere-key"
    config.search_timeouts_config = None  # exercise the pre-#1398 15.0 fallback

    config_service = MagicMock()
    config_service.get_config.return_value = config
    return config_service


class TestDeadlineCapsProviderTimeout:
    def test_far_future_deadline_uses_configured_timeout_unchanged(self) -> None:
        """A deadline far in the future must not shrink the base timeout."""
        from code_indexer.server.mcp.reranking import _run_provider_chain

        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            instance = MockVoyage.return_value
            instance.rerank.return_value = [_make_rerank_result(0, 0.9)]
            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = False

            far_future = time.monotonic() + 9999.0
            _run_provider_chain(
                voyage_model="rerank-2.5",
                cohere_model="rerank-v3.5",
                query="q",
                documents=["doc0"],
                instruction=None,
                top_k=1,
                timeout_seconds=15.0,
                deadline_monotonic=far_future,
            )

            # client constructed with (approximately) the full 15.0s budget,
            # not clipped down toward zero by the (huge) remaining time.
            _, kwargs = MockVoyage.call_args
            assert kwargs["timeout"] == pytest.approx(15.0, abs=0.01)

    def test_near_deadline_clips_provider_timeout_to_remaining(self) -> None:
        """A near deadline must cap the provider client timeout below the
        configured value."""
        from code_indexer.server.mcp.reranking import _run_provider_chain

        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            instance = MockVoyage.return_value
            instance.rerank.return_value = [_make_rerank_result(0, 0.9)]
            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = False

            near_deadline = time.monotonic() + 2.0
            _run_provider_chain(
                voyage_model="rerank-2.5",
                cohere_model="rerank-v3.5",
                query="q",
                documents=["doc0"],
                instruction=None,
                top_k=1,
                timeout_seconds=15.0,
                deadline_monotonic=near_deadline,
            )

            _, kwargs = MockVoyage.call_args
            assert kwargs["timeout"] < 15.0
            assert kwargs["timeout"] <= 2.01

    def test_none_deadline_preserves_pre_1400_behavior(self) -> None:
        """deadline_monotonic=None (default) must not change the timeout at
        all -- byte-identical to every pre-#1400 caller."""
        from code_indexer.server.mcp.reranking import _run_provider_chain

        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            instance = MockVoyage.return_value
            instance.rerank.return_value = [_make_rerank_result(0, 0.9)]
            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = False

            _run_provider_chain(
                voyage_model="rerank-2.5",
                cohere_model="rerank-v3.5",
                query="q",
                documents=["doc0"],
                instruction=None,
                top_k=1,
                timeout_seconds=15.0,
            )

            _, kwargs = MockVoyage.call_args
            assert kwargs["timeout"] == 15.0


class TestDeadlineSkipsRemainingProviders:
    def test_deadline_already_passed_skips_second_provider(self) -> None:
        """Voyage fails; by the time Cohere would be attempted the deadline
        has already passed -- Cohere must never be called.

        Note: _run_provider_chain's provider list is exactly
        [Voyage, Cohere] (reranking.py:227-230) -- there is no third
        provider hop to cover. This test proves the ONLY remaining hop
        (Cohere, after Voyage) is skipped once the deadline has passed,
        which is full coverage of the "skip remaining providers" behavior
        for this two-provider chain.
        """
        from code_indexer.server.mcp.reranking import _run_provider_chain

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
            MockVoyage.return_value.rerank.side_effect = RuntimeError("boom")
            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = False

            already_past = time.monotonic() - 1.0
            scored, provider, failure_reason, _ = _run_provider_chain(
                voyage_model="rerank-2.5",
                cohere_model="rerank-v3.5",
                query="q",
                documents=["doc0"],
                instruction=None,
                top_k=1,
                timeout_seconds=15.0,
                deadline_monotonic=already_past,
            )

            assert scored is None
            MockCohere.return_value.rerank.assert_not_called()

    def test_deadline_passed_before_first_provider_skips_it_too(self) -> None:
        """An already-past deadline must skip even the FIRST provider
        attempt -- Voyage itself must never be called either."""
        from code_indexer.server.mcp.reranking import _run_provider_chain

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
            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = False

            already_past = time.monotonic() - 5.0
            scored, provider, failure_reason, _ = _run_provider_chain(
                voyage_model="rerank-2.5",
                cohere_model="rerank-v3.5",
                query="q",
                documents=["doc0"],
                instruction=None,
                top_k=1,
                timeout_seconds=15.0,
                deadline_monotonic=already_past,
            )

            assert scored is None
            MockVoyage.return_value.rerank.assert_not_called()
            MockCohere.return_value.rerank.assert_not_called()


class TestDeadlineCapsBackoffBudget:
    def test_deadline_caps_cumulative_backoff_sleep_budget(self) -> None:
        """execute_with_backoff's cumulative_cap (429-retry sleep budget)
        must be capped to the remaining time before the deadline, not the
        provider_backoff module's much larger default."""
        from code_indexer.server.mcp.reranking import _attempt_provider_rerank
        from code_indexer.services.provider_health_monitor import (
            ProviderHealthMonitor,
        )

        captured_kwargs = {}

        def _fake_execute_with_backoff(fn, **kwargs):
            captured_kwargs.update(kwargs)
            return fn()

        with (
            patch(
                "code_indexer.services.provider_backoff.execute_with_backoff",
                side_effect=_fake_execute_with_backoff,
            ),
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            MockMonitor.get_instance.return_value = ProviderHealthMonitor()
            client_cls = MagicMock()
            client_cls.return_value.rerank.return_value = [_make_rerank_result(0, 0.9)]

            deadline = time.monotonic() + 3.0
            _attempt_provider_rerank(
                provider_name="Voyage",
                health_key="voyage-reranker",
                client_cls=client_cls,
                query="q",
                documents=["doc0"],
                instruction=None,
                top_k=1,
                monitor=MockMonitor.get_instance.return_value,
                timeout_seconds=15.0,
                deadline_monotonic=deadline,
            )

            assert "cumulative_cap" in captured_kwargs
            assert captured_kwargs["cumulative_cap"] <= 3.01

    def test_no_deadline_omits_cumulative_cap_override(self) -> None:
        """deadline_monotonic=None must NOT pass cumulative_cap at all --
        preserves the provider_backoff module's own default exactly, byte
        identical to every pre-#1400 caller."""
        from code_indexer.server.mcp.reranking import _attempt_provider_rerank
        from code_indexer.services.provider_health_monitor import (
            ProviderHealthMonitor,
        )

        captured_kwargs = {}

        def _fake_execute_with_backoff(fn, **kwargs):
            captured_kwargs.update(kwargs)
            return fn()

        with (
            patch(
                "code_indexer.services.provider_backoff.execute_with_backoff",
                side_effect=_fake_execute_with_backoff,
            ),
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            MockMonitor.get_instance.return_value = ProviderHealthMonitor()
            client_cls = MagicMock()
            client_cls.return_value.rerank.return_value = [_make_rerank_result(0, 0.9)]

            _attempt_provider_rerank(
                provider_name="Voyage",
                health_key="voyage-reranker",
                client_cls=client_cls,
                query="q",
                documents=["doc0"],
                instruction=None,
                top_k=1,
                monitor=MockMonitor.get_instance.return_value,
                timeout_seconds=15.0,
            )

            assert "cumulative_cap" not in captured_kwargs


class TestApplyRerankingSyncThreadsDeadline:
    def test_apply_reranking_sync_accepts_and_forwards_deadline(self) -> None:
        from code_indexer.server.mcp.reranking import _apply_reranking_sync

        results = _make_results(2)
        config_service = _make_config_service()

        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            instance = MockVoyage.return_value
            instance.rerank.return_value = [_make_rerank_result(0, 0.9)]
            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = False

            deadline = time.monotonic() + 5.0
            returned, meta = _apply_reranking_sync(
                results=results,
                rerank_query="q",
                rerank_instruction=None,
                content_extractor=_content_extractor,
                requested_limit=1,
                config_service=config_service,
                deadline_monotonic=deadline,
            )

            assert len(returned) == 1
            _, kwargs = MockVoyage.call_args
            assert kwargs["timeout"] <= 5.01


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
