"""v10.4.4 tests for Finding 3.1 (Layer 2): XRaySearchEngine surfaces phase1 errors.

When Phase 1 (ripgrep content driver) raises RipgrepExecutionError (e.g. invalid
PCRE2 pattern), XRaySearchEngine.run() must NOT swallow the exception. Instead it
must return a result dict with:
  - phase1_failed: True
  - phase1_error: "<the error message>"
  - partial: True
  - matches: []

This ensures the background job result is not silently empty but explicitly signals
a Phase 1 failure.

No mocking: Real ripgrep is used. An invalid PCRE2 pattern naturally causes
RegexSearchService to raise RipgrepExecutionError (Layer 1 fix), which Layer 2
must catch and surface in the result dict.

Requires: ripgrep (rg) available on the system.
"""

from __future__ import annotations

import shutil

import pytest

from code_indexer.xray.search_engine import XRaySearchEngine


# ---------------------------------------------------------------------------
# Skip guard: require real ripgrep
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    shutil.which("rg") is None,
    reason="ripgrep (rg) not available on this system",
)


_DEFAULT_EVALUATOR = (
    'matches = [{"line_number": mp["line_number"]} for mp in match_positions]\n'
    'return {"matches": matches, "value": None}'
)


# ---------------------------------------------------------------------------
# Tests: phase1 error is surfaced in job result
# ---------------------------------------------------------------------------


class TestPhase1ErrorSurfaced:
    """RipgrepExecutionError from Phase 1 becomes phase1_failed in result dict."""

    def test_invalid_pcre2_regex_surfaces_phase1_failed(self, tmp_path):
        """An invalid PCRE2 pattern causes ripgrep to exit with code 2, which raises
        RipgrepExecutionError from RegexSearchService. XRaySearchEngine.run() must
        catch this and return phase1_failed=True — NOT a silently empty result.
        """
        pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

        (tmp_path / "sample.py").write_text("def foo(): pass\n")
        engine = XRaySearchEngine()

        result = engine.run(
            repo_path=tmp_path,
            driver_regex="[unclosed",
            evaluator_code=_DEFAULT_EVALUATOR,
            search_target="content",
            pcre2=True,
        )

        assert result.get("phase1_failed") is True, (
            f"Expected phase1_failed=True, got: {result}"
        )
        assert "phase1_error" in result, (
            f"Expected phase1_error key in result, got: {result}"
        )
        assert result.get("partial") is True, (
            f"Expected partial=True when phase1_failed, got: {result}"
        )
        assert result.get("matches") == [], (
            f"Expected empty matches on phase1 failure, got: {result.get('matches')}"
        )


# ---------------------------------------------------------------------------
# Regression: valid pattern produces no phase1 error fields
# ---------------------------------------------------------------------------


class TestValidPatternNoPhase1Error:
    """Valid patterns with no matches do NOT produce phase1_failed."""

    def test_valid_pattern_no_phase1_error(self, tmp_path):
        """A valid pattern that matches nothing returns a normal result —
        phase1_failed and phase1_error must be absent from the result dict.
        """
        pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

        (tmp_path / "sample.py").write_text("def foo(): pass\n")
        engine = XRaySearchEngine()

        result = engine.run(
            repo_path=tmp_path,
            driver_regex="XYZZY_NEVER_MATCHES_12345",
            evaluator_code=_DEFAULT_EVALUATOR,
            search_target="content",
        )

        assert "phase1_failed" not in result, (
            f"phase1_failed must be absent for valid no-match pattern, got: {result}"
        )
        assert "phase1_error" not in result, (
            f"phase1_error must be absent for valid no-match pattern, got: {result}"
        )
