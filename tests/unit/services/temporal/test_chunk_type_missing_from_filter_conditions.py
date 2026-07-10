"""Test for Story #1290 AC12: chunk_type is a POST-filter, not a vector-store filter.

Story #476 (pre-#1290) attempted to fix a chunk_type bug by pushing a
`{"key": "type", ...}` condition into vector-store filter_conditions. That
approach is now obsolete: Story #1290's per-commit payloads ALWAYS carry
`type == "commit_chunk"` (a single constant, no longer "commit_message" vs
"commit_diff"), so a `type`-keyed vector-store filter would match nothing.

chunk_type is instead validated up front (AC12: only "commit_message" or
"commit_diff" accepted) and applied as an is_head-based POST-filter in
_filter_by_time_range: "commit_message" keeps only head chunks; "commit_diff"
is a no-op (matches all chunks). This test verifies that contract end-to-end.
"""

from pathlib import Path
from unittest.mock import Mock
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchService,
    ALL_TIME_RANGE,
)


def test_chunk_type_is_not_pushed_as_a_type_match_filter_condition():
    """chunk_type must NOT be converted into a `{"key": "type", ...}` vector-store
    filter -- every payload's `type` is the constant "commit_chunk" post-#1290,
    so such a filter would always match zero rows."""
    # Setup
    config_manager = Mock()
    project_root = Path("/fake/project")

    mock_result = {
        "id": "test:commit:abc:0",
        "score": 0.85,
        "payload": {
            "type": "commit_chunk",
            "is_head": True,
            "commit_hash": "abc",
            "primary_path": "dummy",
            "commit_timestamp": 1704088800,
        },
        "chunk_text": "test content",
    }
    vector_store_client = Mock()
    vector_store_client.collection_exists.return_value = True
    vector_store_client.search.return_value = [mock_result]  # List, not tuple

    embedding_provider = Mock()
    embedding_provider.get_embedding.return_value = [0.1] * 1024

    service = TemporalSearchService(
        config_manager=config_manager,
        project_root=project_root,
        vector_store_client=vector_store_client,
        embedding_provider=embedding_provider,
        collection_name="code-indexer-temporal",
    )

    # Execute query with chunk_type filter
    results = service.query_temporal(
        query="temporal",
        time_range=ALL_TIME_RANGE,
        chunk_type="commit_message",  # AC12: is_head-based POST-filter
        limit=10,
    )

    assert vector_store_client.search.called, (
        "Vector store search should have been called"
    )

    call_args = vector_store_client.search.call_args
    filter_conditions = (
        call_args[1].get("filter_conditions", {}) if call_args[1] else {}
    )
    must_conditions = filter_conditions.get("must", [])

    type_filter_found = any(
        condition.get("key") == "type" for condition in must_conditions
    )
    assert not type_filter_found, (
        f"chunk_type must NOT be pushed as a `type`-keyed vector-store filter "
        f"(every payload's type is the constant 'commit_chunk' post-#1290); "
        f"got filter_conditions: {filter_conditions}"
    )

    # And the post-filter still correctly returns the head-chunk result.
    assert len(results.results) == 1
    assert results.results[0].metadata["is_head"] is True
