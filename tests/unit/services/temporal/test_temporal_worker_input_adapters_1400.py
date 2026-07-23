"""
Tests for Story #1400 Phase 3: MCP Dict / REST SemanticQueryRequest ->
TemporalWorkerInput adapters.

FINAL LOCKED DESIGN alias/rejection rules:
- Missing alias on either door -> reject (temporal requires an explicit
  single repo in v1). error_code TEMPORAL_ALIAS_REQUIRED.
- Any MCP list-typed alias, including a single-element list, is rejected
  with a DISTINCT error_code (TEMPORAL_SINGLE_REPO_REQUIRED) -- this
  removes list-vs-string ambiguity from the dedup signature entirely.

Canonical diff_type handling (used identically here and in the dedup
signature): None/empty-string/whitespace-only/[] -> None; a comma-
containing string is split on commas; a plain string becomes a
one-element tuple; a list is stripped element-by-element, empty elements
removed, duplicates removed (first occurrence preserved for display,
additionally sorted for the dedup hash specifically -- this module
produces the sorted/deduped tuple used by both).

NOTE (named, honest gap): fusion_fetch_limit unification between MCP's
access-filter-aware _compute_rerank_limit/_compute_effective_limit and
REST's _rest_calculate_overfetch_limit is NOT implemented in this story
session -- both adapters accept fusion_fetch_limit as an EXPLICIT
precomputed parameter from the caller's own door-specific logic, so
Scenario 12's same-node cross-door join is not yet guaranteed for calls
using different overfetch formulas. This is a deliberate, named scope
boundary, not an oversight -- see the final report.

TDD: written BEFORE implementation.
"""

import pytest

from code_indexer.services.temporal.temporal_worker_input_adapters import (
    TemporalAliasRejectedError,
    build_temporal_worker_input_from_mcp_dict,
    build_temporal_worker_input_from_rest_request,
)


class _FakeRestRequest:
    """Minimal stand-in for SemanticQueryRequest (duck-typed attribute
    access -- the adapter only reads attributes, matching Pydantic model
    field access)."""

    def __init__(self, **kwargs):
        defaults = dict(
            query_text="auth logic",
            repository_alias="my-repo",
            limit=10,
            min_score=None,
            file_extensions=None,
            language=None,
            path_filter=None,
            exclude_language=None,
            exclude_path=None,
            time_range=None,
            time_range_all=False,
            at_commit=None,
            diff_type=None,
            author=None,
            chunk_type=None,
            temporal_embedder=None,
            no_embedding_cache_shortcut=False,
            rerank_query=None,
            rerank_instruction=None,
        )
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)


class TestMcpAdapterBasicMapping:
    def test_maps_core_fields(self):
        params = {
            "query_text": "auth logic",
            "repository_alias": "my-repo",
            "limit": 10,
            "time_range": "2024-01-01..2024-12-31",
        }
        wi = build_temporal_worker_input_from_mcp_dict(
            params, username="alice", fusion_fetch_limit=30
        )
        assert wi.query_text == "auth logic"
        assert wi.repository_alias == "my-repo"
        assert wi.username == "alice"
        assert wi.requested_limit == 10
        assert wi.fusion_fetch_limit == 30
        assert wi.time_range_raw == "2024-01-01..2024-12-31"

    def test_missing_alias_rejected(self):
        params = {"query_text": "q"}
        with pytest.raises(TemporalAliasRejectedError) as exc_info:
            build_temporal_worker_input_from_mcp_dict(
                params, username="alice", fusion_fetch_limit=30
            )
        assert exc_info.value.error_code == "TEMPORAL_ALIAS_REQUIRED"

    def test_list_alias_rejected_even_single_element(self):
        params = {"query_text": "q", "repository_alias": ["my-repo"]}
        with pytest.raises(TemporalAliasRejectedError) as exc_info:
            build_temporal_worker_input_from_mcp_dict(
                params, username="alice", fusion_fetch_limit=30
            )
        assert exc_info.value.error_code == "TEMPORAL_SINGLE_REPO_REQUIRED"


class TestRestAdapterBasicMapping:
    def test_maps_core_fields(self):
        request = _FakeRestRequest()
        wi = build_temporal_worker_input_from_rest_request(
            request, username="bob", fusion_fetch_limit=25
        )
        assert wi.query_text == "auth logic"
        assert wi.repository_alias == "my-repo"
        assert wi.username == "bob"
        assert wi.requested_limit == 10
        assert wi.fusion_fetch_limit == 25

    def test_missing_alias_rejected(self):
        request = _FakeRestRequest(repository_alias=None)
        with pytest.raises(TemporalAliasRejectedError) as exc_info:
            build_temporal_worker_input_from_rest_request(
                request, username="bob", fusion_fetch_limit=25
            )
        assert exc_info.value.error_code == "TEMPORAL_ALIAS_REQUIRED"


class TestDiffTypeCanonicalization:
    def test_comma_string_split_sorted_deduped(self):
        params = {
            "query_text": "q",
            "repository_alias": "my-repo",
            "diff_type": "modified,added,added",
        }
        wi = build_temporal_worker_input_from_mcp_dict(
            params, username="alice", fusion_fetch_limit=30
        )
        assert wi.diff_types == ("added", "modified")

    def test_empty_string_becomes_none(self):
        params = {
            "query_text": "q",
            "repository_alias": "my-repo",
            "diff_type": "   ",
        }
        wi = build_temporal_worker_input_from_mcp_dict(
            params, username="alice", fusion_fetch_limit=30
        )
        assert wi.diff_types is None

    def test_rest_list_diff_type_normalized(self):
        request = _FakeRestRequest(diff_type=["modified", "added", "added"])
        wi = build_temporal_worker_input_from_rest_request(
            request, username="bob", fusion_fetch_limit=25
        )
        assert wi.diff_types == ("added", "modified")


class TestCrossDoorParity:
    def test_equivalent_inputs_produce_identical_worker_input(self):
        """Same logical query via each door (same explicit
        fusion_fetch_limit, the honest named gap noted above) must produce
        a field-identical TemporalWorkerInput."""
        params = {
            "query_text": "auth logic",
            "repository_alias": "my-repo",
            "limit": 10,
            "time_range": "2024-01-01..2024-12-31",
            "language": "python",
            "diff_type": "added,modified",
            "author": "alice@example.com",
        }
        request = _FakeRestRequest(
            time_range="2024-01-01..2024-12-31",
            language="python",
            diff_type=["added", "modified"],
            author="alice@example.com",
        )

        wi_mcp = build_temporal_worker_input_from_mcp_dict(
            params, username="alice", fusion_fetch_limit=30
        )
        wi_rest = build_temporal_worker_input_from_rest_request(
            request, username="alice", fusion_fetch_limit=30
        )

        assert wi_mcp == wi_rest


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
