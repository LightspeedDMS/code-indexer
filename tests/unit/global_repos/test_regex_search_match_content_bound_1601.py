"""Issue #1601 remediation round 4, Priority 2 (REQUIRED -- Codex Critical
finding, real risk to the original bug's purpose).

Capping the READ doesn't cap the RESULT. A single multiline regex match's
``m.group(0)`` can span nearly the entire read buffer (up to the ~64 MiB
``_MAX_READ_BYTES`` ceiling) and gets retained verbatim as ``line_content``
on the resulting ``RegexMatch`` -- the input read was bounded, but nothing
bounded the memory used to hold the RESULT, which is the original #1601
bug's core concern (server OOM risk at ~900-repo fleet scale).

This also applies to the ripgrep/grep single-line paths: a "line" as
ripgrep/grep report it is not itself bounded by anything shorter than the
overall read ceiling -- a file with no newlines (e.g. a minified asset)
can report one "line" whose text is the entire (possibly near-ceiling-
sized) buffer. Both call sites are covered here.
"""

from __future__ import annotations

import json
import re
from unittest.mock import patch

from code_indexer.global_repos.regex_search import (
    RegexSearchService,
    _bounded_match_content,
    _MAX_MATCH_CONTENT_BYTES,
)

_TEST_MAX_RESULTS = 100
_TRUNCATION_MARKER = "[content truncated]"
_TRUNCATION_SUFFIX_BYTES = len(f"\n{_TRUNCATION_MARKER}")


