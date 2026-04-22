"""
Tests for wildcard expansion cap enforcement in _expand_wildcard_patterns.

Bug #881 Phase 3: _expand_wildcard_patterns must enforce omni_wildcard_expansion_cap
(default 50) so that wildcard MCP calls cannot fan out to unbounded repository counts
and cause HNSW cache memory exhaustion.

Cap enforcement returns a CapBreach when exceeded; callers convert it to the
appropriate error format (MCP envelope on MCP path, HTTPException on REST path).

Named constants throughout — no magic numbers.
"""

import tempfile
from typing import List
from unittest.mock import MagicMock, patch


# Named constants — no magic numbers in tests
DEFAULT_CAP = 50
SMALL_CAP = 10
LARGE_CAP = 100
COUNT_ONE_OVER_DEFAULT_CAP = DEFAULT_CAP + 1  # 51 — rejected at cap=50
COUNT_AT_DEFAULT_CAP = DEFAULT_CAP  # 50 — allowed at cap=50 (boundary)
COUNT_ONE_OVER_SMALL_CAP = SMALL_CAP + 1  # 11 — rejected at cap=10
COUNT_WELL_UNDER_LARGE_CAP = DEFAULT_CAP  # 50 — allowed at cap=100
WILDCARD_PATTERN = "*-global"
LITERAL_REPOS = ["repo-alpha-global", "repo-beta-global"]


def _make_fake_repos(count: int) -> List[dict]:
    """Return count fake repo dicts with alias_name ending in -global."""
    return [{"alias_name": f"repo-{i:04d}-global"} for i in range(count)]


def _make_user(username: str = "alice") -> MagicMock:
    user = MagicMock()
    user.username = username
    return user


def _make_cap_config(cap: int) -> MagicMock:
    """Return a mock config service whose get_config() reports omni_wildcard_expansion_cap=cap."""
    mock_config_svc = MagicMock()
    mock_config_svc.get_config.return_value.multi_search_limits_config.omni_wildcard_expansion_cap = cap
    return mock_config_svc


def _run_expand(patterns: List[str], fake_repos: List[dict], cap: int):
    """Run _expand_wildcard_patterns with all external dependencies patched."""
    from code_indexer.server.mcp.handlers._utils import _expand_wildcard_patterns

    with tempfile.TemporaryDirectory() as fake_golden_dir:
        with (
            patch(
                "code_indexer.server.mcp.handlers._utils._list_global_repos",
                return_value=fake_repos,
            ),
            patch(
                "code_indexer.server.mcp.handlers._utils._get_golden_repos_dir",
                return_value=fake_golden_dir,
            ),
            patch(
                "code_indexer.server.mcp.handlers._utils._get_access_filtering_service",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._utils.get_config_service",
                return_value=_make_cap_config(cap),
            ),
        ):
            return _expand_wildcard_patterns(patterns, _make_user())


# ---------------------------------------------------------------------------
# Test 1 — cap=50 blocks 51 matches: CapBreach with correct fields
# ---------------------------------------------------------------------------


def test_cap_blocks_expansion_when_one_more_than_default_cap_matches():
    """Wildcard expansion with cap=50 must return a CapBreach when 51 repos match.

    The CapBreach must carry pattern, observed_count (51), and configured_cap (50).
    """
    from code_indexer.server.mcp.handlers._utils import CapBreach

    result = _run_expand(
        [WILDCARD_PATTERN],
        _make_fake_repos(COUNT_ONE_OVER_DEFAULT_CAP),
        DEFAULT_CAP,
    )

    assert isinstance(result, CapBreach), (
        f"Expected CapBreach for {COUNT_ONE_OVER_DEFAULT_CAP} repos vs cap={DEFAULT_CAP}, "
        f"got: {type(result).__name__} {result!r}"
    )
    assert result.pattern == WILDCARD_PATTERN, f"pattern mismatch: {result.pattern}"
    assert result.observed_count == COUNT_ONE_OVER_DEFAULT_CAP, (
        f"observed_count expected {COUNT_ONE_OVER_DEFAULT_CAP}, got {result.observed_count}"
    )
    assert result.configured_cap == DEFAULT_CAP, (
        f"configured_cap expected {DEFAULT_CAP}, got {result.configured_cap}"
    )


