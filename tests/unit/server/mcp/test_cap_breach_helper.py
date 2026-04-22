"""
Tests for the CapBreach dataclass and cap_breach helpers.

Bug #881 Phase 3: The cap-breach path has two callers with different error contracts:
- MCP path: must return an MCP envelope dict (success=False, error field, cap/observed, pattern)
- REST path: must raise HTTPException with status 400 and actionable detail message

Tests verify that:
- CapBreach carries pattern, observed_count, configured_cap
- cap_breach_response() returns MCP envelope with success=False, error code, cap, observed, and
  the pattern string embedded in the response text for actionable client messaging
- cap_breach_http_exception() raises HTTPException(status_code=400) with pattern+count+cap in detail
- _check_wildcard_cap() returns None when at cap (inclusive boundary) or well under cap
- _check_wildcard_cap() returns CapBreach when observed_count exceeds configured_cap

Named constants throughout — no magic numbers.
"""

import pytest


# Named constants — no magic numbers
CAP_VALUE = 50
OBSERVED_AT_CAP = 50  # boundary: exactly at cap — None (not a breach)
OBSERVED_OVER_CAP = 51  # one over cap — CapBreach
OBSERVED_WELL_UNDER_CAP = 10  # well under cap — None
HTTP_400 = 400
PATTERN = "*-global"
MCP_SUCCESS_FIELD = "success"
MCP_ERROR_FIELD = "error"
MCP_CAP_FIELD = "cap"
MCP_OBSERVED_FIELD = "observed"
MCP_ERROR_CODE = "wildcard_cap_exceeded"


def _make_breach(observed: int = OBSERVED_OVER_CAP, cap: int = CAP_VALUE):
    """Construct a CapBreach for the standard test pattern."""
    from code_indexer.server.mcp.handlers._utils import CapBreach

    return CapBreach(pattern=PATTERN, observed_count=observed, configured_cap=cap)


# ---------------------------------------------------------------------------
# Test 1 — CapBreach is a dataclass with required fields
# ---------------------------------------------------------------------------


def test_cap_breach_dataclass_carries_pattern_observed_and_cap():
    """CapBreach must carry pattern, observed_count, and configured_cap."""
    breach = _make_breach()

    assert breach.pattern == PATTERN, f"pattern mismatch: {breach.pattern}"
    assert breach.observed_count == OBSERVED_OVER_CAP, (
        f"observed_count mismatch: {breach.observed_count}"
    )
    assert breach.configured_cap == CAP_VALUE, (
        f"configured_cap mismatch: {breach.configured_cap}"
    )


# ---------------------------------------------------------------------------
# Test 2 — cap_breach_response() returns MCP envelope with pattern in text
# ---------------------------------------------------------------------------


def test_cap_breach_response_returns_mcp_envelope_with_all_fields_and_pattern():
    """cap_breach_response() must return a dict with MCP content array wrapping
    a JSON payload with success=False, error=wildcard_cap_exceeded, cap, observed,
    and the pattern string embedded in the text for actionable client messaging.
    """
    import json
    from code_indexer.server.mcp.handlers._utils import cap_breach_response

    response = cap_breach_response(_make_breach())

    assert "content" in response, f"Missing 'content' key in MCP response: {response}"
    content_list = response["content"]
    assert isinstance(content_list, list) and len(content_list) == 1, (
        f"MCP 'content' must be a list of one element: {content_list}"
    )
    item = content_list[0]
    assert item.get("type") == "text", f"content item type must be 'text': {item}"

    payload = json.loads(item["text"])
    assert payload.get(MCP_SUCCESS_FIELD) is False, f"success must be False: {payload}"
    assert payload.get(MCP_ERROR_FIELD) == MCP_ERROR_CODE, (
        f"error must be {MCP_ERROR_CODE!r}: {payload}"
    )
    assert payload.get(MCP_CAP_FIELD) == CAP_VALUE, (
        f"cap field must be {CAP_VALUE}: {payload}"
    )
    assert payload.get(MCP_OBSERVED_FIELD) == OBSERVED_OVER_CAP, (
        f"observed field must be {OBSERVED_OVER_CAP}: {payload}"
    )
    assert PATTERN in item["text"], (
        f"Pattern {PATTERN!r} must appear in MCP response text for actionable messaging: {item['text']}"
    )


