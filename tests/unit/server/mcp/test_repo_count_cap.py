"""
Tests for total-fan-out cap enforcement in omni/multi search.

Bug #894: omni_max_repos_per_search was documented in CLAUDE.md but never
implemented. This enforces a per-search-total alias ceiling AFTER wildcard
expansion + literal union, BEFORE the fan-out loop.

Mirror structure of test_wildcard_cap.py.  Named constants throughout —
no magic numbers.
"""

import json
import pytest
from typing import List
from unittest.mock import MagicMock, patch


# Named constants — no magic numbers
DEFAULT_CAP = 50
COUNT_ONE_OVER_DEFAULT_CAP = DEFAULT_CAP + 1  # 51 — rejected at cap=50
COUNT_AT_DEFAULT_CAP = DEFAULT_CAP  # 50 — boundary, allowed

# Test 2 uses a custom cap to model wildcard(25)+literal(10)=35 > cap=30
COMBINED_CAP = 30
COMBINED_WILDCARD_COUNT = 25  # fits wildcard_expansion_cap alone
COMBINED_LITERAL_COUNT = 10  # fits alone too
COMBINED_TOTAL = COMBINED_WILDCARD_COUNT + COMBINED_LITERAL_COUNT  # 35 > 30

HTTP_400 = 400

MCP_SUCCESS_FIELD = "success"
MCP_ERROR_FIELD = "error"
MCP_ERROR_CODE = "repo_count_cap_exceeded"


def _make_alias_list(count: int) -> List[str]:
    """Return a list of count unique alias strings."""
    return [f"repo-{i:04d}-global" for i in range(count)]


def _make_repo_count_cap_config(cap: int) -> MagicMock:
    """Return a mock config service with omni_max_repos_per_search=cap."""
    mock_config_svc = MagicMock()
    mock_config_svc.get_config.return_value.multi_search_limits_config.omni_max_repos_per_search = cap
    return mock_config_svc


def _run_enforce_repo_count_cap(aliases: List[str], cap: int):
    """Run _enforce_repo_count_cap with config patched to the given cap."""
    from code_indexer.server.mcp.handlers._utils import _enforce_repo_count_cap

    with patch(
        "code_indexer.server.mcp.handlers._utils.get_config_service",
        return_value=_make_repo_count_cap_config(cap),
    ):
        return _enforce_repo_count_cap(aliases)


# ---------------------------------------------------------------------------
# Test 1 — 51 literal aliases trigger CapBreach at default cap=50
# ---------------------------------------------------------------------------


def test_cap_triggers_on_51_literal_aliases():
    """51 literal aliases must return CapBreach when omni_max_repos_per_search=50.

    Literal aliases bypass wildcard_expansion_cap but must be caught by the
    total-fan-out cap (Bug #894).

    The CapBreach must carry the alias count (observed_count) and configured cap.
    """
    from code_indexer.server.mcp.handlers._utils import CapBreach

    aliases = _make_alias_list(COUNT_ONE_OVER_DEFAULT_CAP)
    result = _run_enforce_repo_count_cap(aliases, DEFAULT_CAP)

    assert isinstance(result, CapBreach), (
        f"Expected CapBreach for {COUNT_ONE_OVER_DEFAULT_CAP} aliases vs cap={DEFAULT_CAP}, "
        f"got: {type(result).__name__} {result!r}"
    )
    assert result.observed_count == COUNT_ONE_OVER_DEFAULT_CAP, (
        f"observed_count expected {COUNT_ONE_OVER_DEFAULT_CAP}, got {result.observed_count}"
    )
    assert result.configured_cap == DEFAULT_CAP, (
        f"configured_cap expected {DEFAULT_CAP}, got {result.configured_cap}"
    )


# ---------------------------------------------------------------------------
# Test 2 — wildcard(25)+literal(10)=35 > cap=30 triggers CapBreach
# ---------------------------------------------------------------------------


def test_cap_triggers_on_combined_wildcard_plus_literal_exceeding_cap():
    """Cap=30 fires when wildcard expansion gives 25 + 10 literals = 35 total.

    Each source is individually under cap (25 < 30, 10 < 30) but the union
    is 35 > 30.  This is the scenario wildcard_expansion_cap cannot catch
    because it checks per-pattern, not per-search-total.

    The merged final list (as passed by the caller after expansion+union) is
    modelled as a flat 35-element alias list.
    """
    from code_indexer.server.mcp.handlers._utils import CapBreach

    # Simulate the merged list after wildcard expansion and literal union
    wildcard_aliases = [f"wild-{i:03d}-global" for i in range(COMBINED_WILDCARD_COUNT)]
    literal_aliases = [f"literal-{i:03d}-global" for i in range(COMBINED_LITERAL_COUNT)]
    merged = wildcard_aliases + literal_aliases  # 35 unique aliases

    assert len(merged) == COMBINED_TOTAL

    result = _run_enforce_repo_count_cap(merged, COMBINED_CAP)

    assert isinstance(result, CapBreach), (
        f"Expected CapBreach for combined list of {len(merged)} aliases vs cap={COMBINED_CAP}, "
        f"got: {type(result).__name__} {result!r}"
    )
    assert result.observed_count == COMBINED_TOTAL, (
        f"observed_count expected {COMBINED_TOTAL}, got {result.observed_count}"
    )
    assert result.configured_cap == COMBINED_CAP, (
        f"configured_cap expected {COMBINED_CAP}, got {result.configured_cap}"
    )


