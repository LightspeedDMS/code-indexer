"""
Tests for robust JSON extraction from noisy LLM (Claude CLI) responses.

Staging bug: cidx-server self-monitoring scans failed with
``{"error": "Scan failed: Invalid JSON response from Claude:
Expecting value: line 1 column 1 (char 0)", "status": "FAILURE"}``.

Root cause: pace-maker is installed on all cluster nodes, so the server's own
``claude -p`` invocations get a telemetry preamble injected at byte 0, e.g.::

    § △0.0 ◎surg ■other ◇1.0 ↻1

(and sometimes a ``Warning: no stdin data received ...`` line). The leading
``§`` makes ``json.loads`` raise exactly
``Expecting value: line 1 column 1 (char 0)``.

These tests pin the behavior of the reusable ``extract_json_from_llm_response``
helper that strips that noise and returns the real JSON payload, while still
raising a clear error for a genuinely empty/garbage response (anti-silent-failure).
"""

import pytest

from code_indexer.server.self_monitoring.llm_response_parser import (
    extract_json_from_llm_response,
)


# Realistic captured pace-maker telemetry preamble (the staging shape).
PACEMAKER_PREAMBLE = "§ △0.0 ◎surg ■other ◇1.0 ↻1"


class TestExtractJsonHappyPath:
    """Clean responses must still parse unchanged."""

    def test_plain_json_object(self):
        result = extract_json_from_llm_response(
            '{"status": "SUCCESS", "issues_created": 0}'
        )
        assert result == {"status": "SUCCESS", "issues_created": 0}

    def test_plain_json_array(self):
        result = extract_json_from_llm_response("[1, 2, 3]")
        assert result == [1, 2, 3]

    def test_surrounding_whitespace(self):
        result = extract_json_from_llm_response('\n\n   {"status": "FAILURE"}  \n')
        assert result == {"status": "FAILURE"}


class TestExtractJsonWithPacemakerPreamble:
    """The exact staging failure: § telemetry line at byte 0."""

    def test_pacemaker_telemetry_line_before_object(self):
        raw = PACEMAKER_PREAMBLE + '\n\n{"status":"SUCCESS","issues_created":0}'
        result = extract_json_from_llm_response(raw)
        assert result == {"status": "SUCCESS", "issues_created": 0}

    def test_multiple_pacemaker_lines(self):
        raw = (
            PACEMAKER_PREAMBLE
            + "\n"
            + "§ △1.0 ◎other ■surg ◇0.5 ↻2"
            + '\n{"status":"FAILURE","error":"boom"}'
        )
        result = extract_json_from_llm_response(raw)
        assert result == {"status": "FAILURE", "error": "boom"}

    def test_pacemaker_line_interleaved_after_json_is_ignored(self):
        # The first balanced top-level object wins; trailing telemetry is noise.
        raw = (
            PACEMAKER_PREAMBLE
            + '\n{"status":"SUCCESS"}\n'
            + "§ △0.0 ◎surg ■other ◇1.0 ↻9"
        )
        result = extract_json_from_llm_response(raw)
        assert result == {"status": "SUCCESS"}


class TestExtractJsonWithWarningLine:
    """``Warning: no stdin data received ...`` preamble line."""

    def test_leading_warning_line(self):
        raw = (
            "Warning: no stdin data received, continuing\n"
            + '{"status":"SUCCESS","issues_created":2}'
        )
        result = extract_json_from_llm_response(raw)
        assert result == {"status": "SUCCESS", "issues_created": 2}

    def test_pacemaker_and_warning_combined(self):
        raw = (
            PACEMAKER_PREAMBLE
            + "\nWarning: no stdin data received, continuing\n"
            + '{"status":"SUCCESS"}'
        )
        result = extract_json_from_llm_response(raw)
        assert result == {"status": "SUCCESS"}


class TestExtractJsonWithCodeFences:
    """Markdown ```json ... ``` fences around the payload."""

    def test_json_code_fence(self):
        raw = '```json\n{"status":"SUCCESS","issues_created":0}\n```'
        result = extract_json_from_llm_response(raw)
        assert result == {"status": "SUCCESS", "issues_created": 0}

    def test_bare_code_fence(self):
        raw = '```\n{"status":"FAILURE"}\n```'
        result = extract_json_from_llm_response(raw)
        assert result == {"status": "FAILURE"}

    def test_pacemaker_then_code_fence(self):
        raw = PACEMAKER_PREAMBLE + '\n```json\n{"status":"SUCCESS"}\n```'
        result = extract_json_from_llm_response(raw)
        assert result == {"status": "SUCCESS"}


