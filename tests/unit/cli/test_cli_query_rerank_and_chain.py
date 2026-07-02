"""Unit tests for Bundle 4 (Stories #694 + #904): --rerank-query / --rerank-instruction
flags and embedder chain wiring.

Tests (names reflect exact scope of each assertion):
  1. test_rerank_flags_visible_in_query_help
  2. test_funnel_reranks_semantic_results_when_rerank_query_provided
  3. test_voyage_sinbinned_cohere_used_as_fallback_in_chain
  4. test_chain_returns_failure_tuple_when_both_providers_down
  5. test_resolver_returns_none_none_when_no_api_keys_configured

Anti-mock compliance (MESSI Rule 1):
  - ProviderHealthMonitor: real instance with file persistence (tmp_path).
  - CliRerankConfigService: real instance wrapping real GlobalCliConfig.
  - Reranker HTTP boundary patched at VoyageRerankerClient._post (returns
    MagicMock(spec=httpx.Response)) -- identical pattern to
    tests/unit/cli_search_funnel/conftest.py.
  - Module-level service lookups patched:
      - rc_module.get_config_service -> real CliRerankConfigService
      - ProviderHealthMonitor.get_instance -> real monitor
    because server reranker clients resolve these at call time.
  - Embedder chain HTTP boundary patched at provider._make_sync_request.
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

import code_indexer.server.clients.reranker_clients as rc_module


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VOYAGE_KEY = "test-voyage-key-placeholder"
_COHERE_KEY = "test-cohere-key-placeholder"

VOYAGE_DIM = 1024
COHERE_DIM = 1536
VOYAGE_STUB_VAL = 0.1
COHERE_STUB_VAL = 0.2

# How long (seconds) a sin-bin entry stays active in persistence test fixtures
SINBIN_DURATION_SECONDS = 3600


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_vector(val: float, dim: int) -> List[float]:
    return [val] * dim


def _cohere_api_response(dim: int = COHERE_DIM) -> Dict[str, Any]:
    return {"embeddings": {"float": [_stub_vector(COHERE_STUB_VAL, dim)]}}


def _write_sinbin_file(path: Path, providers: List[str]) -> None:
    """Pre-populate a sinbin persistence file with sin-binned providers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        p: {
            "sinbin_until_wall_seconds": time.time() + SINBIN_DURATION_SECONDS,
            "last_failure_kind": "sinbin",
        }
        for p in providers
    }
    path.write_text(json.dumps(state), encoding="utf-8")


def _make_voyage_rerank_response(
    body: Dict[str, Any],
    *,
    reverse_order: bool = False,
) -> MagicMock:
    """Build an httpx.Response mock for the Voyage rerank API.

    reverse_order=False: scores descend (doc[0] = highest).
    reverse_order=True:  scores ascend (last doc = highest), producing full reversal.
    """
    docs = body.get("documents", [])
    top_k = body.get("top_k", len(docs))
    n = min(top_k, len(docs))
    if reverse_order:
        # last doc gets highest score so it sorts first
        data = [
            {"index": len(docs) - 1 - i, "relevance_score": float(n - i) / n}
            for i in range(n)
        ]
    else:
        data = [{"index": i, "relevance_score": 1.0 - i * 0.1} for i in range(n)]
    data.sort(key=lambda x: x["relevance_score"], reverse=True)
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = {"data": data}
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_singleton():
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    ProviderHealthMonitor.reset_instance()
    yield
    ProviderHealthMonitor.reset_instance()


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def voyage_provider():
    """Real VoyageAIClient; each test patches _make_sync_request as needed."""
    with patch.dict(os.environ, {"VOYAGE_API_KEY": _VOYAGE_KEY}, clear=False):
        from code_indexer.config import VoyageAIConfig
        from code_indexer.services.voyage_ai import VoyageAIClient

        yield VoyageAIClient(VoyageAIConfig(), None)


@pytest.fixture
def cohere_provider():
    """Real CohereEmbeddingProvider; each test patches _make_sync_request as needed."""
    with patch.dict(os.environ, {"CO_API_KEY": _COHERE_KEY}, clear=False):
        from code_indexer.config import CohereConfig
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

        yield CohereEmbeddingProvider(CohereConfig(), None)


