"""Tests for CliRerankConfigService shim (Story #692 — Epic #689).

The shim is a duck-typed config facade that lets the server reranking
orchestrator (_apply_reranking_sync) run in the CLI context without
importing the server's database-backed ConfigService.

Attribute surface contract (derived from reranker_clients.py and reranking.py):

From reranking.py (_load_provider_models):
  config_service.get_config()                        -> _ConfigShim
  config_shim.rerank_config                          -> _RerankConfigShim
  config_shim.rerank_config.voyage_reranker_model    -> str
  config_shim.rerank_config.cohere_reranker_model    -> str
  config_shim.rerank_config.overfetch_multiplier     -> int
  config_shim.rerank_config.preferred_vendor_order   -> List[str]

From reranker_clients.py (VoyageRerankerClient / CohereRerankerClient):
  config.claude_integration_config.voyageai_api_key  -> Optional[str]
  config.claude_integration_config.cohere_api_key    -> Optional[str]
  config.rerank_config.voyage_reranker_model         -> str
  config.rerank_config.cohere_reranker_model         -> str
"""

from typing import List, Optional

import pytest

from code_indexer.config_global import GlobalCliConfig, RerankSettings
from code_indexer.services.cli_rerank_config_shim import (
    CliRerankConfigService,
    build_cli_rerank_config_service,
    is_rerank_available,
)
from code_indexer.server.mcp.reranking import (
    _apply_reranking_sync,
    _load_provider_models,
)
import code_indexer.server.clients.reranker_clients as _rc_module
from code_indexer.server.clients.reranker_clients import (
    CohereRerankerClient,
    VoyageRerankerClient,
)


# ---------------------------------------------------------------------------
# Shared helpers and fixtures
# ---------------------------------------------------------------------------


def _make_global_config(
    voyage_model: str = "rerank-2.5",
    cohere_model: str = "rerank-v3.5",
    overfetch: int = 5,
    preferred_order: Optional[List[str]] = None,
) -> GlobalCliConfig:
    """Build a real GlobalCliConfig with explicit settings — no file I/O."""
    return GlobalCliConfig(
        rerank=RerankSettings(
            voyage_reranker_model=voyage_model,
            cohere_reranker_model=cohere_model,
            overfetch_multiplier=overfetch,
            preferred_vendor_order=(
                preferred_order if preferred_order is not None else ["voyage", "cohere"]
            ),
        )
    )


@pytest.fixture()
def make_shim(monkeypatch: pytest.MonkeyPatch):
    """Factory fixture: call make_shim(voyage_key, cohere_key, **config_kwargs).

    Sets or clears VOYAGE_API_KEY / CO_API_KEY for the test, then
    constructs and returns a CliRerankConfigService.  All **config_kwargs
    are forwarded to _make_global_config.
    """

    def _factory(
        voyage_key: Optional[str] = None,
        cohere_key: Optional[str] = None,
        **config_kwargs,
    ) -> CliRerankConfigService:
        if voyage_key is not None:
            monkeypatch.setenv("VOYAGE_API_KEY", voyage_key)
        else:
            monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        if cohere_key is not None:
            monkeypatch.setenv("CO_API_KEY", cohere_key)
        else:
            monkeypatch.delenv("CO_API_KEY", raising=False)
        return CliRerankConfigService(_make_global_config(**config_kwargs))

    return _factory


@pytest.fixture()
def default_shim(make_shim) -> CliRerankConfigService:
    """Shim with no API keys and default GlobalCliConfig."""
    return make_shim()


# ---------------------------------------------------------------------------
# get_config() attribute surface
# ---------------------------------------------------------------------------


def test_get_config_exposes_rerank_config(default_shim: CliRerankConfigService) -> None:
    assert hasattr(default_shim.get_config(), "rerank_config")


def test_get_config_exposes_claude_integration_config(
    default_shim: CliRerankConfigService,
) -> None:
    assert hasattr(default_shim.get_config(), "claude_integration_config")


@pytest.mark.parametrize(
    "attr",
    [
        "voyage_reranker_model",
        "cohere_reranker_model",
        "overfetch_multiplier",
        "preferred_vendor_order",
    ],
)
def test_rerank_config_has_required_attr(
    attr: str, default_shim: CliRerankConfigService
) -> None:
    assert hasattr(default_shim.get_config().rerank_config, attr)


@pytest.mark.parametrize("attr", ["voyageai_api_key", "cohere_api_key"])
def test_claude_integration_config_has_required_attr(
    attr: str, default_shim: CliRerankConfigService
) -> None:
    assert hasattr(default_shim.get_config().claude_integration_config, attr)


# ---------------------------------------------------------------------------
# Values sourced from GlobalCliConfig
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "voyage_model, cohere_model, overfetch, preferred_order",
    [
        ("rerank-2.5", "rerank-v3.5", 5, ["voyage", "cohere"]),
        ("rerank-2-lite", "rerank-english-v3.0", 7, ["cohere", "voyage"]),
    ],
)
def test_rerank_config_values_from_global_config(
    make_shim,
    voyage_model: str,
    cohere_model: str,
    overfetch: int,
    preferred_order: List[str],
) -> None:
    s = make_shim(
        voyage_model=voyage_model,
        cohere_model=cohere_model,
        overfetch=overfetch,
        preferred_order=preferred_order,
    )
    rc = s.get_config().rerank_config
    assert rc.voyage_reranker_model == voyage_model
    assert rc.cohere_reranker_model == cohere_model
    assert rc.overfetch_multiplier == overfetch
    assert rc.preferred_vendor_order == preferred_order


