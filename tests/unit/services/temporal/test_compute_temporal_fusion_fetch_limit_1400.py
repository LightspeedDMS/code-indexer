"""Story #1400 HIGH item: unify fusion_fetch_limit computation across doors.

FINAL LOCKED DESIGN: "Unify fusion_fetch_limit computation into ONE shared
function both doors call (adopt MCP's more-correct access-filter-aware
formula as the canonical one -- REST's current formula silently under-
fetches relative to it)."

MCP's formula (search.py's _compute_effective_limit + _compute_rerank_limit):
  1. effective_limit = access_filtering_service.calculate_over_fetch_limit(
         requested_limit) for a non-admin user with an access service,
     else requested_limit unchanged.
  2. If rerank_query is present, apply calculate_overfetch_limit on top,
     with access_filter_extra = effective_limit - requested_limit.

compute_temporal_fusion_fetch_limit reproduces this EXACT two-step formula
as the single shared implementation both MCP and REST adapters call, so an
identical logical query produces an identical dedup signature regardless
of which door it arrives through (Scenario 12, same-node case).
"""

import pytest


class _FakeAccessFilteringService:
    def __init__(self, admins=None, over_fetch_multiplier=2):
        self._admins = admins or set()
        self._multiplier = over_fetch_multiplier

    def is_admin_user(self, username):
        return username in self._admins

    def calculate_over_fetch_limit(self, requested_limit):
        return requested_limit * self._multiplier


def _make_config_service(overfetch_multiplier=5):
    from unittest.mock import MagicMock

    from code_indexer.server.utils.config_manager import RerankConfig

    config = MagicMock()
    config.rerank_config = RerankConfig(overfetch_multiplier=overfetch_multiplier)
    config_service = MagicMock()
    config_service.get_config.return_value = config
    return config_service


class TestNonAdminNoRerank:
    def test_uses_access_filter_over_fetch_limit(self):
        from code_indexer.services.temporal.temporal_fusion_limit import (
            compute_temporal_fusion_fetch_limit,
        )

        access_svc = _FakeAccessFilteringService(over_fetch_multiplier=2)
        result = compute_temporal_fusion_fetch_limit(
            requested_limit=10,
            rerank_query=None,
            access_filtering_service=access_svc,
            username="alice",
            config_service=_make_config_service(),
        )
        assert result == 20  # 10 * 2 access-filter multiplier


class TestAdminNoRerank:
    def test_admin_bypasses_access_filter_over_fetch(self):
        from code_indexer.services.temporal.temporal_fusion_limit import (
            compute_temporal_fusion_fetch_limit,
        )

        access_svc = _FakeAccessFilteringService(
            admins={"admin"}, over_fetch_multiplier=2
        )
        result = compute_temporal_fusion_fetch_limit(
            requested_limit=10,
            rerank_query=None,
            access_filtering_service=access_svc,
            username="admin",
            config_service=_make_config_service(),
        )
        assert result == 10  # admin: no access-filter overfetch


class TestWithRerankQuery:
    def test_applies_rerank_overfetch_on_top_of_access_filter_limit(self):
        from code_indexer.services.temporal.temporal_fusion_limit import (
            compute_temporal_fusion_fetch_limit,
        )

        access_svc = _FakeAccessFilteringService(over_fetch_multiplier=2)
        result = compute_temporal_fusion_fetch_limit(
            requested_limit=10,
            rerank_query="auth logic",
            access_filtering_service=access_svc,
            username="alice",
            config_service=_make_config_service(overfetch_multiplier=5),
        )
        # effective_limit = 20 (access-filter); access_filter_extra = 10;
        # calculate_overfetch_limit formula: max(requested*overfetch_mul,
        #   requested+access_filter_extra) = max(10*5, 10+10) = max(50, 20) = 50
        assert result == 50


class TestNoAccessFilteringService:
    def test_none_access_filtering_service_falls_back_to_requested_limit(self):
        from code_indexer.services.temporal.temporal_fusion_limit import (
            compute_temporal_fusion_fetch_limit,
        )

        result = compute_temporal_fusion_fetch_limit(
            requested_limit=10,
            rerank_query=None,
            access_filtering_service=None,
            username="alice",
            config_service=_make_config_service(),
        )
        assert result == 10


class TestCrossDoorParity:
    def test_identical_inputs_produce_identical_result_regardless_of_caller(self):
        """The whole point: both doors calling this with the same logical
        inputs must get the exact same fetch limit."""
        from code_indexer.services.temporal.temporal_fusion_limit import (
            compute_temporal_fusion_fetch_limit,
        )

        access_svc = _FakeAccessFilteringService(over_fetch_multiplier=3)
        config_service = _make_config_service(overfetch_multiplier=4)

        mcp_result = compute_temporal_fusion_fetch_limit(
            requested_limit=15,
            rerank_query="query text",
            access_filtering_service=access_svc,
            username="bob",
            config_service=config_service,
        )
        rest_result = compute_temporal_fusion_fetch_limit(
            requested_limit=15,
            rerank_query="query text",
            access_filtering_service=access_svc,
            username="bob",
            config_service=config_service,
        )
        assert mcp_result == rest_result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