# ---------------------------------------------------------------------------
# Test 3 — exactly 50 aliases is NOT a breach (inclusive boundary)
# ---------------------------------------------------------------------------


def test_cap_not_triggered_at_exactly_50():
    """Exactly 50 aliases must NOT trigger CapBreach (inclusive boundary).

    observed_count == configured_cap is allowed (mirrors wildcard cap semantics).
    """
    aliases = _make_alias_list(COUNT_AT_DEFAULT_CAP)
    result = _run_enforce_repo_count_cap(aliases, DEFAULT_CAP)

    assert result is None, (
        f"Unexpected CapBreach for exactly {COUNT_AT_DEFAULT_CAP} aliases at cap={DEFAULT_CAP}: "
        f"{result!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — CapBreach envelope shape for MCP path
# ---------------------------------------------------------------------------


def test_cap_breach_envelope_shape_mcp_structured():
    """CapBreach from _enforce_repo_count_cap must produce a valid MCP envelope.

    cap_breach_response(breach) must return:
      {"content": [{"type": "text", "text": "<json>"}]}
    where the JSON carries success=False, error=repo_count_cap_exceeded,
    observed == COUNT_ONE_OVER_DEFAULT_CAP, and cap == DEFAULT_CAP.
    """
    from code_indexer.server.mcp.handlers._utils import CapBreach, cap_breach_response

    aliases = _make_alias_list(COUNT_ONE_OVER_DEFAULT_CAP)
    breach = _run_enforce_repo_count_cap(aliases, DEFAULT_CAP)

    assert isinstance(breach, CapBreach), (
        f"Expected CapBreach to test envelope shape, got {type(breach).__name__}"
    )

    envelope = cap_breach_response(breach)

    assert "content" in envelope, f"Missing 'content' key in MCP response: {envelope}"
    content_list = envelope["content"]
    assert isinstance(content_list, list) and len(content_list) == 1, (
        f"MCP 'content' must be a one-element list: {content_list}"
    )
    item = content_list[0]
    assert item.get("type") == "text", f"content item type must be 'text': {item}"

    payload = json.loads(item["text"])
    assert payload.get(MCP_SUCCESS_FIELD) is False, f"success must be False: {payload}"
    assert payload.get(MCP_ERROR_FIELD) == MCP_ERROR_CODE, (
        f"error must be {MCP_ERROR_CODE!r}, got {payload.get(MCP_ERROR_FIELD)!r}: {payload}"
    )
    assert payload.get("cap") == DEFAULT_CAP, (
        f"cap field must be {DEFAULT_CAP}: {payload}"
    )
    assert payload.get("observed") == COUNT_ONE_OVER_DEFAULT_CAP, (
        f"observed field must be {COUNT_ONE_OVER_DEFAULT_CAP}: {payload}"
    )


# ---------------------------------------------------------------------------
# Test 5 — REST path: cap_breach_http_exception raises HTTP 400
# ---------------------------------------------------------------------------


def test_cap_breach_envelope_shape_rest_http_400():
    """cap_breach_http_exception(breach) must raise HTTPException with status 400.

    The detail must contain the observed count and configured cap so the client
    knows exactly how to fix the request.
    """
    from fastapi import HTTPException
    from code_indexer.server.mcp.handlers._utils import (
        CapBreach,
        cap_breach_http_exception,
    )

    aliases = _make_alias_list(COUNT_ONE_OVER_DEFAULT_CAP)
    breach = _run_enforce_repo_count_cap(aliases, DEFAULT_CAP)

    assert isinstance(breach, CapBreach), (
        f"Expected CapBreach to test REST path, got {type(breach).__name__}"
    )

    with pytest.raises(HTTPException) as exc_info:
        cap_breach_http_exception(breach)

    exc = exc_info.value
    assert exc.status_code == HTTP_400, (
        f"HTTPException status_code must be {HTTP_400}, got {exc.status_code}"
    )
    detail_text = str(exc.detail)
    assert str(COUNT_ONE_OVER_DEFAULT_CAP) in detail_text, (
        f"observed_count {COUNT_ONE_OVER_DEFAULT_CAP} must appear in detail: {detail_text}"
    )
    assert str(DEFAULT_CAP) in detail_text, (
        f"configured_cap {DEFAULT_CAP} must appear in detail: {detail_text}"
    )
