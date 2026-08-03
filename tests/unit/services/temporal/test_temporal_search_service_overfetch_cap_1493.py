"""Tests for Story #1493 AC1 wiring: query_temporal() applies the combined
overfetch ceiling (cap_combined_overfetch_search_limit) to the search_limit
it sends to the vector store, using the true (pre-shard-multiplication)
user-requested limit when the caller (temporal_fusion_dispatch.py) supplies
it via the new `true_user_limit` parameter.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.services.temporal.temporal_fusion import (
    TEMPORAL_COMBINED_OVERFETCH_CEILING,
)
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchService,
    ALL_TIME_RANGE,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

# Mirrors the real stacking: temporal_fusion_dispatch.py multiplies the true
# user limit by the shard-level TEMPORAL_OVERFETCH_MULTIPLIER (3) BEFORE
# calling query_temporal, so query_temporal's own `limit` param already
# carries that factor.
_SHARD_MULTIPLIER = 3
_TRUE_USER_LIMIT = 10
_SHARD_MULTIPLIED_LIMIT = _TRUE_USER_LIMIT * _SHARD_MULTIPLIER
_COMMIT_MESSAGE_CHUNK_TYPE_MULTIPLIER = 40


def _make_service() -> TemporalSearchService:
    mock_vector_store = MagicMock()
    mock_vector_store.search.return_value = ([], None)
    mock_embedding_provider = MagicMock()
    mock_embedding_provider.embed_query.return_value = [0.1] * 1024
    return TemporalSearchService(
        config_manager=MagicMock(),
        project_root=Path("/fake/repo"),
        vector_store_client=mock_vector_store,
        embedding_provider=mock_embedding_provider,
    )


def test_worst_case_commit_message_search_limit_is_capped():
    """limit already shard-multiplied to 30 (true_user_limit=10 x shard 3),
    chunk_type=commit_message multiplies by 40 -> natural 1200, combined vs
    true_user_limit = 120x -- must be capped to true_user_limit * CEILING."""
    service = _make_service()

    with patch(
        "code_indexer.services.temporal.temporal_search_service.isinstance",
        return_value=True,
    ):
        try:
            service.query_temporal(
                query="test query",
                time_range=ALL_TIME_RANGE,
                chunk_type="commit_message",
                limit=_SHARD_MULTIPLIED_LIMIT,
                true_user_limit=_TRUE_USER_LIMIT,
            )
        except Exception:
            pass  # only the vector_store.search call args matter here

    call_args = service.vector_store_client.search.call_args
    actual_search_limit = call_args.kwargs["limit"]

    natural_search_limit = (
        _SHARD_MULTIPLIED_LIMIT * _COMMIT_MESSAGE_CHUNK_TYPE_MULTIPLIER
    )
    assert natural_search_limit == 1200  # sanity: matches the report's own number
    assert actual_search_limit == _TRUE_USER_LIMIT * TEMPORAL_COMBINED_OVERFETCH_CEILING
    assert actual_search_limit < natural_search_limit
    # prefetch_limit must track the same capped value (both sites, per the
    # story's technical requirements referencing lines 573/577 today)
    assert call_args.kwargs["prefetch_limit"] == actual_search_limit


def test_chunk_type_is_forwarded_to_fsv_as_temporal_chunk_type():
    """Story #1493 AC2: query_temporal must forward `chunk_type` to the FSV
    search() call as `temporal_chunk_type`, so the storage layer's
    decode-avoidance (skip full decode for the opposite chunk type) can
    activate. Without this wiring, AC2's FSV-level mechanism is inert for
    every real production temporal query.

    Uses a MagicMock(spec=FilesystemVectorStore) so the real isinstance()
    check in query_temporal resolves True naturally -- no patching of
    Python's builtin isinstance."""
    mock_vector_store = MagicMock(spec=FilesystemVectorStore)
    mock_vector_store.search.return_value = ([], {})
    mock_embedding_provider = MagicMock()
    mock_embedding_provider.embed_query.return_value = [0.1] * 1024
    service = TemporalSearchService(
        config_manager=MagicMock(),
        project_root=Path("/fake/repo"),
        vector_store_client=mock_vector_store,
        embedding_provider=mock_embedding_provider,
    )

    service.query_temporal(
        query="test query",
        time_range=ALL_TIME_RANGE,
        chunk_type="commit_message",
        limit=_TRUE_USER_LIMIT,
    )

    call_args = mock_vector_store.search.call_args
    assert call_args.kwargs["temporal_chunk_type"] == "commit_message"


def test_below_ceiling_query_is_unaffected():
    """No chunk_type, no true_user_limit supplied (defaults to limit itself)
    -- search_limit must be byte-identical to pre-#1493 behavior."""
    service = _make_service()

    with patch(
        "code_indexer.services.temporal.temporal_search_service.isinstance",
        return_value=True,
    ):
        try:
            service.query_temporal(
                query="test query",
                time_range=ALL_TIME_RANGE,
                limit=_TRUE_USER_LIMIT,
            )
        except Exception:
            pass

    call_args = service.vector_store_client.search.call_args
    actual_search_limit = call_args.kwargs["limit"]
    # No post-filters, is_all_time -> exact limit (pre-existing behavior)
    assert actual_search_limit == _TRUE_USER_LIMIT
