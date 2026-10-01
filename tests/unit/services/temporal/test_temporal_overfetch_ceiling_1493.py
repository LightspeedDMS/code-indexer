"""Tests for the combined temporal overfetch ceiling (Story #1493 AC1).

Report Finding C2: TEMPORAL_OVERFETCH_MULTIPLIER (shard-level) and the
per-chunk-type multiplier in temporal_search_service.py stack multiplicatively
to as much as 120x (3 shard x 40 chunk-type, the commit_message worst case).
cap_combined_overfetch_search_limit() is the ONE authoritative place that
bounds the COMBINED multiplier at TEMPORAL_COMBINED_OVERFETCH_CEILING.
"""

from code_indexer.services.temporal.temporal_fusion import (
    TEMPORAL_COMBINED_OVERFETCH_CEILING,
    cap_combined_overfetch_search_limit,
)

# Report Finding C2's documented worst-case factors: shard-level overfetch
# multiplier (TEMPORAL_OVERFETCH_MULTIPLIER) x chunk_type=commit_message's
# per-chunk-type multiplier.
_WORST_CASE_SHARD_MULTIPLIER = 3
_WORST_CASE_CHUNK_TYPE_MULTIPLIER = 40
_WORST_CASE_COMBINED_MULTIPLIER = (
    _WORST_CASE_SHARD_MULTIPLIER * _WORST_CASE_CHUNK_TYPE_MULTIPLIER
)  # 120x

# A combined multiplier well under any sane ceiling -- used to prove
# below-ceiling queries are byte-identical to their pre-change behavior.
_BELOW_CEILING_MULTIPLIER = 15

_SAMPLE_USER_LIMIT = 10


def test_ceiling_constant_is_below_worst_case_120x() -> None:
    """The ceiling must be a real cap: strictly below the 120x worst case
    the report identifies, and strictly positive."""
    assert TEMPORAL_COMBINED_OVERFETCH_CEILING < _WORST_CASE_COMBINED_MULTIPLIER
    assert TEMPORAL_COMBINED_OVERFETCH_CEILING > 0


def test_worst_case_commit_message_is_capped() -> None:
    """limit=10, shard_multiplier=3, chunk_type_multiplier=40 -> natural
    combined = 120x -> natural search_limit = 1200. Must be capped down to
    true_user_limit * CEILING."""
    natural_search_limit = _SAMPLE_USER_LIMIT * _WORST_CASE_COMBINED_MULTIPLIER

    capped = cap_combined_overfetch_search_limit(
        _SAMPLE_USER_LIMIT, natural_search_limit
    )

    assert capped == _SAMPLE_USER_LIMIT * TEMPORAL_COMBINED_OVERFETCH_CEILING
    assert capped < natural_search_limit


def test_below_ceiling_queries_are_unaffected() -> None:
    """A combined multiplier already at/below the ceiling must be returned
    byte-identical (no change to existing behavior)."""
    natural_search_limit = _SAMPLE_USER_LIMIT * _BELOW_CEILING_MULTIPLIER

    capped = cap_combined_overfetch_search_limit(
        _SAMPLE_USER_LIMIT, natural_search_limit
    )

    assert capped == natural_search_limit


def test_never_truncates_below_user_requested_limit() -> None:
    """Capping must never return less than the true user-requested limit,
    even in a pathological/misconfigured-ceiling scenario (combined
    multiplier of exactly 1x -- no overfetch at all)."""
    true_user_limit = _SAMPLE_USER_LIMIT
    natural_search_limit = true_user_limit

    capped = cap_combined_overfetch_search_limit(true_user_limit, natural_search_limit)

    assert capped >= true_user_limit


def test_zero_true_user_limit_is_defensive_noop() -> None:
    """A true_user_limit of 0 is not a real production input (dispatch/
    service always pass a positive limit) -- must not raise or divide-by-
    zero, and defensively returns the natural search_limit unchanged."""
    natural_search_limit = _SAMPLE_USER_LIMIT * _BELOW_CEILING_MULTIPLIER

    capped = cap_combined_overfetch_search_limit(0, natural_search_limit)

    assert capped == natural_search_limit


def test_negative_true_user_limit_is_defensive_noop() -> None:
    """A negative true_user_limit is not a real production input -- must not
    raise or divide-by-zero, and defensively returns the natural
    search_limit unchanged."""
    natural_search_limit = _SAMPLE_USER_LIMIT * _BELOW_CEILING_MULTIPLIER

    capped = cap_combined_overfetch_search_limit(-1, natural_search_limit)

    assert capped == natural_search_limit
