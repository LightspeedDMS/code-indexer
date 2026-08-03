"""Tests for Story #1493 AC2's zero-I/O point_id projection primitives.

build_point_id() (Story #1290) encodes "{project_id}:commit:{hash}:{j}",
where chunk_index j==0 is DEFINED to be the head chunk (contextual_chunker.py:
"is_head = (idx == 0) -- the message always leads the aggregated document").
parse_temporal_point_id()/is_head_chunk_id() recover this SAME fact purely
from the point_id string -- zero database access, zero decode -- so
FilesystemVectorStore's hydration loop can skip a full zstd+json decode for
any candidate the chunk_type post-filter would discard anyway (report
Finding C2).
"""

from code_indexer.services.temporal.temporal_point_builder import (
    build_point_id,
    is_head_chunk_id,
    parse_temporal_point_id,
)


def test_parse_recovers_project_hash_and_chunk_index():
    point_id = build_point_id("myproject", "abc123def", 0)

    parsed = parse_temporal_point_id(point_id)

    assert parsed == ("myproject", "abc123def", 0)


def test_parse_returns_none_for_non_temporal_point_id():
    """A point_id that does not match the "...:commit:...:<int>" shape must
    return None -- never a guessed/wrong tuple."""
    assert parse_temporal_point_id("some/other/path.py:0:100") is None
    assert parse_temporal_point_id("not_temporal_at_all") is None


def test_parse_returns_none_for_non_integer_chunk_index():
    """A malformed trailing segment (not parseable as int) must return None,
    never raise -- callers fall back to a full decode rather than crash."""
    assert parse_temporal_point_id("myproject:commit:abc123:not_an_int") is None


def test_is_head_true_for_chunk_index_zero():
    point_id = build_point_id("myproject", "abc123def", 0)
    assert is_head_chunk_id(point_id) is True


def test_is_head_false_for_nonzero_chunk_index():
    point_id = build_point_id("myproject", "abc123def", 3)
    assert is_head_chunk_id(point_id) is False


def test_is_head_none_for_unparseable_point_id():
    """None (not False) signals 'could not determine' -- callers MUST treat
    this as 'fall back to full decode', never as 'non-match'."""
    assert is_head_chunk_id("not_temporal_at_all") is None