class TestExtractJsonWithProsePreamble:
    """Leading natural-language prose before the JSON."""

    def test_prose_then_json(self):
        raw = (
            "Here is the analysis result you asked for:\n\n"
            + '{"status":"SUCCESS","issues_created":1}'
        )
        result = extract_json_from_llm_response(raw)
        assert result == {"status": "SUCCESS", "issues_created": 1}

    def test_nested_braces_in_payload(self):
        raw = PACEMAKER_PREAMBLE + '\n{"status":"SUCCESS","detail":{"a":1,"b":[2,3]}}'
        result = extract_json_from_llm_response(raw)
        assert result == {
            "status": "SUCCESS",
            "detail": {"a": 1, "b": [2, 3]},
        }

    def test_brace_inside_string_literal_not_treated_as_structure(self):
        # A "}" inside a string value must not prematurely close the object.
        raw = PACEMAKER_PREAMBLE + '\n{"status":"FAILURE","error":"oops } not closed"}'
        result = extract_json_from_llm_response(raw)
        assert result == {"status": "FAILURE", "error": "oops } not closed"}

    def test_escaped_quote_inside_string(self):
        # A backslash-escaped quote must NOT end the string scan early.
        raw = PACEMAKER_PREAMBLE + '\n{"status":"FAILURE","error":"say \\"} hi\\" now"}'
        result = extract_json_from_llm_response(raw)
        assert result == {"status": "FAILURE", "error": 'say "} hi" now'}

    def test_escaped_backslash_inside_string(self):
        # A literal backslash (escaped) followed by a quote closes the string.
        raw = PACEMAKER_PREAMBLE + '\n{"path":"C:\\\\tmp\\\\x","status":"SUCCESS"}'
        result = extract_json_from_llm_response(raw)
        assert result == {"path": "C:\\tmp\\x", "status": "SUCCESS"}


class TestExtractJsonWithStrayFragmentBeforePayload:
    """Issue #1436: a stray bracket/brace fragment embedded inline in prose
    (NOT on its own clean line, so ``_strip_noise_lines`` cannot remove it)
    appears BEFORE the real, valid JSON payload later in the same string.
    The extractor must try candidates in order and return the first one that
    actually parses, rather than locking onto the first balanced span found.
    """

    def test_stray_bracket_fragment_before_real_json_object_is_recovered(self):
        # "[ref]" is a balanced bracket span but not valid JSON; the real
        # payload follows later in the same string, inline (no clean line
        # boundary the old line-based noise stripping could exploit).
        raw = (
            'See [ref] before the real payload: {"status":"SUCCESS","issues_created":3}'
        )
        result = extract_json_from_llm_response(raw)
        assert result == {"status": "SUCCESS", "issues_created": 3}

    def test_stray_brace_fragment_before_real_json_object_is_recovered(self):
        # "{ incomplete" opens a brace that never balances against the rest
        # of the string on its own; the real payload's own braces should be
        # found as a separate, later, successfully-parsing candidate.
        raw = (
            "Note: config { incomplete real payload follows: "
            '{"status":"SUCCESS","issues_created":7}'
        )
        result = extract_json_from_llm_response(raw)
        assert result == {"status": "SUCCESS", "issues_created": 7}


class TestExtractJsonErrors:
    """Genuinely empty / garbage responses must raise a clear error."""

    def test_no_candidate_parses_raises_value_error(self):
        # Multiple bracket/brace candidates exist, but NONE of them are
        # valid JSON -- must raise loudly, never silently return a wrong or
        # partial result (anti-silent-failure).
        raw = "[not json] and {also not json}"
        with pytest.raises(ValueError):
            extract_json_from_llm_response(raw)

    def test_empty_string_raises(self):
        with pytest.raises(ValueError) as exc:
            extract_json_from_llm_response("")
        assert "no json" in str(exc.value).lower() or "empty" in str(exc.value).lower()

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError):
            extract_json_from_llm_response("   \n\n  \t ")

    def test_pacemaker_only_no_json_raises(self):
        # All telemetry, no payload -> must NOT be a false success.
        with pytest.raises(ValueError):
            extract_json_from_llm_response(PACEMAKER_PREAMBLE)

    def test_prose_only_no_json_raises(self):
        with pytest.raises(ValueError):
            extract_json_from_llm_response("I could not complete the analysis.")

    def test_unbalanced_braces_raise(self):
        with pytest.raises(ValueError):
            extract_json_from_llm_response(
                PACEMAKER_PREAMBLE + '\n{"status": "SUCCESS"'
            )

    def test_balanced_but_invalid_json_payload_raises(self):
        # Braces balance, but the content is not valid JSON (unquoted key) ->
        # must surface a clear "not valid JSON" error, not crash.
        raw = PACEMAKER_PREAMBLE + "\n{status: SUCCESS}"
        with pytest.raises(ValueError) as exc:
            extract_json_from_llm_response(raw)
        assert "not valid json" in str(exc.value).lower()

    def test_none_raises(self):
        with pytest.raises((ValueError, TypeError)):
            extract_json_from_llm_response(None)  # type: ignore[arg-type]
