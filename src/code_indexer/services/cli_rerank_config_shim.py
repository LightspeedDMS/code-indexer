"""CliRerankConfigService -- duck-typed config facade for reusing server
reranker pipeline code in the CLI without importing the server's
database-backed ConfigService.

Story #692 of Epic #689.

Attribute surface audit
-----------------------
The shim satisfies every attribute access made by the server reranking code:

reranking.py (_load_provider_models, _apply_reranking_sync):
  config_service.get_config()                       -> _ConfigShim
  cfg.rerank_config                                 -> _RerankConfigShim
  rc.voyage_reranker_model                          -> str
  rc.cohere_reranker_model                          -> str

reranker_clients.py (VoyageRerankerClient / CohereRerankerClient):
  get_config_service().get_config()                 -> same _ConfigShim surface
  config.claude_integration_config.voyageai_api_key -> Optional[str]
  config.claude_integration_config.cohere_api_key   -> Optional[str]
  config.rerank_config.voyage_reranker_model         -> str
  config.rerank_config.cohere_reranker_model         -> str

Design rules (Story #692 Must NOT):
  - No import from code_indexer.server.*
  - No file I/O (caller supplies a loaded GlobalCliConfig)
  - No env var mutation
  - API keys read at construction time for deterministic test behaviour
"""

import os
from dataclasses import dataclass
from typing import List, Optional

from code_indexer.config_global import GlobalCliConfig


# ---------------------------------------------------------------------------
# Internal dataclasses matching the server config_service attribute surface.
# Only fields actually accessed by reranker_clients.py and reranking.py.
# ---------------------------------------------------------------------------


@dataclass
class _RerankConfigShim:
    """Mirrors the fields of server RerankConfig that reranking.py reads."""

    voyage_reranker_model: str
    cohere_reranker_model: str
    overfetch_multiplier: int
    preferred_vendor_order: List[str]


@dataclass
class _ClaudeIntegrationConfigShim:
    """Mirrors ClaudeIntegrationConfig fields that reranker_clients.py reads."""

    voyageai_api_key: Optional[str]
    cohere_api_key: Optional[str]


@dataclass
class _ConfigShim:
    """Root config object returned by get_config() -- matches server ServerConfig surface."""

    rerank_config: _RerankConfigShim
    claude_integration_config: _ClaudeIntegrationConfigShim


# ---------------------------------------------------------------------------
# Public shim class
# ---------------------------------------------------------------------------


class CliRerankConfigService:
    """Duck-typed config facade so server reranker pipeline code runs in the CLI.

    The server reranking orchestrator (_apply_reranking_sync) and the
    individual reranker clients (VoyageRerankerClient, CohereRerankerClient)
    call config_service.get_config() to read model names and API keys.
    This class satisfies that surface using:
      - GlobalCliConfig  for model names, overfetch multiplier, vendor order
      - VOYAGE_API_KEY / COHERE_API_KEY env vars for API key values

    API keys are captured once at construction time so shim instances are
    deterministic -- env var mutations after construction have no effect on
    an existing instance.
    """

    def __init__(self, global_config: GlobalCliConfig) -> None:
        """
        Args:
            global_config: Loaded GlobalCliConfig (from load_global_config or test).
                           Must be provided by the caller -- no file I/O here.

        Raises:
            TypeError: If global_config is None, lacks a .rerank attribute, or
                       if global_config.rerank is None.
        """
        if global_config is None:
            raise TypeError("global_config must not be None")
        if not hasattr(global_config, "rerank"):
            raise TypeError(
                "global_config must have a .rerank attribute (expected GlobalCliConfig)"
            )

        rerank = global_config.rerank
        if rerank is None:
            raise TypeError("global_config.rerank must not be None")

        # Read API keys once at construction time (deterministic state).
        voyageai_api_key: Optional[str] = os.environ.get("VOYAGE_API_KEY") or None
        cohere_api_key: Optional[str] = os.environ.get("COHERE_API_KEY") or None

        self._config = _ConfigShim(
            rerank_config=_RerankConfigShim(
                voyage_reranker_model=rerank.voyage_reranker_model,
                cohere_reranker_model=rerank.cohere_reranker_model,
                overfetch_multiplier=rerank.overfetch_multiplier,
                preferred_vendor_order=list(rerank.preferred_vendor_order),
            ),
            claude_integration_config=_ClaudeIntegrationConfigShim(
                voyageai_api_key=voyageai_api_key,
                cohere_api_key=cohere_api_key,
            ),
        )

    def get_config(self) -> _ConfigShim:
        """Return the config shim.  Mirrors ConfigService.get_config()."""
        return self._config

    def effective_vendor_order(self) -> List[str]:
        """Return configured vendor order filtered to vendors with API keys present.

        A vendor is included only when its corresponding API key was captured
        at construction time:
          - "voyage"  requires VOYAGE_API_KEY
          - "cohere"  requires COHERE_API_KEY

        The relative ordering follows global_config.rerank.preferred_vendor_order.
        """
        available = {
            "voyage": self._config.claude_integration_config.voyageai_api_key
            is not None,
            "cohere": self._config.claude_integration_config.cohere_api_key is not None,
        }
        return [
            vendor
            for vendor in self._config.rerank_config.preferred_vendor_order
            if available.get(vendor, False)
        ]


# ---------------------------------------------------------------------------
# Public factory and helper
# ---------------------------------------------------------------------------


def build_cli_rerank_config_service(
    global_config: GlobalCliConfig,
) -> CliRerankConfigService:
    """Factory: construct a CliRerankConfigService from a loaded GlobalCliConfig.

    Args:
        global_config: Loaded GlobalCliConfig instance.

    Returns:
        Ready-to-use CliRerankConfigService shim.

    Raises:
        TypeError: If global_config is None.
    """
    if global_config is None:
        raise TypeError("global_config must not be None")
    return CliRerankConfigService(global_config)


def is_rerank_available(shim: CliRerankConfigService) -> bool:
    """Return True when at least one reranker vendor API key is available.

    Args:
        shim: A CliRerankConfigService instance.

    Returns:
        True if VOYAGE_API_KEY or COHERE_API_KEY was set at shim construction.

    Raises:
        TypeError: If shim is None.
    """
    if shim is None:
        raise TypeError("shim must not be None")
    ci = shim.get_config().claude_integration_config
    return ci.voyageai_api_key is not None or ci.cohere_api_key is not None
