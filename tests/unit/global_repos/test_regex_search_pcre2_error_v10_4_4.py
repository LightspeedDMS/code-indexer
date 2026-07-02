"""v10.4.4 tests for Finding 3.1 (Layer 1): regex_search ripgrep error surfacing.

Previously, when ripgrep returned exit_code >= 2 (invalid regex, bad PCRE2
pattern, etc.), RegexSearchService.search() logged a warning and returned
empty results silently. This caused xray_search to complete with COMPLETED
status and empty results — no signal to the caller.

Fix: Raise RipgrepExecutionError when ripgrep exits with a real error (exit_code
!= 1, or exit_code == 1 with stderr), preserving the existing no-match behavior
(exit_code=1, no stderr = silent empty return).

No mocking: Real ripgrep invocations are used (anti-mock principle). Invalid
patterns (both PCRE2 and standard) naturally produce exit_code=2 from ripgrep.
Valid no-match patterns produce exit_code=1 with no stderr (silent return).

Requires: ripgrep (rg) available on the system. Skips if not found.
"""

from __future__ import annotations

import shutil

import pytest

from code_indexer.global_repos.regex_search import (
    RegexSearchService,
    RipgrepExecutionError,
)

# ---------------------------------------------------------------------------
# Skip guard: these tests require real ripgrep
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    shutil.which("rg") is None,
    reason="ripgrep (rg) not available on this system",
)


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def regex_service(tmp_path):
    """Create a RegexSearchService over a repo with one Python file."""
    (tmp_path / "sample.py").write_text("def foo(): pass\n")
    return RegexSearchService(tmp_path)


# ---------------------------------------------------------------------------
# Tests: RipgrepExecutionError is raised on invalid patterns
# ---------------------------------------------------------------------------


class TestRipgrepExecutionError:
    """Invalid patterns cause ripgrep exit_code=2 → RipgrepExecutionError.

    Both PCRE2 and standard regex modes share the same error-handling code path
    in _search_ripgrep, so both are covered here.
    """

    @pytest.mark.asyncio
    async def test_invalid_pcre2_pattern_raises(self, regex_service):
        """Invalid PCRE2 pattern ([unclosed) → ripgrep exits with code 2 and
        stderr → service must raise RipgrepExecutionError, not return empty."""
        with pytest.raises(RipgrepExecutionError):
            await regex_service.search(pattern="[unclosed", pcre2=True)

    @pytest.mark.asyncio
    async def test_invalid_standard_regex_raises(self, regex_service):
        """Invalid standard regex ([unclosed) → same exit_code=2 code path →
        must raise RipgrepExecutionError."""
        with pytest.raises(RipgrepExecutionError):
            await regex_service.search(pattern="[unclosed", pcre2=False)


# ---------------------------------------------------------------------------
# Tests: normal "no matches" path is NOT affected
# ---------------------------------------------------------------------------


class TestNoMatchStillSilent:
    """exit_code=1 with no stderr (ripgrep 'no matches' code) → silent empty return."""

    @pytest.mark.asyncio
    async def test_valid_pattern_no_matches_returns_empty(self, regex_service):
        """A valid pattern that matches nothing returns empty silently (no raise)."""
        result = await regex_service.search(
            pattern="XYZZY_PATTERN_THAT_WILL_NEVER_MATCH_ANYTHING_12345"
        )
        assert result.matches == [], (
            f"Expected empty matches for no-match pattern, got {result.matches}"
        )