class TestBoundedMatchContentHelper:
    """Pure-function unit tests for the new truncation helper."""

    def test_short_content_passes_through_unchanged(self):
        value = "a normal, short line of matched content"
        assert _bounded_match_content(value) == value

    def test_content_exactly_at_ceiling_passes_through_unchanged(self):
        value = "x" * _MAX_MATCH_CONTENT_BYTES
        assert _bounded_match_content(value) == value

    def test_content_one_byte_over_ceiling_is_truncated_with_marker(self):
        value = "x" * (_MAX_MATCH_CONTENT_BYTES + 1)
        result = _bounded_match_content(value)

        assert len(result) < len(value)
        assert result.endswith(_TRUNCATION_MARKER)
        assert (
            len(result.encode("utf-8"))
            <= _MAX_MATCH_CONTENT_BYTES + _TRUNCATION_SUFFIX_BYTES
        )

    def test_truncation_is_utf8_safe_across_multibyte_boundary(self):
        """A multi-byte UTF-8 character straddling the truncation cut point
        must never be corrupted into invalid output -- errors='replace'
        degrades gracefully instead of raising or emitting a mangled
        half-character."""
        # Enough 3-byte '€' characters to comfortably straddle the ceiling.
        multibyte_char = "€"
        value = multibyte_char * (_MAX_MATCH_CONTENT_BYTES // 2)
        result = _bounded_match_content(value)

        # Must not raise (decoding happened without error) and must be a
        # valid str.
        assert isinstance(result, str)
        assert result.endswith(_TRUNCATION_MARKER)


class TestMultilineMatchContentBound:
    """Priority 2 (mandatory case): _scan_multiline_content must not
    retain an unbounded m.group(0) as line_content."""

    def test_huge_multiline_match_is_truncated_not_retained_verbatim(self):
        """Construct one multiline match spanning most of a REDUCED test
        content ceiling (the real ``_MAX_MATCH_CONTENT_BYTES`` module
        constant, patched down) -- confirms it would otherwise produce a
        huge line_content, then confirms the fix truncates it with a
        clear marker."""
        import code_indexer.global_repos.regex_search as regex_search_module

        reduced_ceiling_bytes = 4096
        # A pattern that matches from "START" to "END", with a blob of
        # filler comfortably larger than the (patched-down) content
        # ceiling in between, so the single match's raw length genuinely
        # exceeds it.
        filler = "y" * (reduced_ceiling_bytes * 2)
        content = f"START{filler}END"
        compiled = re.compile(r"START[\s\S]*END")

        with patch.object(
            regex_search_module, "_MAX_MATCH_CONTENT_BYTES", reduced_ceiling_bytes
        ):
            matches: list = []
            stop, count = RegexSearchService._scan_multiline_content(
                content,
                compiled,
                "fake/path.py",
                matches,
                max_results=_TEST_MAX_RESULTS,
            )

        assert stop is False
        assert count == 1
        assert len(matches) == 1
        # The full raw match is genuinely larger than the reduced ceiling
        # -- otherwise this test would prove nothing.
        assert len(content.encode("utf-8")) > reduced_ceiling_bytes
        # The actual assertion that matters: line_content must be bounded
        # to (approximately) the reduced ceiling, not the full raw match,
        # and carry the truncation marker -- proving the fix, not merely
        # that a match was found.
        line_content = matches[0].line_content
        assert (
            len(line_content.encode("utf-8"))
            <= reduced_ceiling_bytes + _TRUNCATION_SUFFIX_BYTES
        )
        assert line_content.endswith(_TRUNCATION_MARKER)
        assert len(line_content) < len(content)

    def test_normal_sized_multiline_match_is_returned_verbatim(self):
        """A match comfortably under the content ceiling is unaffected."""
        content = "class Foo:\n    def login(self):\n        pass\n"
        compiled = re.compile(r"class[\s\S]*login")

        matches: list = []
        stop, count = RegexSearchService._scan_multiline_content(
            content, compiled, "fake/path.py", matches, max_results=_TEST_MAX_RESULTS
        )

        assert stop is False
        assert count == 1
        assert matches[0].line_content == "class Foo:\n    def login"


class TestSingleLineMatchContentBound:
    """Priority 2 (investigated gap): a single ripgrep/grep-reported
    "line" is not itself bounded by anything shorter than the overall
    read ceiling (e.g. a file with no newlines) -- verify the same bound
    is applied at the point RegexMatch.line_content is constructed for
    both engines' match events, and that normal-sized events going
    through the SAME real parser are left untouched."""

    def _build_ripgrep_service(self, tmp_path) -> RegexSearchService:
        with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/rg"
            return RegexSearchService(tmp_path)

    def _build_grep_service(self, tmp_path) -> RegexSearchService:
        with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
            mock_which.side_effect = lambda cmd: (
                "/usr/bin/grep" if cmd == "grep" else None
            )
            return RegexSearchService(tmp_path)

    def test_ripgrep_match_event_line_content_is_bounded(self, tmp_path):
        service = self._build_ripgrep_service(tmp_path)

        huge_line = "z" * (_MAX_MATCH_CONTENT_BYTES + 500)
        event = {
            "type": "match",
            "data": {
                "path": {"text": "file1.py"},
                "line_number": 1,
                "lines": {"text": huge_line},
                "submatches": [{"start": 0, "end": 1}],
            },
        }
        matches, total = service._parse_ripgrep_json_output(
            json.dumps(event), max_results=_TEST_MAX_RESULTS, context_lines=0
        )

        assert total == 1
        assert len(matches) == 1
        line_content = matches[0].line_content
        assert (
            len(line_content.encode("utf-8"))
            <= _MAX_MATCH_CONTENT_BYTES + _TRUNCATION_SUFFIX_BYTES
        )
        assert line_content.endswith(_TRUNCATION_MARKER)

    def test_ripgrep_normal_match_event_line_content_unaffected(self, tmp_path):
        """Sanity: a normal-sized ripgrep match event, parsed through the
        SAME real parser, is returned byte-for-byte unchanged."""
        service = self._build_ripgrep_service(tmp_path)

        event = {
            "type": "match",
            "data": {
                "path": {"text": "file1.py"},
                "line_number": 1,
                "lines": {"text": "def func_1():\n"},
                "submatches": [{"start": 0, "end": 3}],
            },
        }
        matches, total = service._parse_ripgrep_json_output(
            json.dumps(event), max_results=_TEST_MAX_RESULTS, context_lines=0
        )

        assert total == 1
        assert len(matches) == 1
        assert matches[0].line_content == "def func_1():"

    def test_grep_match_line_content_is_bounded(self, tmp_path):
        service = self._build_grep_service(tmp_path)

        huge_line = "z" * (_MAX_MATCH_CONTENT_BYTES + 500)
        output = f"file1.py:1:{huge_line}"

        matches, total = service._parse_grep_output(
            output, max_results=_TEST_MAX_RESULTS, context_lines=0
        )

        assert total == 1
        assert len(matches) == 1
        line_content = matches[0].line_content
        assert (
            len(line_content.encode("utf-8"))
            <= _MAX_MATCH_CONTENT_BYTES + _TRUNCATION_SUFFIX_BYTES
        )
        assert line_content.endswith(_TRUNCATION_MARKER)

    def test_grep_normal_match_line_content_unaffected(self, tmp_path):
        """Sanity: a normal-sized grep match line, parsed through the SAME
        real parser, is returned unchanged."""
        service = self._build_grep_service(tmp_path)

        output = "file1.py:1:def func_1():"

        matches, total = service._parse_grep_output(
            output, max_results=_TEST_MAX_RESULTS, context_lines=0
        )

        assert total == 1
        assert len(matches) == 1
        assert matches[0].line_content == "def func_1():"