# ---------------------------------------------------------------------------
# Test 3 — cap_breach_http_exception() raises HTTPException with 400
# ---------------------------------------------------------------------------


def test_cap_breach_http_exception_raises_400_with_actionable_detail():
    """cap_breach_http_exception() must raise HTTPException(status_code=400).

    The detail must contain the pattern name, observed count, and configured cap
    so that the client knows exactly how to fix the request.
    """
    from fastapi import HTTPException
    from code_indexer.server.mcp.handlers._utils import cap_breach_http_exception

    with pytest.raises(HTTPException) as exc_info:
        cap_breach_http_exception(_make_breach())

    exc = exc_info.value
    assert exc.status_code == HTTP_400, (
        f"HTTPException status_code must be {HTTP_400}, got {exc.status_code}"
    )
    detail_text = str(exc.detail)
    assert PATTERN in detail_text, (
        f"Pattern {PATTERN!r} must appear in HTTPException detail: {detail_text}"
    )
    assert str(OBSERVED_OVER_CAP) in detail_text, (
        f"observed_count {OBSERVED_OVER_CAP} must appear in HTTPException detail: {detail_text}"
    )
    assert str(CAP_VALUE) in detail_text, (
        f"configured_cap {CAP_VALUE} must appear in HTTPException detail: {detail_text}"
    )


# ---------------------------------------------------------------------------
# Test 4 — _check_wildcard_cap returns None when at cap and when well under cap
# ---------------------------------------------------------------------------


def test_check_wildcard_cap_returns_none_when_at_cap():
    """_check_wildcard_cap must return None when observed_count <= configured_cap.

    At the boundary (exactly at cap) the request is still valid.
    Well under cap is trivially valid.
    """
    from code_indexer.server.mcp.handlers._utils import _check_wildcard_cap

    result_at_cap = _check_wildcard_cap(PATTERN, OBSERVED_AT_CAP, CAP_VALUE)
    assert result_at_cap is None, (
        f"Expected None for observed={OBSERVED_AT_CAP} at cap={CAP_VALUE}, "
        f"got {result_at_cap!r}"
    )

    result_under = _check_wildcard_cap(PATTERN, OBSERVED_WELL_UNDER_CAP, CAP_VALUE)
    assert result_under is None, (
        f"Expected None for observed={OBSERVED_WELL_UNDER_CAP} at cap={CAP_VALUE}, "
        f"got {result_under!r}"
    )


# ---------------------------------------------------------------------------
# Test 5 — _check_wildcard_cap returns CapBreach when over cap
# ---------------------------------------------------------------------------


def test_check_wildcard_cap_returns_cap_breach_when_over_cap():
    """_check_wildcard_cap must return a CapBreach when observed_count > configured_cap.

    The returned CapBreach must carry the supplied pattern, observed_count, and configured_cap.
    """
    from code_indexer.server.mcp.handlers._utils import CapBreach, _check_wildcard_cap

    result = _check_wildcard_cap(PATTERN, OBSERVED_OVER_CAP, CAP_VALUE)

    assert isinstance(result, CapBreach), (
        f"Expected CapBreach for observed={OBSERVED_OVER_CAP} > cap={CAP_VALUE}, "
        f"got {type(result).__name__} {result!r}"
    )
    assert result.pattern == PATTERN, f"pattern mismatch: {result.pattern}"
    assert result.observed_count == OBSERVED_OVER_CAP, (
        f"observed_count mismatch: {result.observed_count}"
    )
    assert result.configured_cap == CAP_VALUE, (
        f"configured_cap mismatch: {result.configured_cap}"
    )
