"""Unit tests for CIDXDaemonService rerank wire-up (Story #695).

Scope:
- Daemon __init__ sets _rerank_config and _health_monitor with XDG-compliant path.
- exposed_query / exposed_query_fts / exposed_query_hybrid accept rerank_query /
  rerank_instruction without TypeError.
- Short-circuit path (rerank_query=None or "") returns truncated results WITHOUT
  calling the funnel.
- Funnel (_apply_cli_rerank_and_filter) is called when rerank_query is set, receives
  correct kwargs, and its return value propagates to the response.

Anti-mock strategy (MESSI Rule 1):
- Real ProviderHealthMonitor with tmp_path persistence_path.
- Real CliRerankConfigService from a real GlobalCliConfig.
- External I/O below _execute_semantic_search and _execute_fts_search is patched at
  the IMPORT boundary inside the daemon module (ConfigManager, BackendFactory,
  EmbeddingProviderFactory, TantivyIndexManager) -- these are genuine external
  boundaries, not internal SUT methods.
- _apply_cli_rerank_and_filter is the rerank network boundary; it is patched in
  tests that verify funnel invocation because the reranker clients make HTTP calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest


def _make_log_and_return(
    log: list[Dict[str, Any]], limit: Optional[int] = None
) -> Callable[..., list[Dict[str, Any]]]:
    """Return a side-effect that appends call kwargs to *log* then returns results.

    Replaces ``lambda **kw: log.append(kw) or kw["results"]`` which triggers
    mypy's func-returns-value error (list.append returns None, not a value).
    When *limit* is given, the slice ``results[:limit]`` is returned instead.
    """

    def _side_effect(**kw: Any) -> list[Dict[str, Any]]:
        log.append(kw)
        results: list[Dict[str, Any]] = kw["results"]
        return results if limit is None else results[:limit]

    return _side_effect


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_health_monitor_singleton():
    """Ensure ProviderHealthMonitor singleton is isolated between tests."""
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    ProviderHealthMonitor.reset_instance()
    yield
    ProviderHealthMonitor.reset_instance()


@pytest.fixture()
def daemon_service():
    """CIDXDaemonService with eviction thread cleaned up after test."""
    from code_indexer.daemon.service import CIDXDaemonService

    service = CIDXDaemonService()
    yield service
    service.eviction_thread.stop()
    service.eviction_thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Shared result builders
# ---------------------------------------------------------------------------


def _semantic_results(n: int = 3) -> List[Dict[str, Any]]:
    return [
        {
            "score": round(1.0 - i * 0.1, 2),
            "payload": {
                "path": f"f{i}.py",
                "content": f"content {i}",
                "line_start": i + 1,
            },
        }
        for i in range(n)
    ]


def _fts_results(n: int = 3) -> List[Dict[str, Any]]:
    return [
        {
            "path": f"f{i}.py",
            "snippet": f"snippet {i}",
            "score": round(1.0 - i * 0.1, 2),
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Shared external-boundary patch helpers (no internal SUT methods patched)
# ---------------------------------------------------------------------------


def _sem_patches(results, timing=None):
    """Patch list for the real external I/O in _execute_semantic_search.

    All lazy-imported symbols inside _execute_semantic_search are patched at
    their source modules, not at the daemon service module (which holds no
    module-level references to them).
    """
    if timing is None:
        timing = {}
    vs = MagicMock()
    vs.search.return_value = (results, timing)
    vs.resolve_collection_name.return_value = "main"

    backend = MagicMock()
    backend.get_vector_store_client.return_value = vs

    config_mgr = MagicMock()
    config_mgr.get_config.return_value = MagicMock()

    ep = MagicMock()

    return [
        patch(
            "code_indexer.config.ConfigManager.create_with_backtrack",
            return_value=config_mgr,
        ),
        patch(
            "code_indexer.backends.backend_factory.BackendFactory.create",
            return_value=backend,
        ),
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
            return_value=ep,
        ),
        patch(
            "code_indexer.remote.staleness_detector.StalenessDetector",
            side_effect=ImportError("unit-test stub"),
        ),
    ]


def _fts_patches(results):
    """Patch list for the real external I/O in _execute_fts_search.

    TantivyIndexManager is lazy-imported inside the method; patch at its source.
    """
    mgr = MagicMock()
    mgr.search.return_value = results
    return [
        patch(
            "code_indexer.services.tantivy_index_manager.TantivyIndexManager",
            return_value=mgr,
        )
    ]


_FUNNEL_PATCH = "code_indexer.cli_search_funnel._apply_cli_rerank_and_filter"


def _make_fts_dir(tmp_path: Path) -> None:
    """Create the tantivy index directory that _execute_fts_search checks."""
    (tmp_path / ".code-indexer" / "tantivy_index").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# TestDaemonRerankStartupInit
# ---------------------------------------------------------------------------


class TestDaemonRerankStartupInit:
    """Daemon __init__ creates _rerank_config and _health_monitor once."""

    def test_init_sets_rerank_config_attribute(self, daemon_service):
        from code_indexer.services.cli_rerank_config_shim import CliRerankConfigService

        assert hasattr(daemon_service, "_rerank_config")
        assert isinstance(daemon_service._rerank_config, CliRerankConfigService)

    def test_init_sets_health_monitor_attribute(self, daemon_service):
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        assert hasattr(daemon_service, "_health_monitor")
        assert isinstance(daemon_service._health_monitor, ProviderHealthMonitor)

    def test_health_monitor_has_xdg_persistence_path(self, daemon_service):
        """Persistence path must end in cidx/reranker_state.json (XDG-compliant)."""
        path = daemon_service._health_monitor._persistence_path
        assert path is not None, "Persistence path must not be None"
        assert path.name == "reranker_state.json"
        assert path.parent.name == "cidx"

    def test_health_monitor_is_the_process_singleton(self, daemon_service):
        """The daemon's _health_monitor must be the same object as the process singleton.

        ProviderHealthMonitor.get_instance() must return the exact same object that
        CIDXDaemonService stored at startup -- verifying the daemon wires the singleton,
        not a private separate instance.
        """
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        singleton = ProviderHealthMonitor.get_instance()
        assert daemon_service._health_monitor is singleton, (
            "Daemon _health_monitor must be the ProviderHealthMonitor process singleton"
        )


# ---------------------------------------------------------------------------
# TestDaemonRerankParamAcceptance
# ---------------------------------------------------------------------------


class TestDaemonRerankParamAcceptance:
    """exposed_query* methods must accept rerank_query and rerank_instruction."""

    def test_exposed_query_accepts_rerank_params(self, daemon_service, tmp_path):
        results = _semantic_results(3)
        ps = _sem_patches(results)
        with ps[0], ps[1], ps[2], ps[3]:
            with patch(
                "code_indexer.daemon.service._apply_cli_rerank_and_filter",
                return_value=results,
            ):
                response = daemon_service.exposed_query(
                    str(tmp_path),
                    "q",
                    limit=5,
                    rerank_query="q",
                    rerank_instruction=None,
                )
        assert "results" in response

    def test_exposed_query_fts_accepts_rerank_params(self, daemon_service, tmp_path):
        results = _fts_results(3)
        _make_fts_dir(tmp_path)
        ps = _fts_patches(results)
        with ps[0]:
            with patch(
                "code_indexer.daemon.service._apply_cli_rerank_and_filter",
                return_value=results,
            ):
                result = daemon_service.exposed_query_fts(
                    str(tmp_path),
                    "q",
                    rerank_query="q",
                    rerank_instruction=None,
                )
        assert isinstance(result, list)

    def test_exposed_query_hybrid_accepts_rerank_params(self, daemon_service, tmp_path):
        sem = _semantic_results(3)
        fts = _fts_results(3)
        _make_fts_dir(tmp_path)
        sp = _sem_patches(sem)
        fp = _fts_patches(fts)
        with sp[0], sp[1], sp[2], sp[3]:
            with fp[0]:
                with patch(
                    "code_indexer.daemon.service._apply_cli_rerank_and_filter",
                    return_value=sem[:2],
                ):
                    result = daemon_service.exposed_query_hybrid(
                        str(tmp_path),
                        "q",
                        rerank_query="q",
                        rerank_instruction="Be precise.",
                    )
        assert "semantic" in result
        assert "fts" in result


# ---------------------------------------------------------------------------
# TestDaemonRerankShortCircuit
# ---------------------------------------------------------------------------


class TestDaemonRerankShortCircuit:
    """When rerank_query is None or '', funnel is skipped and results truncated."""

    def test_funnel_not_called_and_results_truncated_when_rerank_query_none(
        self, daemon_service, tmp_path
    ):
        results = _semantic_results(5)
        call_log: list[Dict[str, Any]] = []
        ps = _sem_patches(results)
        with ps[0], ps[1], ps[2], ps[3]:
            with patch(
                "code_indexer.daemon.service._apply_cli_rerank_and_filter",
                side_effect=_make_log_and_return(call_log),
            ):
                response = daemon_service.exposed_query(
                    str(tmp_path),
                    "q",
                    limit=2,
                    rerank_query=None,
                )
        assert call_log == [], "Funnel must not be called when rerank_query is None"
        assert len(response["results"]) == 2, "Results must be truncated to limit=2"

    def test_funnel_not_called_and_results_truncated_when_rerank_query_empty(
        self, daemon_service, tmp_path
    ):
        results = _semantic_results(5)
        call_log: list[Dict[str, Any]] = []
        ps = _sem_patches(results)
        with ps[0], ps[1], ps[2], ps[3]:
            with patch(
                "code_indexer.daemon.service._apply_cli_rerank_and_filter",
                side_effect=_make_log_and_return(call_log),
            ):
                response = daemon_service.exposed_query(
                    str(tmp_path),
                    "q",
                    limit=3,
                    rerank_query="",
                )
        assert call_log == [], "Funnel must not be called when rerank_query is empty"
        assert len(response["results"]) == 3, "Results must be truncated to limit=3"


# ---------------------------------------------------------------------------
# TestDaemonRerankFunnelInvocation
# ---------------------------------------------------------------------------


class TestDaemonRerankFunnelInvocation:
    """Funnel is called with correct kwargs and its return value propagates."""

    def test_exposed_query_calls_funnel_with_correct_kwargs(
        self, daemon_service, tmp_path
    ):
        results = _semantic_results(5)
        call_log: list[Dict[str, Any]] = []
        ps = _sem_patches(results)
        with ps[0], ps[1], ps[2], ps[3]:
            with patch(
                "code_indexer.daemon.service._apply_cli_rerank_and_filter",
                side_effect=_make_log_and_return(call_log, limit=2),
            ):
                response = daemon_service.exposed_query(
                    str(tmp_path),
                    "search text",
                    limit=2,
                    rerank_query="search text",
                    rerank_instruction="Rank by relevance.",
                )

        assert len(call_log) == 1
        call = call_log[0]
        assert call["rerank_query"] == "search text"
        assert call["rerank_instruction"] == "Rank by relevance."
        assert call["user_limit"] == 2
        assert len(response["results"]) == 2

    def test_exposed_query_fts_calls_funnel_with_correct_kwargs(
        self, daemon_service, tmp_path
    ):
        results = _fts_results(4)
        call_log: list[Dict[str, Any]] = []
        _make_fts_dir(tmp_path)
        ps = _fts_patches(results)
        with ps[0]:
            with patch(
                "code_indexer.daemon.service._apply_cli_rerank_and_filter",
                side_effect=_make_log_and_return(call_log, limit=1),
            ):
                result = daemon_service.exposed_query_fts(
                    str(tmp_path),
                    "fts text",
                    rerank_query="fts text",
                    rerank_instruction=None,
                    limit=1,
                )

        assert len(call_log) == 1
        assert call_log[0]["rerank_query"] == "fts text"
        assert call_log[0]["rerank_instruction"] is None
        assert len(result) == 1

    def test_exposed_query_hybrid_calls_funnel_twice_with_correct_kwargs(
        self, daemon_service, tmp_path
    ):
        """Hybrid mode calls funnel once for semantic and once for FTS, both with correct kwargs."""
        sem = _semantic_results(4)
        fts = _fts_results(4)
        call_log: list[Dict[str, Any]] = []
        _make_fts_dir(tmp_path)
        sp = _sem_patches(sem)
        fp = _fts_patches(fts)
        with sp[0], sp[1], sp[2], sp[3]:
            with fp[0]:
                with patch(
                    "code_indexer.daemon.service._apply_cli_rerank_and_filter",
                    side_effect=_make_log_and_return(call_log, limit=2),
                ):
                    result = daemon_service.exposed_query_hybrid(
                        str(tmp_path),
                        "hybrid",
                        rerank_query="hybrid",
                        rerank_instruction=None,
                    )

        assert len(call_log) == 2, (
            "Funnel must be called once for semantic and once for FTS"
        )
        # Both sub-calls must carry the same rerank params
        for call in call_log:
            assert call["rerank_query"] == "hybrid", (
                f"Each funnel sub-call must have rerank_query='hybrid', got: {call.get('rerank_query')!r}"
            )
            assert call["rerank_instruction"] is None, (
                f"Each funnel sub-call must have rerank_instruction=None, got: {call.get('rerank_instruction')!r}"
            )
        assert "semantic" in result
        assert "fts" in result
