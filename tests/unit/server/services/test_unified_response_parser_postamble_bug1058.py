"""
Unit tests for UnifiedResponseParser postamble stripping — Bug #1058.

The parser had _strip_preamble (strips prose before {) but no _strip_postamble
(strips prose after }).  Production logged 17/17 lifecycle-batch calls failing
with "Extra data: line 2 column 1" because Claude appended trailing commentary
after the JSON object.

Five tests cover the approved fix shape:
  1. Trailing one-line prose after JSON succeeds.
  2. Prose before AND after JSON succeeds (preamble + postamble compose).
  3. String literal containing a brace is not mis-truncated.
  4. String literal with escaped quote then brace is handled correctly.
  5. Code-fenced JSON with trailing prose — fence-strip + postamble-strip compose.
"""

import json

from code_indexer.global_repos.unified_response_parser import UnifiedResponseParser

# ---------------------------------------------------------------------------
# Minimal-but-valid lifecycle JSON fixture (all required fields present).
# Uses v3 optional sections (branching, ci, release) so the schema validator
# inside parse() does not reject the fixture for unrelated reasons.
# ---------------------------------------------------------------------------

_LIFECYCLE_OBJ = {
    "description": "A repository that does important things for the business.",
    "lifecycle": {
        "ci_system": "GitHub Actions",
        "deployment_target": "AWS",
        "language_ecosystem": "Python",
        "build_system": "pip",
        "testing_framework": "pytest",
        "confidence": "high",
        "branching": {
            "default_branch": "main",
            "model": "github-flow",
            "release_branch_pattern": None,
            "protected_branches": ["main"],
        },
        "ci": {
            "trigger_events": ["push", "pull_request"],
            "required_checks": ["lint", "test"],
            "deploy_on": "merge-to-main",
            "environments": ["staging", "production"],
        },
        "release": {
            "versioning": "semver",
            "version_source": "pyproject.toml",
            "changelog": "CHANGELOG.md",
            "auto_publish": False,
            "artifact_types": ["wheel"],
        },
    },
}

_LIFECYCLE_JSON = json.dumps(_LIFECYCLE_OBJ, indent=2)


class TestStripPostambleBug1058:
    """Tests for _strip_postamble and its integration with parse()."""

    # ------------------------------------------------------------------
    # Test 1: trailing one-line prose after JSON → parse succeeds
    # ------------------------------------------------------------------

    def test_trailing_one_line_prose_after_json_succeeds(self) -> None:
        """
        When Claude appends a closing sentence after the JSON object,
        parse() must return a UnifiedResult with the correct fields
        and must NOT raise UnifiedResponseParseError.

        This is the exact failure shape from the production logs:
        char offset ~2KB = JSON ends, then prose follows.
        """
        trailing_prose = "\nLet me know if you have questions about this repository!\n"
        raw = _LIFECYCLE_JSON + trailing_prose

        result = UnifiedResponseParser.parse(raw)

        assert result.description == _LIFECYCLE_OBJ["description"]
        assert result.lifecycle["ci_system"] == "GitHub Actions"
        assert result.lifecycle["confidence"] == "high"

    # ------------------------------------------------------------------
    # Test 2: prose before AND after JSON → compose correctly
    # ------------------------------------------------------------------

    def test_prose_before_and_after_json_succeeds(self) -> None:
        """
        _strip_preamble removes leading prose; _strip_postamble removes trailing
        prose.  Both applied together must produce a clean round-trip.
        """
        preamble = "Here is the repository analysis you requested:\n\n"
        postamble = (
            "\n\nI hope this analysis is helpful. Feel free to ask follow-up questions."
        )
        raw = preamble + _LIFECYCLE_JSON + postamble

        result = UnifiedResponseParser.parse(raw)

        assert result.description == _LIFECYCLE_OBJ["description"]
        assert result.lifecycle["deployment_target"] == "AWS"

    # ------------------------------------------------------------------
    # Test 3: brace inside a JSON string value → must NOT truncate early
    # ------------------------------------------------------------------

    def test_string_literal_with_brace_does_not_truncate_inside_json(self) -> None:
        """
        If a JSON string value contains a '}' character (e.g. a description
        like "method body }: ..."), _strip_postamble must not treat that brace
        as the closing brace of the top-level object.  The full JSON must parse.

        The postamble stripper must track whether it is inside a JSON string
        and skip braces that appear within string literals.
        """
        obj_with_brace_in_string = dict(_LIFECYCLE_OBJ)
        obj_with_brace_in_string["description"] = (
            "This repo uses method body }: unusual syntax in comments."
        )
        json_with_brace = json.dumps(obj_with_brace_in_string, indent=2)
        trailing_prose = "\nThat covers the analysis.\n"
        raw = json_with_brace + trailing_prose

        result = UnifiedResponseParser.parse(raw)

        assert "method body }:" in result.description

    # ------------------------------------------------------------------
    # Test 4: escaped quote inside string then brace → string-state correct
    # ------------------------------------------------------------------

    def test_string_literal_with_escaped_quote_then_brace(self) -> None:
        """
        String-state tracking must handle escaped quotes (\\") so that a
        quote preceded by a backslash does not terminate the string-scan
        state.  Input: description contains 'a\\"b}c' followed by trailing prose.
        The brace inside the string must NOT trigger early truncation.
        """
        # Construct a description with an escaped quote followed by a brace.
        # json.dumps will emit the string with proper JSON escaping.
        obj_escaped = dict(_LIFECYCLE_OBJ)
        obj_escaped["description"] = 'Analysis: a"b}c — repo uses quote-brace combos.'
        json_escaped = json.dumps(obj_escaped, indent=2)
        # Verify json.dumps included the escaped quote so the test is non-trivial
        assert '\\"' in json_escaped or '"b}c' in json_escaped
        trailing_prose = "\nEnd of analysis.\n"
        raw = json_escaped + trailing_prose

        result = UnifiedResponseParser.parse(raw)

        assert "b}c" in result.description

    # ------------------------------------------------------------------
    # Test 5: code-fenced JSON with trailing prose → both strips compose
    # ------------------------------------------------------------------

    def test_code_fenced_json_with_trailing_prose(self) -> None:
        """
        When Claude wraps JSON in a ```json ... ``` fence AND appends trailing
        prose AFTER the closing fence, _strip_code_fence removes the fence and
        _strip_postamble removes the prose.  Both compose correctly.
        """
        fenced = f"```json\n{_LIFECYCLE_JSON}\n```"
        trailing_prose = "\nThis completes my analysis of the repository."
        raw = fenced + trailing_prose

        result = UnifiedResponseParser.parse(raw)

        assert result.description == _LIFECYCLE_OBJ["description"]
        assert result.lifecycle["testing_framework"] == "pytest"