# ---------------------------------------------------------------------------
# Test 2 — cap=50 allows exactly 50 matches (boundary: cap is inclusive)
# ---------------------------------------------------------------------------


def test_cap_allows_expansion_at_exactly_default_cap():
    """Wildcard expansion with cap=50 must succeed when exactly 50 repos match.

    The boundary is inclusive: exactly cap matches is NOT a breach.
    """
    from code_indexer.server.mcp.handlers._utils import CapBreach

    result = _run_expand(
        [WILDCARD_PATTERN],
        _make_fake_repos(COUNT_AT_DEFAULT_CAP),
        DEFAULT_CAP,
    )

    assert not isinstance(result, CapBreach), (
        f"Unexpected CapBreach for exactly {COUNT_AT_DEFAULT_CAP} repos at cap={DEFAULT_CAP}"
    )
    assert isinstance(result, list), f"Expected list, got {type(result).__name__}"
    assert len(result) == COUNT_AT_DEFAULT_CAP, (
        f"Expected {COUNT_AT_DEFAULT_CAP} aliases, got {len(result)}"
    )


# ---------------------------------------------------------------------------
# Test 3 — cap=10 blocks 11 matches
# ---------------------------------------------------------------------------


def test_small_cap_blocks_expansion_when_one_more_than_small_cap_matches():
    """Wildcard expansion with cap=10 must return a CapBreach when 11 repos match."""
    from code_indexer.server.mcp.handlers._utils import CapBreach

    result = _run_expand(
        [WILDCARD_PATTERN],
        _make_fake_repos(COUNT_ONE_OVER_SMALL_CAP),
        SMALL_CAP,
    )

    assert isinstance(result, CapBreach), (
        f"Expected CapBreach for {COUNT_ONE_OVER_SMALL_CAP} repos vs cap={SMALL_CAP}"
    )
    assert result.configured_cap == SMALL_CAP, (
        f"configured_cap expected {SMALL_CAP}, got {result.configured_cap}"
    )
    assert result.observed_count == COUNT_ONE_OVER_SMALL_CAP, (
        f"observed_count expected {COUNT_ONE_OVER_SMALL_CAP}, got {result.observed_count}"
    )


# ---------------------------------------------------------------------------
# Test 4 — cap=100 allows 50 matches (well under cap)
# ---------------------------------------------------------------------------


def test_large_cap_allows_expansion_well_under_cap():
    """Wildcard expansion with cap=100 must succeed when 50 repos match."""
    from code_indexer.server.mcp.handlers._utils import CapBreach

    result = _run_expand(
        [WILDCARD_PATTERN],
        _make_fake_repos(COUNT_WELL_UNDER_LARGE_CAP),
        LARGE_CAP,
    )

    assert not isinstance(result, CapBreach), (
        f"Unexpected CapBreach for {COUNT_WELL_UNDER_LARGE_CAP} repos at cap={LARGE_CAP}"
    )
    assert len(result) == COUNT_WELL_UNDER_LARGE_CAP


# ---------------------------------------------------------------------------
# Test 5 — literal repos bypass cap entirely
# ---------------------------------------------------------------------------


def test_literal_repos_bypass_cap_check():
    """Literal (non-wildcard) repo aliases must pass through without cap enforcement.

    The cap applies ONLY to wildcard expansion — not to explicit alias lists.
    Two literal repos with cap=10 and zero available repos must return the two literals unchanged.
    """
    from code_indexer.server.mcp.handlers._utils import CapBreach

    result = _run_expand(LITERAL_REPOS, [], SMALL_CAP)

    assert not isinstance(result, CapBreach), (
        "Literal repo list must not trigger CapBreach regardless of cap"
    )
    assert result == LITERAL_REPOS, (
        f"Literal repos should pass through unchanged: {result}"
    )
