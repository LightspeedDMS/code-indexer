"""
Bug #899: EmbeddingProviderFactory.create() must propagate http_client_factory
to VoyageAIClient and CohereEmbeddingProvider so that FaultInjectingSyncTransport
can intercept query-time embedding HTTP calls.

Without this fix, fault profiles are accepted by the control plane but the
embedded providers fall back to NullFaultFactory, so fault history stays empty
during query-path tests.

TDD: these tests were written BEFORE the production fix.
"""

from __future__ import annotations

import random
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.fault_injection.fault_injection_service import (
    FaultInjectionService,
)
from code_indexer.server.fault_injection.fault_injecting_sync_transport import (
    FaultInjectingSyncTransport,
)
from code_indexer.server.fault_injection.http_client_factory import HttpClientFactory
from code_indexer.server.fault_injection.null_factory import NullFaultFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEED = 42

# (provider_name, env_var, env_value)
_PROVIDER_PARAMS = [
    pytest.param("voyage-ai", "VOYAGE_API_KEY", "test-key-voyage", id="voyage-ai"),
    pytest.param("cohere", "CO_API_KEY", "test-key-cohere", id="cohere"),
]


def _make_service() -> FaultInjectionService:
    return FaultInjectionService(enabled=True, rng=random.Random(_SEED))


def _make_config(provider: str) -> Any:
    """Build a minimal CLI Config-like object for the given provider."""
    from code_indexer.config import VoyageAIConfig, CohereConfig

    cfg = MagicMock(spec=[])
    cfg.embedding_provider = provider
    if provider == "voyage-ai":
        cfg.voyage_ai = VoyageAIConfig()
    else:
        cfg.cohere = CohereConfig()
    return cfg


# ---------------------------------------------------------------------------
# Tests: EmbeddingProviderFactory.create() propagates http_client_factory
# ---------------------------------------------------------------------------


class TestFactoryCreatePropagatesInjectedFactory:
    """
    Bug #899: EmbeddingProviderFactory.create() must accept http_client_factory
    and propagate it to VoyageAIClient / CohereEmbeddingProvider.

    Before the fix, create() had no http_client_factory parameter, so providers
    always fell back to NullFaultFactory regardless of the caller's intent.
    """

    @pytest.mark.parametrize("provider,env_var,env_val", _PROVIDER_PARAMS)
    def test_injected_factory_is_stored_on_provider(
        self, provider: str, env_var: str, env_val: str
    ) -> None:
        """create() with an explicit http_client_factory stores it on the provider.

        The returned provider's _http_client_factory must be the injected factory,
        not the NullFaultFactory that constructors fall back to when None is passed.
        This is the core Bug #899 regression guard.
        """
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        injected_factory = HttpClientFactory(fault_injection_service=_make_service())
        config = _make_config(provider)

        with patch.dict("os.environ", {env_var: env_val}):
            result = EmbeddingProviderFactory.create(
                config=config,
                http_client_factory=injected_factory,
            )

        assert result._http_client_factory is injected_factory, (
            f"Bug #899: EmbeddingProviderFactory.create() did not propagate "
            f"http_client_factory to the {provider} provider. "
            "Fault injection cannot intercept query-time embedding HTTP calls."
        )

    @pytest.mark.parametrize("provider,env_var,env_val", _PROVIDER_PARAMS)
    def test_no_factory_arg_produces_null_factory(
        self, provider: str, env_var: str, env_val: str
    ) -> None:
        """create() without http_client_factory produces NullFaultFactory (safe default).

        CLI and non-server paths that do not need fault injection must still get
        a concrete factory (NullFaultFactory) — never a bare None.
        """
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        config = _make_config(provider)

        with patch.dict("os.environ", {env_var: env_val}):
            result = EmbeddingProviderFactory.create(config=config)

        assert isinstance(result._http_client_factory, NullFaultFactory)

    @pytest.mark.parametrize("provider,env_var,env_val", _PROVIDER_PARAMS)
    def test_fault_factory_enables_sync_transport_interception(
        self, provider: str, env_var: str, env_val: str
    ) -> None:
        """End-to-end: injected fault factory makes create_sync_client() install
        FaultInjectingSyncTransport, proving embedding HTTP calls can be intercepted.

        This is the empirical proof that Bug #899 is fixed: when an
        HttpClientFactory with an enabled FaultInjectionService is threaded
        through EmbeddingProviderFactory.create(), the provider's HTTP clients
        carry the fault transport — making kill profiles visible to queries.
        """
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        injected_factory = HttpClientFactory(fault_injection_service=_make_service())
        config = _make_config(provider)

        with patch.dict("os.environ", {env_var: env_val}):
            result = EmbeddingProviderFactory.create(
                config=config,
                http_client_factory=injected_factory,
            )

        with result._http_client_factory.create_sync_client() as sync_client:
            assert isinstance(sync_client._transport, FaultInjectingSyncTransport), (
                f"Bug #899: provider '{provider}' sync client does not carry "
                "FaultInjectingSyncTransport even after factory was injected. "
                "Fault profiles will not intercept embedding calls."
            )
