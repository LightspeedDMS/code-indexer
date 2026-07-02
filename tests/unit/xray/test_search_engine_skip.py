"""Tests for Story #993 Improvements 4 and 6 to XRaySearchEngine._evaluate_file.

Improvement 4: Early bail-out when evaluator returns {"skip": True}
  AC4.1: {"skip": True} → matches=[], errors=[], file_meta=None
  AC4.2: {"skip": True, "matches": [...], "value": "x"} → skip takes precedence
  AC4.3: {"matches": [...], "value": None} without skip → existing behavior unchanged

Improvement 6: file_role tagging in file_metadata
  AC6.1: {"matches": [], "value": None, "file_role": "connection_factory"} →
         file_metadata contains file_role
  AC6.2: {"matches": [...], "value": "x"} without file_role →
         file_metadata has value but NO file_role key
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _evaluate(
    evaluator_code: str,
    tmp_path: Path,
    file_content: str = "def foo(): pass",
):
    """Call XRaySearchEngine._evaluate_file and return (matches, errors, file_meta)."""
    from code_indexer.xray.search_engine import XRaySearchEngine

    file_path = tmp_path / "sample.py"
    file_path.write_text(file_content, encoding="utf-8")
    engine = XRaySearchEngine()
    return engine._evaluate_file(
        file_path=file_path,
        lang="python",
        source=file_content,
        match_positions=[{"line_number": 1}],
        evaluator_code=evaluator_code,
        context_lines=0,
        include_ast_debug=False,
        max_debug_nodes=50,
    )


# ===========================================================================
# Improvement 4: Early bail-out on skip: True (AC4.1, AC4.2, AC4.3)
# ===========================================================================


@pytest.mark.parametrize(
    "evaluator_code, expected_match_count, expected_file_meta",
    [
        # AC4.1: bare skip=True → empty result
        ("return {'skip': True}", 0, None),
        # AC4.2: skip=True overrides matches and value
        (
            "return {'skip': True, 'matches': [{'line_number': 1}], 'value': 'x'}",
            0,
            None,
        ),
        # AC4.3: no skip key → existing behaviour (1 match, value=None → no file_meta)
        ("return {'matches': [{'line_number': 1}], 'value': None}", 1, None),
    ],
    ids=["ac4_1_bare_skip", "ac4_2_skip_overrides", "ac4_3_no_skip_unchanged"],
)
def test_skip_behaviour(
    tmp_path: Path,
    evaluator_code: str,
    expected_match_count: int,
    expected_file_meta: Optional[Any],
) -> None:
    """AC4.1/AC4.2/AC4.3: skip=True bail-out and no-skip baseline."""
    matches, errors, file_meta = _evaluate(evaluator_code, tmp_path)
    assert len(matches) == expected_match_count
    assert errors == []
    assert file_meta == expected_file_meta


# ===========================================================================
# Improvement 6: file_role tagging (AC6.1, AC6.2)
# ===========================================================================


@pytest.mark.parametrize(
    "evaluator_code, expected_value, expected_file_role",
    [
        # AC6.1: file_role present, value=None → file_meta has file_role, no value key
        (
            "return {'matches': [], 'value': None, 'file_role': 'connection_factory'}",
            None,
            "connection_factory",
        ),
        # AC6.2: file_role absent → file_meta has value but no file_role key
        (
            "return {'matches': [{'line_number': 1}], 'value': 'data'}",
            "data",
            None,
        ),
    ],
    ids=["ac6_1_file_role_present", "ac6_2_file_role_absent"],
)
def test_file_role_tagging(
    tmp_path: Path,
    evaluator_code: str,
    expected_value: Optional[Any],
    expected_file_role: Optional[str],
) -> None:
    """AC6.1/AC6.2: file_role tagging in file_metadata."""
    _, _, file_meta = _evaluate(evaluator_code, tmp_path)
    assert file_meta is not None, (
        f"Expected file_meta to be set for code: {evaluator_code!r}"
    )
    if expected_value is not None:
        assert file_meta.get("value") == expected_value
    else:
        assert "value" not in file_meta
    if expected_file_role is not None:
        assert file_meta.get("file_role") == expected_file_role
    else:
        assert "file_role" not in file_meta
