"""Story #1418 LOW-2 (code review finding): tag the cache-shadow-audit
re-embed call with purpose="cache_shadow_audit" for vendor-cost
reconciliation, instead of the ordinary purpose="query" it inherits by
reusing the standard query-embedding code path.

Investigation (see docstring in embedding_cache_audit.py) established that
``_get_second_search_vector``'s mode=="on" branch deliberately makes a REAL
live vendor call via ``governed_query_embedding()`` -- the primary search
used the CACHED vector, and this audit re-embed verifies it against a fresh
live embedding. Because that call flows through the exact same code path as
an ordinary user query (voyage_ai.py / cohere_embedding.py derive
``purpose`` from an internal retry flag, not from any caller-supplied
"why" signal), it was being recorded as purpose="query" -- indistinguishable
from a real user query in the embedding_call_stats table. mode=="shadow"
never calls a live provider (it decodes the cached blob), so it has no
stats-purpose implication at all.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestOnModeReembedTaggedCacheShadowAudit:
    def test_on_mode_reembed_call_is_wrapped_in_stats_purpose_override(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            _stats_purpose_override,
        )
        from code_indexer.server.services.embedding_cache_audit import (
            _get_second_search_vector,
        )

        captured: dict = {}

        def fake_governed_query_embedding(provider, text, *, embedding_purpose="query"):
            captured["override_during_call"] = _stats_purpose_override.get()
            return [1.0, 0.0, 0.0]

        with patch(
            "code_indexer.server.services.embedding_cache_audit.governed_query_embedding",
            side_effect=fake_governed_query_embedding,
        ):
            result = _get_second_search_vector("on", {}, MagicMock(), "query text")

        assert result == [1.0, 0.0, 0.0]
        assert captured["override_during_call"] == "cache_shadow_audit"

    def test_override_is_not_active_outside_the_on_mode_reembed_call(self):
        from code_indexer.server.services.embedding_call_instrumentation import (
            _stats_purpose_override,
        )
        from code_indexer.server.services.embedding_cache_audit import (
            _get_second_search_vector,
        )

        with patch(
            "code_indexer.server.services.embedding_cache_audit.governed_query_embedding",
            return_value=[1.0, 0.0, 0.0],
        ):
            _get_second_search_vector("on", {}, MagicMock(), "query text")

        # Override must be reset once the wrapped call returns -- it must
        # never leak into unrelated code running after this helper.
        assert _stats_purpose_override.get() is None

    def test_shadow_mode_never_triggers_the_override(self):
        """shadow mode decodes the cached blob -- no live provider call, so
        no stats-purpose override is ever activated."""
        import struct

        from code_indexer.server.services.embedding_call_instrumentation import (
            _stats_purpose_override,
        )
        from code_indexer.server.services.embedding_cache_audit import (
            _get_second_search_vector,
        )

        cached_blob = struct.pack("<3f", 1.0, 0.0, 0.0)
        audit_ctx = {"cached_blob": cached_blob}

        result = _get_second_search_vector(
            "shadow", audit_ctx, MagicMock(), "query text"
        )

        assert result == [1.0, 0.0, 0.0]
        assert _stats_purpose_override.get() is None
