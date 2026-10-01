"""Bug #1876 item 4: xray_search's front door must turn invalid
include_patterns/exclude_patterns into a structured error, never silently
skip or pass a non-string/malformed pattern through to the background job.

`handle_xray_search` previously extracted include_patterns/exclude_patterns
with a bare `params.get(...) or []` and never validated them at all -- a
non-string item (`[None]`) or a malformed pattern (unbalanced brace,
gitignore negation/comment syntax) passed straight into the background job,
where an exclude failing open would silently widen the search deep inside a
job most callers never inspect for this class of failure.
"""

from __future__ import annotations

import pytest

from code_indexer.server.mcp.handlers.xray import _validate_xray_search_patterns


@pytest.mark.parametrize(
    "include_patterns,exclude_patterns,expected_error",
    [
        ([None], [], "include_patterns_invalid"),
        ([], [123], "exclude_patterns_invalid"),
        (["*.{ts,md"], [], "include_patterns_invalid"),
        ([], ["!*.md"], "exclude_patterns_invalid"),
        (["#x"], [], "include_patterns_invalid"),
    ],
)
def test_invalid_pattern_is_a_structured_error(
    include_patterns, exclude_patterns, expected_error
):
    err = _validate_xray_search_patterns(include_patterns, exclude_patterns)
    assert err is not None
    assert err["error"] == expected_error


@pytest.mark.parametrize(
    "include_patterns,exclude_patterns",
    [
        (["*.{ts,md}"], ["docs"]),
        ([], []),
        (None, None),
    ],
)
def test_valid_patterns_pass_validation(include_patterns, exclude_patterns):
    err = _validate_xray_search_patterns(include_patterns, exclude_patterns)
    assert err is None


@pytest.mark.parametrize(
    "include_patterns,exclude_patterns,expected_error",
    [
        ("", [], "include_patterns_invalid"),
        (0, [], "include_patterns_invalid"),
        (False, [], "include_patterns_invalid"),
        ([], "", "exclude_patterns_invalid"),
        ([], 0, "exclude_patterns_invalid"),
        ([], False, "exclude_patterns_invalid"),
    ],
)
def test_falsy_non_list_pattern_is_a_structured_error_not_silently_dropped(
    include_patterns, exclude_patterns, expected_error
):
    """A falsy-but-not-a-list value (e.g. "" or 0) must be rejected, not
    silently normalized to 'no filter' by a caller-side `or []`."""
    err = _validate_xray_search_patterns(include_patterns, exclude_patterns)
    assert err is not None
    assert err["error"] == expected_error
