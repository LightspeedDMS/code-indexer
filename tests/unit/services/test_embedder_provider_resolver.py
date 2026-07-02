"""Unit tests for Story #904: embedder_provider_resolver._resolve_embedder_providers().

Covers all 4 env-var combinations from the truth table in the story spec.

Anti-mock: real provider instances when keys are set (HTTP boundary patched).
Real ProviderHealthMonitor with tmp persistence.
"""

import os
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VOYAGE_KEY = "test-voyage-key-placeholder"
_COHERE_KEY = "test-cohere-key-placeholder"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolver():
    """Import resolver fresh on each call (avoids module-level import side effects)."""
    from code_indexer.services.embedder_provider_resolver import (
        _resolve_embedder_providers,
    )

    return _resolve_embedder_providers


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestResolveEmbedderProviders:
    """Truth-table tests for _resolve_embedder_providers."""

    def test_both_keys_set_returns_voyage_primary_cohere_secondary(self, tmp_path):
        """VOYAGE_API_KEY + COHERE_API_KEY -> (VoyageAIClient, CohereEmbeddingProvider)."""
        env = {"VOYAGE_API_KEY": _VOYAGE_KEY, "CO_API_KEY": _COHERE_KEY}
        with patch.dict(os.environ, env, clear=False):
            primary, secondary = _resolver()()

        from code_indexer.services.voyage_ai import VoyageAIClient
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

        assert isinstance(primary, VoyageAIClient), (
            f"Expected VoyageAIClient as primary, got {type(primary)}"
        )
        assert isinstance(secondary, CohereEmbeddingProvider), (
            f"Expected CohereEmbeddingProvider as secondary, got {type(secondary)}"
        )

    def test_voyage_only_returns_voyage_primary_none_secondary(self, tmp_path):
        """VOYAGE_API_KEY set, CO_API_KEY unset -> (VoyageAIClient, None)."""
        env = {"VOYAGE_API_KEY": _VOYAGE_KEY}
        # Ensure CO_API_KEY is absent
        without = {k: v for k, v in os.environ.items() if k != "CO_API_KEY"}
        without.update(env)
        with patch.dict(os.environ, without, clear=True):
            primary, secondary = _resolver()()

        from code_indexer.services.voyage_ai import VoyageAIClient

        assert isinstance(primary, VoyageAIClient)
        assert secondary is None

    def test_cohere_only_returns_cohere_primary_none_secondary(self, tmp_path):
        """CO_API_KEY set, VOYAGE_API_KEY unset -> (CohereEmbeddingProvider, None)."""
        env = {"CO_API_KEY": _COHERE_KEY}
        without = {k: v for k, v in os.environ.items() if k != "VOYAGE_API_KEY"}
        without.update(env)
        with patch.dict(os.environ, without, clear=True):
            primary, secondary = _resolver()()

        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

        assert isinstance(primary, CohereEmbeddingProvider)
        assert secondary is None

    def test_no_keys_returns_none_none(self, tmp_path):
        """Both keys absent -> (None, None)."""
        without = {
            k: v
            for k, v in os.environ.items()
            if k not in ("VOYAGE_API_KEY", "CO_API_KEY")
        }
        with patch.dict(os.environ, without, clear=True):
            primary, secondary = _resolver()()

        assert primary is None
        assert secondary is None
