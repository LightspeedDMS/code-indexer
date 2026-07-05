"""Story #1295 (Epic #1288 final) Step F: guard tests proving the legacy
in-memory query-embedding cache tracker and its consumers are fully deleted.

These are the executable "grep proof" the story's DoD requires: after Step E
(OTEL re-source, embedding_cache_otel_metrics.py) and the audit re-source
(embedding_cache_audit.py -> update_audit_by_key) are both green, every
remaining reader of the retiring QueryEmbeddingCacheMetrics tallies must be
gone -- module, accessors, coalescer counter fields, CoalescerRegistry.metrics(),
and the admin_coalescer_metrics REST route.
"""

from __future__ import annotations

import importlib

import pytest


class TestQueryEmbeddingCacheMetricsModuleDeleted:
    def test_module_import_raises_module_not_found(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(
                "code_indexer.server.services.query_embedding_cache_metrics"
            )


class TestGovernedCallAccessorsDeleted:
    def test_get_query_embedding_cache_metrics_symbol_absent(self):
        from code_indexer.server.services import governed_call

        assert not hasattr(governed_call, "get_query_embedding_cache_metrics")

    def test_set_query_embedding_cache_metrics_symbol_absent(self):
        from code_indexer.server.services import governed_call

        assert not hasattr(governed_call, "set_query_embedding_cache_metrics")

    def test_clear_query_embedding_cache_metrics_symbol_absent(self):
        from code_indexer.server.services import governed_call

        assert not hasattr(governed_call, "clear_query_embedding_cache_metrics")

    def test_serve_with_cache_has_no_metrics_parameter(self):
        import inspect

        from code_indexer.server.services.governed_call import _serve_with_cache

        sig = inspect.signature(_serve_with_cache)
        assert "metrics" not in sig.parameters


class TestCoalescerCounterFieldsDeleted:
    def test_coalescer_instance_has_no_dispatch_counters(self):
        from code_indexer.server.services.embedding_coalescer import (
            EmbeddingCoalescer,
        )
        from code_indexer.server.services.provider_concurrency_governor import (
            ProviderConcurrencyGovernor,
        )
        from unittest.mock import MagicMock

        governor = ProviderConcurrencyGovernor(4)
        fake_provider = MagicMock()
        fake_provider.get_provider_name.return_value = "voyage-ai"
        c = EmbeddingCoalescer("voyage:embed", fake_provider, governor=governor)

        assert not hasattr(c, "texts_coalesced")
        assert not hasattr(c, "batches_dispatched")
        assert not hasattr(c, "dedup_savings")
        assert not hasattr(c, "provider_embed_calls")


class TestCoalescerRegistryMetricsDeleted:
    def test_registry_has_no_metrics_method(self):
        from code_indexer.server.services.coalescer_registry import CoalescerRegistry

        registry = CoalescerRegistry()
        assert not hasattr(registry, "metrics")


class TestAdminCoalescerMetricsRouteRemoved:
    def test_router_module_deleted(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(
                "code_indexer.server.routers.admin_coalescer_metrics"
            )

    def test_route_not_registered_in_inline_routes(self):
        import inspect

        from code_indexer.server.routers import inline_routes

        source = inspect.getsource(inline_routes)
        assert "admin_coalescer_metrics" not in source


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
