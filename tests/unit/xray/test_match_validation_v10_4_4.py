"""v10.4.4 tests for Findings 3.3 and 3.4: match validation in _evaluate_file.

Finding 3.3: Evaluator returning {'matches': [{'no_line_number': 'x'}], 'value': None}
was accepted silently and passed through enrichment. line_number is a required field;
absence should return InvalidEvaluatorReturn for the entire file.

Finding 3.4: {'line_number': 'abc'} (non-int string) caused a ValueError with raw
Python error message instead of InvalidEvaluatorReturn with an actionable message.

Fix:
- Before enrichment, check 'line_number' presence → if missing, return
  InvalidEvaluatorReturn for the entire file.
- Wrap int() coercion in try/except ValueError → return InvalidEvaluatorReturn
  with actionable message if coercion fails.
- String digit ("2") must coerce to int(2) and pass enrichment normally.

Tests drive _evaluate_file directly. No mocking — anti-mock principle.
Requires tree_sitter_languages (skips otherwise).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

import pytest

from code_indexer.xray.search_engine import XRaySearchEngine


pytestmark = pytest.mark.importorskip("tree_sitter_languages")


# ---------------------------------------------------------------------------
# Source text: 5 lines, known content for enrichment verification
# ---------------------------------------------------------------------------

_SOURCE = "line_one = 1\nline_two = 2\nline_three = 3\nline_four = 4\nline_five = 5\n"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _call_evaluate_file(
    tmp_path: Path,
    evaluator_code: str,
    source_text: str = _SOURCE,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Write a Python file and call _evaluate_file with custom evaluator code.

    _evaluate_file reads the file itself; we only pass path, evaluator_code,
    and a representative match_positions list for line 1.

    Returns the (matches, errors, file_meta) tuple from _evaluate_file.
    """
    py_file = tmp_path / "sample.py"
    py_file.write_text(source_text)

    lines = source_text.splitlines()
    first_line_content = lines[0] if lines else ""

    engine = XRaySearchEngine()

    # cast: _evaluate_file's documented return shape (search_engine.py:382) is
    # the 3-tuple below, but mypy infers Any due to internal tree-sitter generics.
    # The cast preserves the helper's strict return type for caller assertions.
    return cast(
        Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]],
        engine._evaluate_file(
            file_path=py_file,
            evaluator_code=evaluator_code,
            include_ast_debug=False,
            max_debug_nodes=50,
            match_positions=[
                {
                    "line_number": 1,
                    "line_content": first_line_content,
                    "column": 0,
                    "byte_offset": 0,
                    "context_before": [],
                    "context_after": [],
                }
            ],
        ),
    )


# ---------------------------------------------------------------------------
# Finding 3.3: missing line_number returns InvalidEvaluatorReturn
# ---------------------------------------------------------------------------


