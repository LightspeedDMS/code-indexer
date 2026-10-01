"""
TDD driver for #1876 rework, matcher-foundation step (items 1, 4, 5, 6).

Covers, at the shared `PathPatternMatcher` layer only (consumer wiring in
regex_search.py / search_engine.py / xray_graph.py lands in a later step):

- Item 1: brace-glob expansion (`*.{ts,md}`), including nested/multiple groups.
- Item 4: structured `InvalidPatternError` for non-string items, unbalanced
  braces, and gitignore negation/comment syntax (`!x`, `#x`) that can never
  produce a match as a standalone include/exclude pattern -- replacing the
  old silent per-pattern skip in `matches_any_pattern`.
- Item 5/6: a compiled selector (`PathPatternMatcher.create_selector`) that
  builds exactly one PathSpec per pattern list for the whole query, instead
  of re-normalizing/rebuilding per path -- this is the shared helper item 6's
  four duplicated call sites will be collapsed onto in the next step.
"""

import pathspec
import pytest

from code_indexer.services.path_pattern_matcher import (
    InvalidPatternError,
    PathPatternMatcher,
)


@pytest.fixture()
def matcher() -> PathPatternMatcher:
    return PathPatternMatcher()


# --- Item 1: brace expansion ---------------------------------------------


@pytest.mark.parametrize(
    "path,pattern,expected",
    [
        ("foo.ts", "*.{ts,md}", True),
        ("foo.md", "*.{ts,md}", True),
        ("foo.js", "*.{ts,md}", False),
        ("README.md", "*.{ts,md}", True),
    ],
)
def test_single_brace_group(matcher, path, pattern, expected):
    assert matcher.matches_pattern(path, pattern) == expected


@pytest.mark.parametrize(
    "path,pattern,expected",
    [
        ("src/a/x.ts", "src/{a,b}/*.{ts,md}", True),
        ("src/b/y.md", "src/{a,b}/*.{ts,md}", True),
        ("src/c/x.ts", "src/{a,b}/*.{ts,md}", False),
        ("src/a/x.js", "src/{a,b}/*.{ts,md}", False),
    ],
)
def test_nested_multiple_brace_groups(matcher, path, pattern, expected):
    assert matcher.matches_pattern(path, pattern) == expected


def test_brace_expansion_via_matches_any_pattern(matcher):
    assert matcher.matches_any_pattern("dist/app.ts", ["*.{ts,md}"]) is True
    assert matcher.matches_any_pattern("dist/app.json", ["*.{ts,md}"]) is False


def test_unbalanced_brace_raises_invalid_pattern_error(matcher):
    with pytest.raises(InvalidPatternError):
        matcher.matches_pattern("foo.ts", "*.{ts,md")


def test_single_option_brace_group_is_literal_not_expanded(matcher):
    # `{md}` has no top-level comma, so it is not a real alternation
    # (mirrors bash/ripgrep brace semantics) -- it stays literal and
    # must NOT silently become equivalent to plain `*.md`.
    assert matcher.matches_pattern("foo.md", "*.{md}") is False
    assert matcher.matches_pattern("foo.{md}", "*.{md}") is True


# --- Item 4: structured validation, no silent skip ------------------------


def test_matches_any_pattern_no_longer_silently_skips_invalid(matcher):
    # Pre-fix behaviour: matches_any_pattern caught (TypeError, ValueError)
    # per-pattern and silently continued, so an invalid exclude pattern
    # failed OPEN (widened the search) with zero signal. Post-fix: it
    # must raise so the caller can surface a structured error instead.
    with pytest.raises(InvalidPatternError):
        matcher.matches_any_pattern("x.py", ["!*.md"])


@pytest.mark.parametrize("bad_pattern", ["!*.md", "#x", "!", "#"])
def test_negation_and_comment_syntax_rejected(matcher, bad_pattern):
    with pytest.raises(InvalidPatternError):
        matcher.matches_pattern("x.py", bad_pattern)


def test_compile_patterns_rejects_non_string_item(matcher):
    with pytest.raises(InvalidPatternError):
        matcher.compile_patterns([None])


def test_compile_patterns_rejects_non_string_item_int(matcher):
    with pytest.raises(InvalidPatternError):
        matcher.compile_patterns([123])


def test_compile_patterns_rejects_unbalanced_brace(matcher):
    with pytest.raises(InvalidPatternError):
        matcher.compile_patterns(["*.{ts,md"])


def test_compile_patterns_rejects_negation(matcher):
    with pytest.raises(InvalidPatternError):
        matcher.compile_patterns(["!*.md"])


def test_compile_patterns_rejects_comment(matcher):
    with pytest.raises(InvalidPatternError):
        matcher.compile_patterns(["#x"])


def test_invalid_pattern_error_is_a_value_error():
    # Backward compat: existing `except (TypeError, ValueError)` callers
    # (soon to be removed at consumer sites) still catch this.
    assert issubclass(InvalidPatternError, ValueError)


def test_compile_patterns_none_returns_none(matcher):
    assert matcher.compile_patterns(None) is None


def test_compile_patterns_empty_list_returns_none(matcher):
    assert matcher.compile_patterns([]) is None


# --- Item 5/6 (partial): compile_patterns wired into matches_any_pattern -
#
# The per-query outside-the-loop selector (`create_selector`/`PathSelector`,
# combining an include + an exclude CompiledPatternSet) lands in the next
# step together with the actual consumer-site wiring in regex_search.py /
# search_engine.py / xray_graph.py -- adding it here first, unconsumed by
# any production call site, is exactly the orphan-capability trap this
# project's rulebook forbids. What lands THIS step is real today:
# `compile_patterns`/`CompiledPatternSet` are exercised on every call
# through `matches_any_pattern`, which the 4 existing duplicated call sites
# already invoke on every real regex_search/xray_search request.


def test_compile_patterns_basic(matcher):
    compiled = matcher.compile_patterns(["*.py", "*.{ts,md}"])
    assert compiled.matches("src/foo.py") is True
    assert compiled.matches("src/foo.ts") is True
    assert compiled.matches("src/foo.md") is True
    assert compiled.matches("src/foo.json") is False


def test_matches_any_pattern_builds_pathspec_once_per_call(matcher, monkeypatch):
    # Old implementation looped `matches_pattern` per pattern, doing a
    # separate cache lookup/spec build per pattern per path. New
    # implementation must compile the WHOLE pattern list into one PathSpec
    # per `matches_any_pattern` call (item 5's per-query amortization is
    # completed next step when the call site itself hoists this outside its
    # own per-path loop).
    call_count = {"n": 0}
    original_from_lines = pathspec.PathSpec.from_lines

    def counting_from_lines(*args, **kwargs):
        call_count["n"] += 1
        return original_from_lines(*args, **kwargs)

    monkeypatch.setattr(
        pathspec.PathSpec, "from_lines", staticmethod(counting_from_lines)
    )

    patterns = ["*.py", "*.{ts,md}", "*.json"]
    assert matcher.matches_any_pattern("src/foo.py", patterns) is True
    assert call_count["n"] == 1