@pytest.fixture
def health_monitor(tmp_path):
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    return ProviderHealthMonitor(persistence_path=tmp_path / "sinbin.json")


@pytest.fixture
def global_config():
    from code_indexer.config_global import GlobalCliConfig, RerankSettings

    return GlobalCliConfig(
        rerank=RerankSettings(
            auto_populate_rerank_query=True,
            overfetch_multiplier=2,
            preferred_vendor_order=["voyage", "cohere"],
            voyage_reranker_model="rerank-2.5",
            cohere_reranker_model="rerank-v3.5",
        )
    )


# ---------------------------------------------------------------------------
# Test 1: Both Click option decorators appear in --help output.
# This confirms the @click.option decorators were added to the query command.
# ---------------------------------------------------------------------------


def test_rerank_flags_visible_in_query_help(runner):
    """--rerank-query and --rerank-instruction must appear in `cidx query --help`."""
    from code_indexer.cli import cli

    result = runner.invoke(cli, ["query", "--help"])
    assert result.exit_code == 0, f"query --help failed: {result.output}"
    assert "--rerank-query" in result.output, (
        "--rerank-query flag not found in query --help; decorator not added"
    )
    assert "--rerank-instruction" in result.output, (
        "--rerank-instruction flag not found in query --help; decorator not added"
    )


# ---------------------------------------------------------------------------
# Test 2: _apply_cli_rerank_and_filter re-orders results when rerank_query provided.
# Exercises the funnel with real CliRerankConfigService and HTTP-boundary stub.
# ---------------------------------------------------------------------------


def test_funnel_reranks_semantic_results_when_rerank_query_provided(
    health_monitor, global_config, monkeypatch
):
    """Funnel reorders semantic results according to reranker scores.

    HTTP boundary patched at VoyageRerankerClient._post using a lambda that
    returns a MagicMock(spec=httpx.Response) (via _make_voyage_rerank_response).
    Module-level service lookups (get_config_service, get_instance) wired to
    real objects so the reranker clients work end-to-end.
    rc_module is imported at module level (line: import code_indexer.server.clients.reranker_clients as rc_module).
    """
    from code_indexer.cli_search_funnel import _apply_cli_rerank_and_filter
    from code_indexer.services.cli_rerank_config_shim import CliRerankConfigService
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    # Two results; reranker will reverse their order (index 1 becomes rank 0)
    semantic_results = [
        {
            "score": 0.9,
            "payload": {"content": "def authenticate(user): ...", "path": "auth.py"},
        },
        {
            "score": 0.8,
            "payload": {"content": "def login(creds): ...", "path": "login.py"},
        },
    ]

    monkeypatch.setenv("VOYAGE_API_KEY", _VOYAGE_KEY)
    shim = CliRerankConfigService(global_config)

    # Wire module-level service lookups used by reranker clients
    monkeypatch.setattr(
        ProviderHealthMonitor, "get_instance", staticmethod(lambda: health_monitor)
    )
    monkeypatch.setattr(rc_module, "get_config_service", lambda: shim)

    # Patch the HTTP boundary: _post returns MagicMock(spec=httpx.Response)
    # _make_voyage_rerank_response() returns MagicMock(spec=httpx.Response) with
    # json() returning {"data": [...]} in reversed order so login.py sorts first.
    monkeypatch.setattr(
        rc_module.VoyageRerankerClient,
        "_post",
        lambda self_c, b: _make_voyage_rerank_response(b, reverse_order=True),
    )

    reranked = _apply_cli_rerank_and_filter(
        results=semantic_results,
        rerank_query="authentication logic",
        rerank_instruction=None,
        config=shim,
        user_limit=2,
        health_monitor=health_monitor,
    )

    assert len(reranked) == 2
    # With reverse_order=True, doc[1] (login.py) is scored higher and sorts first
    assert reranked[0]["payload"]["path"] == "login.py", (
        f"Expected login.py first after rerank, got {reranked[0]['payload']['path']}"
    )


# ---------------------------------------------------------------------------
# Test 3: Sin-binned voyage causes chain to fall over to cohere.
# Persistence file pre-populated; only Cohere HTTP boundary stub is exercised.
# ---------------------------------------------------------------------------


