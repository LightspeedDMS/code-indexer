"""Tests for Story #993 Improvement 3: ast_node enrichment in _evaluate_file.

AC3.1: Content search match has ast_node with correct byte range populated
       in match_positions before evaluator is called.
AC3.2: Filename search (empty match_positions) completes without error.
AC3.3: Evaluator can call ast_node.enclosing() on the enriched ast_node.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JAVA_GETCONNECTION = """\
public class Demo {
    public void setup() {
        Connection c = getConnection();
    }
}
"""

_JAVA_TRY_WITH_RESOURCES = """\
public class Demo {
    public void setup() throws Exception {
        try (Connection c = getConnection()) {
            c.close();
        }
    }
}
"""


def _evaluate(
    evaluator_code: str,
    tmp_path: Path,
    file_content: str,
    match_positions: list,
    lang: str = "java",
):
    """Call XRaySearchEngine._evaluate_file and return (matches, errors, file_meta)."""
    from code_indexer.xray.search_engine import XRaySearchEngine

    file_path = tmp_path / "Demo.java"
    file_path.write_text(file_content, encoding="utf-8")
    engine = XRaySearchEngine()
    return engine._evaluate_file(
        file_path=file_path,
        lang=lang,
        source=file_content,
        match_positions=match_positions,
        evaluator_code=evaluator_code,
        context_lines=0,
        include_ast_debug=False,
        max_debug_nodes=50,
    )


# ===========================================================================
# AC3.1: Content match has ast_node with correct byte range
# ===========================================================================


def test_ac3_1_content_match_has_ast_node(tmp_path: Path) -> None:
    """AC3.1: match_positions entries get ast_node populated with a valid node
    whose byte range contains the match byte offset."""
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

    evaluator_code = """\
result_matches = []
for pos in match_positions:
    an = pos.get("ast_node")
    if an is not None:
        result_matches.append({
            "line_number": pos["line_number"],
            "ast_node_type": an.type,
            "ast_node_start": an.start_byte,
            "ast_node_end": an.end_byte,
        })
return {"matches": result_matches, "value": None}
"""

    # "getConnection" appears on line 3 of _JAVA_GETCONNECTION
    matches, errors, _ = _evaluate(
        evaluator_code=evaluator_code,
        tmp_path=tmp_path,
        file_content=_JAVA_GETCONNECTION,
        match_positions=[{"line_number": 3}],
    )

    assert errors == [], f"Unexpected errors: {errors}"
    assert len(matches) == 1, f"Expected 1 match, got {matches}"

    m = matches[0]
    assert "ast_node_type" in m, "ast_node_type missing from match"
    assert isinstance(m["ast_node_type"], str), "ast_node_type must be a string"
    assert len(m["ast_node_type"]) > 0, "ast_node_type must be non-empty"

    # Byte range must be valid (start < end)
    assert m["ast_node_start"] < m["ast_node_end"], (
        f"ast_node byte range invalid: start={m['ast_node_start']}, end={m['ast_node_end']}"
    )

    # The node's byte range must contain the byte_offset of line 3
    # (we don't know exact offset, but start_byte <= byte_offset < end_byte)
    byte_offset_line3 = _JAVA_GETCONNECTION.encode("utf-8").index(b"getConnection")
    assert m["ast_node_start"] <= byte_offset_line3 < m["ast_node_end"], (
        f"ast_node range [{m['ast_node_start']}, {m['ast_node_end']}) "
        f"does not contain byte_offset {byte_offset_line3}"
    )


# ===========================================================================
# AC3.2: Filename search (empty match_positions) → no error
# ===========================================================================


def test_ac3_2_empty_match_positions_no_error(tmp_path: Path) -> None:
    """AC3.2: When match_positions is empty (filename-target mode), _evaluate_file
    completes normally without crashing."""
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

    evaluator_code = "return {'matches': [], 'value': None}"

    matches, errors, _ = _evaluate(
        evaluator_code=evaluator_code,
        tmp_path=tmp_path,
        file_content=_JAVA_GETCONNECTION,
        match_positions=[],
    )

    assert errors == [], f"Unexpected errors: {errors}"
    assert matches == []


# ===========================================================================
# AC3.3: Evaluator can call ast_node.enclosing() on enriched node
# ===========================================================================


def test_ac3_3_evaluator_can_call_enclosing(tmp_path: Path) -> None:
    """AC3.3: Evaluator can call ast_node.enclosing('try_with_resources_statement')
    and get a valid node (not None) for matches inside a try-with-resources block."""
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

    evaluator_code = """\
result_matches = []
for pos in match_positions:
    an = pos.get("ast_node")
    if an is not None:
        enclosing = an.enclosing("try_with_resources_statement")
        result_matches.append({
            "line_number": pos["line_number"],
            "has_enclosing_try": enclosing is not None,
        })
return {"matches": result_matches, "value": None}
"""

    # "getConnection" is on line 4 in _JAVA_TRY_WITH_RESOURCES, inside try-with-resources
    matches, errors, _ = _evaluate(
        evaluator_code=evaluator_code,
        tmp_path=tmp_path,
        file_content=_JAVA_TRY_WITH_RESOURCES,
        match_positions=[{"line_number": 4}],
    )

    assert errors == [], f"Unexpected errors: {errors}"
    assert len(matches) == 1, f"Expected 1 match, got {matches}"
    assert matches[0]["has_enclosing_try"] is True, (
        "Expected ast_node.enclosing('try_with_resources_statement') to return "
        f"a non-None node for a match inside try-with-resources, got: {matches[0]}"
    )