# ---------------------------------------------------------------------------
# API key env-var detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "voyage_key, cohere_key, attr, expected",
    [
        ("vkey", None, "voyageai_api_key", "vkey"),
        (None, "ckey", "cohere_api_key", "ckey"),
        (None, None, "voyageai_api_key", None),
        (None, None, "cohere_api_key", None),
        ("vkey", "ckey", "voyageai_api_key", "vkey"),
        ("vkey", "ckey", "cohere_api_key", "ckey"),
    ],
)
def test_api_key_detection(
    make_shim,
    voyage_key: Optional[str],
    cohere_key: Optional[str],
    attr: str,
    expected: Optional[str],
) -> None:
    s = make_shim(voyage_key=voyage_key, cohere_key=cohere_key)
    assert getattr(s.get_config().claude_integration_config, attr) == expected


def test_api_keys_captured_at_construction_time(
    make_shim, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env var changes after construction must not affect existing shim instance."""
    s = make_shim(voyage_key="original-key")
    # Mutate env after construction — shim must NOT see the new value.
    monkeypatch.setenv("VOYAGE_API_KEY", "changed-key")
    assert s.get_config().claude_integration_config.voyageai_api_key == "original-key"


# ---------------------------------------------------------------------------
# is_rerank_available() and effective_vendor_order()
# ---------------------------------------------------------------------------


def test_is_rerank_available_false_when_no_keys(
    default_shim: CliRerankConfigService,
) -> None:
    assert is_rerank_available(default_shim) is False


def test_is_rerank_available_true_when_voyage_key_set(make_shim) -> None:
    s = make_shim(voyage_key="vkey")
    assert is_rerank_available(s) is True


@pytest.mark.parametrize(
    "voyage_key, cohere_key, configured_order, expected_effective_order",
    [
        # Only voyage key present — effective order contains only voyage
        ("vkey", None, ["voyage", "cohere"], ["voyage"]),
        ("vkey", None, ["cohere", "voyage"], ["voyage"]),
        # Only cohere key present — effective order contains only cohere
        (None, "ckey", ["voyage", "cohere"], ["cohere"]),
        (None, "ckey", ["cohere", "voyage"], ["cohere"]),
        # Both keys present — effective order follows configured order
        ("vkey", "ckey", ["voyage", "cohere"], ["voyage", "cohere"]),
        ("vkey", "ckey", ["cohere", "voyage"], ["cohere", "voyage"]),
        # No keys — effective order empty (both configured-order variants)
        (None, None, ["voyage", "cohere"], []),
        (None, None, ["cohere", "voyage"], []),
    ],
)
def test_effective_vendor_order(
    make_shim,
    voyage_key: Optional[str],
    cohere_key: Optional[str],
    configured_order: List[str],
    expected_effective_order: List[str],
) -> None:
    s = make_shim(
        voyage_key=voyage_key,
        cohere_key=cohere_key,
        preferred_order=configured_order,
    )
    assert s.effective_vendor_order() == expected_effective_order


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def test_build_cli_rerank_config_service_returns_shim_instance(make_shim) -> None:
    s = build_cli_rerank_config_service(_make_global_config())
    assert isinstance(s, CliRerankConfigService)


# ---------------------------------------------------------------------------
# Real reranker client instantiation (server clients patched to use shim)
# ---------------------------------------------------------------------------


def test_voyage_client_constructs_with_shim_registered(
    make_shim, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = make_shim(voyage_key="test-key")
    monkeypatch.setattr(_rc_module, "get_config_service", lambda: s)
    assert VoyageRerankerClient() is not None


def test_cohere_client_constructs_with_shim_registered(
    make_shim, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = make_shim(cohere_key="test-key")
    monkeypatch.setattr(_rc_module, "get_config_service", lambda: s)
    assert CohereRerankerClient() is not None


# ---------------------------------------------------------------------------
# _apply_reranking_sync duck-type compatibility
# ---------------------------------------------------------------------------


def test_load_provider_models_via_shim(make_shim) -> None:
    s = make_shim(voyage_model="rerank-2.5", cohere_model="rerank-v3.5")
    voyage, cohere = _load_provider_models(s)
    assert voyage == "rerank-2.5"
    assert cohere == "rerank-v3.5"


def test_apply_reranking_sync_no_query_is_noop_via_shim(
    default_shim: CliRerankConfigService,
) -> None:
    docs = [{"content": "result A"}, {"content": "result B"}]
    results, meta = _apply_reranking_sync(
        results=docs,
        rerank_query=None,
        rerank_instruction=None,
        content_extractor=lambda r: r["content"],
        requested_limit=10,
        config_service=default_shim,
    )
    assert results == docs
    assert meta["reranker_used"] is False


# ---------------------------------------------------------------------------
# Shim instance independence
# ---------------------------------------------------------------------------


def test_two_shim_instances_are_independent(make_shim) -> None:
    s1 = make_shim(voyage_key="key1", voyage_model="model-A")
    s2 = make_shim(voyage_key="key2", voyage_model="model-B")

    assert s1.get_config().rerank_config.voyage_reranker_model == "model-A"
    assert s2.get_config().rerank_config.voyage_reranker_model == "model-B"
    assert s1.get_config().claude_integration_config.voyageai_api_key == "key1"
    assert s2.get_config().claude_integration_config.voyageai_api_key == "key2"