def test_voyage_sinbinned_cohere_used_as_fallback_in_chain(
    voyage_provider, cohere_provider, tmp_path
):
    """_run_embedder_chain skips sin-binned Voyage and succeeds via Cohere.

    Writes a real persistence file with 'voyage-embedder' sin-binned.
    ProviderHealthMonitor reads this file at construction (Story #691).
    Voyage's _make_sync_request raises AssertionError to catch any accidental call.
    Cohere's _make_sync_request returns a valid embedding response.
    """
    from code_indexer.services.embedder_chain import _run_embedder_chain
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    sinbin_path = tmp_path / "sinbin.json"
    _write_sinbin_file(sinbin_path, ["voyage-embedder"])
    monitor = ProviderHealthMonitor(persistence_path=sinbin_path)

    with (
        patch.object(
            voyage_provider,
            "_make_sync_request",
            side_effect=AssertionError("Voyage must not be called when sin-binned"),
        ),
        patch.object(
            cohere_provider,
            "_make_sync_request",
            return_value=_cohere_api_response(),
        ),
    ):
        vector, provider_name, failure_reason, elapsed_ms, _outcomes = (
            _run_embedder_chain(
                text="authentication query",
                embedding_purpose="query",
                primary_provider=voyage_provider,
                secondary_provider=cohere_provider,
                health_monitor=monitor,
            )
        )

    assert vector is not None, "Expected a vector from Cohere fallback"
    assert provider_name == "cohere", (
        f"Expected 'cohere' as the fallback provider, got {provider_name!r}"
    )
    assert failure_reason is None


# ---------------------------------------------------------------------------
# Test 4: Both providers raise exceptions -> chain returns total-failure tuple.
# ---------------------------------------------------------------------------


def test_chain_returns_failure_tuple_when_both_providers_down(
    voyage_provider, cohere_provider, health_monitor
):
    """_run_embedder_chain returns (None, None, 'failed', ms) when both providers fail.

    The CLI caller uses this tuple to raise EmbedderUnavailableError.
    Both provider HTTP boundaries patched to raise ConnectionError.
    """
    from code_indexer.services.embedder_chain import _run_embedder_chain

    with (
        patch.object(
            voyage_provider,
            "_make_sync_request",
            side_effect=ConnectionError("Voyage unreachable"),
        ),
        patch.object(
            cohere_provider,
            "_make_sync_request",
            side_effect=ConnectionError("Cohere unreachable"),
        ),
    ):
        vector, provider_name, failure_reason, elapsed_ms, _outcomes = (
            _run_embedder_chain(
                text="foo",
                embedding_purpose="query",
                primary_provider=voyage_provider,
                secondary_provider=cohere_provider,
                health_monitor=health_monitor,
            )
        )

    assert vector is None, "Expected None vector on total failure"
    assert provider_name is None, "Expected None provider name on total failure"
    assert failure_reason == "failed", (
        f"Expected 'failed' reason, got {failure_reason!r}"
    )
    assert isinstance(elapsed_ms, int) and elapsed_ms >= 0


# ---------------------------------------------------------------------------
# Test 5: No API keys -> resolver returns (None, None).
# This is the precondition for the CLI to surface "no providers configured" error.
# ---------------------------------------------------------------------------


def test_resolver_returns_none_none_when_no_api_keys_configured():
    """_resolve_embedder_providers returns (None, None) when both API keys are absent.

    This is the contract the CLI wiring relies on to detect the "no providers"
    case and exit with a clean error before invoking the chain.
    """
    clean_env = {
        k: v for k, v in os.environ.items() if k not in ("VOYAGE_API_KEY", "CO_API_KEY")
    }

    with patch.dict(os.environ, clean_env, clear=True):
        from code_indexer.services.embedder_provider_resolver import (
            _resolve_embedder_providers,
        )

        primary, secondary = _resolve_embedder_providers()

    assert primary is None, (
        f"Expected None primary when no keys set, got {type(primary)}"
    )
    assert secondary is None, (
        f"Expected None secondary when no keys set, got {type(secondary)}"
    )