class TestMissingLineNumber:
    """A match dict without 'line_number' must cause InvalidEvaluatorReturn."""

    def test_match_missing_line_number_rejected_as_invalid_evaluator_return(
        self, tmp_path
    ):
        """Evaluator returns match without line_number → InvalidEvaluatorReturn
        for the entire file, not a silently enriched match.
        """
        evaluator_code = 'return {"matches": [{"no_line_number": "x"}], "value": None}'
        matches, errors, _ = _call_evaluate_file(tmp_path, evaluator_code)

        assert matches == [], (
            f"Expected no matches on InvalidEvaluatorReturn, got {matches}"
        )
        assert len(errors) == 1, f"Expected 1 error, got {errors}"
        assert errors[0]["error_type"] == "InvalidEvaluatorReturn", (
            f"Expected InvalidEvaluatorReturn, got: {errors[0]['error_type']}"
        )
        assert "line_number" in errors[0]["error_message"].lower(), (
            f"Error message should mention 'line_number': {errors[0]['error_message']}"
        )

    def test_partial_invalid_matches_reject_entire_file_response(self, tmp_path):
        """If ANY match lacks line_number, entire file response is InvalidEvaluatorReturn.
        Partial enrichment (only the valid matches) must not occur.
        """
        evaluator_code = (
            'return {"matches": ['
            '  {"line_number": 1},'
            '  {"no_line_number": "x"},'
            '  {"line_number": 1},'
            '], "value": None}'
        )
        matches, errors, _ = _call_evaluate_file(tmp_path, evaluator_code)

        assert matches == [], (
            f"Expected no matches when any match has missing line_number, got {matches}"
        )
        assert len(errors) == 1, f"Expected 1 error, got {errors}"
        assert errors[0]["error_type"] == "InvalidEvaluatorReturn"

    def test_match_with_line_number_valid_passes(self, tmp_path):
        """Evaluator returning line_number=1 passes enrichment.

        Verifies enrichment ran: 'line_content' field added with correct content.
        """
        evaluator_code = 'return {"matches": [{"line_number": 1}], "value": None}'
        matches, errors, _ = _call_evaluate_file(tmp_path, evaluator_code)

        assert errors == [], f"Expected no errors for valid match, got {errors}"
        assert len(matches) == 1, f"Expected 1 match, got {matches}"
        assert matches[0]["line_number"] == 1
        assert "line_content" in matches[0], (
            f"Enrichment field 'line_content' must be added; got {matches[0]}"
        )
        assert matches[0]["line_content"] == "line_one = 1", (
            f"Expected line_content for line 1, got {matches[0]['line_content']!r}"
        )


# ---------------------------------------------------------------------------
# Finding 3.4: non-int line_number returns InvalidEvaluatorReturn
# ---------------------------------------------------------------------------


class TestLineNumberTypeCoercion:
    """line_number type coercion: non-numeric raises InvalidEvaluatorReturn."""

    def test_match_line_number_not_int_returns_invalid_evaluator_return(self, tmp_path):
        """line_number='abc' → InvalidEvaluatorReturn (NOT raw ValueError)."""
        evaluator_code = 'return {"matches": [{"line_number": "abc"}], "value": None}'
        matches, errors, _ = _call_evaluate_file(tmp_path, evaluator_code)

        assert matches == [], f"Expected no matches, got {matches}"
        assert len(errors) == 1, f"Expected 1 error, got {errors}"
        assert errors[0]["error_type"] == "InvalidEvaluatorReturn", (
            f"Expected InvalidEvaluatorReturn, not {errors[0]['error_type']!r}"
        )
        assert "line_number" in errors[0]["error_message"].lower(), (
            f"Error message should mention 'line_number': {errors[0]['error_message']}"
        )

    def test_match_line_number_int_passes(self, tmp_path):
        """line_number=2 (int) passes enrichment. line_content verified for line 2."""
        evaluator_code = 'return {"matches": [{"line_number": 2}], "value": None}'
        matches, errors, _ = _call_evaluate_file(tmp_path, evaluator_code)

        assert errors == [], f"Expected no errors for int line_number, got {errors}"
        assert len(matches) == 1, f"Expected 1 match, got {matches}"
        assert "line_content" in matches[0], "line_content enrichment must be present"
        assert matches[0]["line_content"] == "line_two = 2", (
            f"Expected line content for line 2, got {matches[0]['line_content']!r}"
        )

    def test_match_line_number_str_digit_passes(self, tmp_path):
        """line_number='2' (string digit) coerces to int(2) and enriches normally.

        Asserts: coercion succeeded (isinstance int, value==2), line_content correct.
        """
        evaluator_code = 'return {"matches": [{"line_number": "2"}], "value": None}'
        matches, errors, _ = _call_evaluate_file(tmp_path, evaluator_code)

        assert errors == [], (
            f"Expected no errors for str-digit line_number '2', got {errors}"
        )
        assert len(matches) == 1, f"Expected 1 match, got {matches}"
        assert isinstance(matches[0]["line_number"], int), (
            f"line_number must be coerced to int, got {type(matches[0]['line_number'])}"
        )
        assert matches[0]["line_number"] == 2, (
            f"Expected line_number==2 after str coercion, got {matches[0]['line_number']}"
        )
        assert matches[0]["line_content"] == "line_two = 2", (
            f"Expected line_content for line 2, got {matches[0]['line_content']!r}"
        )
