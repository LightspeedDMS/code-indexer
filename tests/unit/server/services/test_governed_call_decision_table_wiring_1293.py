"""Story #1293 M1 (review nit): governed_call.py must route its Path-B
EmbeddingCacheMetadata (outcome, role) constructions through the SINGLE
decision-table classifier (embed_event_decision_table.decide_role_and_outcome)
instead of hardcoding the same mapping inline a second time.

These tests patch decide_role_and_outcome itself and assert governed_call's
constructed EmbeddingCacheMetadata reflects the PATCHED sentinel value at
every Path-B call site, AND that the classifier receives the correct kwargs
for that scenario. If governed_call still hardcodes literals, patching the
classifier has NO effect and these tests fail -- proving real wiring (not
just coincidentally-matching constants).
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch


def _make_provider(provider_name: str = "voyage-ai") -> MagicMock:
    provider = MagicMock()
    provider.get_provider_name.return_value = provider_name
    provider.get_embedding.return_value = [0.1, 0.2, 0.3]
    return provider


class TestGovernedCallRoutesThroughDecisionTable:
    """Every Path-B construction site must call decide_role_and_outcome()."""

    def _call(self, provider=None, text="query", **kwargs):
        from code_indexer.server.services.governed_call import (
            coalesced_query_embedding,
        )

        if provider is None:
            provider = _make_provider()
        return coalesced_query_embedding(provider, text, **kwargs)

    def _run_with_patches(
        self, patch_specs, provider, expected_outcome, expected_role, **call_kwargs
    ):
        """Apply patch_specs (dict of target->return_value), call
        coalesced_query_embedding, and return (meta, mock_classifier)."""
        with ExitStack() as stack:
            for target, value in patch_specs.items():
                stack.enter_context(patch(target, return_value=value))
            mock_classifier = stack.enter_context(
                patch(
                    "code_indexer.server.services.governed_call.decide_role_and_outcome",
                    return_value=(expected_outcome, expected_role),
                )
            )
            _vec, meta = self._call(provider, **call_kwargs)

        mock_classifier.assert_called_once()
        assert meta.outcome == expected_outcome
        assert meta.role == expected_role
        return meta, mock_classifier

    def test_no_registry_no_cache_uses_classifier(self):
        """direct_live row: cache_hit=None/cache_mode=None, no bypass/error."""
        provider = _make_provider("voyage-ai")
        _meta, mock_classifier = self._run_with_patches(
            {
                "code_indexer.server.services.governed_call.get_coalescer_registry": None,
                "code_indexer.server.services.governed_call.get_query_embedding_cache": None,
                "code_indexer.server.services.governed_call.governed_query_embedding": [
                    1.0,
                    2.0,
                ],
                "code_indexer.server.services.governed_call._digest_for_provider": "digest-no-cache",
            },
            provider,
            "SENTINEL-OUTCOME-1",
            "SENTINEL-ROLE-1",
        )
        call_kwargs = mock_classifier.call_args.kwargs
        assert call_kwargs.get("cache_hit") is None
        assert call_kwargs.get("cache_mode") is None
        # bypass/error are optional kwargs on decide_role_and_outcome (default
        # False) -- the direct_live call site must not pass bypass/error=True.
        assert call_kwargs.get("bypass", False) is False
        assert call_kwargs.get("error", False) is False

    def test_bypass_path_uses_classifier(self):
        provider = _make_provider("voyage-ai")
        mock_cache = MagicMock()
        mock_cache.enabled_for.return_value = True
        mock_cache.mode_for.return_value = "on"
        mock_cache.build_key_for_provider.return_value = "key-abc"
        mock_cache.qualifier.return_value = MagicMock()

        _meta, mock_classifier = self._run_with_patches(
            {
                "code_indexer.server.services.governed_call.get_coalescer_registry": None,
                "code_indexer.server.services.governed_call.get_query_embedding_cache": mock_cache,
                "code_indexer.server.services.governed_call._digest_for_provider": "d"
                * 10,
                "code_indexer.server.services.governed_call.governed_query_embedding": [
                    10.0,
                    11.0,
                ],
            },
            provider,
            "SENTINEL-OUTCOME-BYPASS",
            "SENTINEL-ROLE-BYPASS",
            no_embedding_cache_shortcut=True,
        )
        call_kwargs = mock_classifier.call_args.kwargs
        assert call_kwargs.get("bypass") is True

    def test_serve_with_cache_on_mode_hit_uses_classifier(self):
        import struct

        provider = _make_provider("voyage-ai")
        live_vec = [1.0, 2.0, 3.0]
        blob = struct.pack(f"<{len(live_vec)}f", *live_vec)

        mock_cache = MagicMock()
        mock_cache.enabled_for.return_value = True
        mock_cache.mode_for.return_value = "on"
        mock_cache.build_key_for_provider.return_value = "s:d:hello"
        mock_qualifier = MagicMock()
        mock_qualifier.dimension = len(live_vec)
        mock_cache.qualifier.return_value = mock_qualifier
        mock_cache.lookup.return_value = blob

        _meta, mock_classifier = self._run_with_patches(
            {
                "code_indexer.server.services.governed_call.get_coalescer_registry": None,
                "code_indexer.server.services.governed_call.get_query_embedding_cache": mock_cache,
                "code_indexer.server.services.governed_call._digest_for_provider": "digest-hit",
            },
            provider,
            "SENTINEL-OUTCOME-HIT",
            "SENTINEL-ROLE-HIT",
        )
        call_kwargs = mock_classifier.call_args.kwargs
        assert call_kwargs.get("cache_hit") is True
        assert call_kwargs.get("cache_mode") == "on"

    def test_serve_with_cache_on_mode_miss_uses_classifier(self):
        provider = _make_provider("voyage-ai")
        mock_cache = MagicMock()
        mock_cache.enabled_for.return_value = True
        mock_cache.mode_for.return_value = "on"
        mock_cache.build_key_for_provider.return_value = "s:d:miss-key"
        mock_qualifier = MagicMock()
        mock_qualifier.dimension = 3
        mock_cache.qualifier.return_value = mock_qualifier
        mock_cache.lookup.return_value = None  # MISS

        _meta, mock_classifier = self._run_with_patches(
            {
                "code_indexer.server.services.governed_call.get_coalescer_registry": None,
                "code_indexer.server.services.governed_call.get_query_embedding_cache": mock_cache,
                "code_indexer.server.services.governed_call._digest_for_provider": "digest-miss",
                "code_indexer.server.services.governed_call.governed_query_embedding": [
                    9.0,
                    9.0,
                    9.0,
                ],
            },
            provider,
            "SENTINEL-OUTCOME-MISS",
            "SENTINEL-ROLE-MISS",
        )
        call_kwargs = mock_classifier.call_args.kwargs
        assert call_kwargs.get("cache_hit") is False
        assert call_kwargs.get("cache_mode") == "on"

    def test_decide_role_and_outcome_is_imported_symbol_in_governed_call(self):
        """Regression guard: governed_call.py must import the SAME function
        object as embed_event_decision_table.decide_role_and_outcome (single
        source of truth), not a re-implementation."""
        import code_indexer.server.services.governed_call as gc_mod
        from code_indexer.server.services.embed_event_decision_table import (
            decide_role_and_outcome,
        )

        assert gc_mod.decide_role_and_outcome is decide_role_and_outcome
