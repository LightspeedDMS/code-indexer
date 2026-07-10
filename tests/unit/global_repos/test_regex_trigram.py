"""Tests for regex -> required-trigram analysis (index-assisted regex).

The overriding requirement is CORRECTNESS: the derived trigram set must be a
*necessary* condition for a match, so a candidate filter built from it can never
exclude a real match. The property test at the bottom enforces exactly that.
"""

import re

import pytest

from code_indexer.global_repos.regex_trigram import (
    extract_required_trigrams,
    trigrams,
)


class TestTrigrams:
    def test_basic(self):
        assert trigrams("abcd") == {"abc", "bcd"}

    def test_too_short(self):
        assert trigrams("ab") == set()
        assert trigrams("") == set()


class TestExtractRequiredTrigrams:
    def test_simple_literal(self):
        assert extract_required_trigrams("Authenticator") == trigrams("authenticator")

    def test_two_required_literals_are_anded(self):
        got = extract_required_trigrams(r"class\s+\w*Authenticator")
        assert got == trigrams("class") | trigrams("authenticator")

    def test_star_atom_is_not_required(self):
        # \w* contributes nothing; only the fixed literals do
        assert extract_required_trigrams(r"\w*Authenticator") == trigrams(
            "authenticator"
        )

    def test_optional_last_char_trimmed(self):
        # colou?r -> "colo" is guaranteed ("u" optional), "r" alone too short
        assert extract_required_trigrams("colou?r") == trigrams("colo")

    def test_plus_quantified_char_not_in_run(self):
        # ab+c -> a, (b+), c : no fixed run of length >= 3 -> None
        assert extract_required_trigrams("ab+c") is None

    def test_counted_quantifier_breaks_run(self):
        assert extract_required_trigrams("abc{2}defgh") == trigrams("defgh")

    def test_char_class_breaks_run(self):
        # "foo" and "barbaz" are both required (the class is between them)
        assert extract_required_trigrams(r"foo[0-9]+barbaz") == (
            trigrams("foo") | trigrams("barbaz")
        )

    def test_escaped_metachar_is_literal(self):
        # \. is a literal dot -> "abc.def" contiguous
        assert extract_required_trigrams(r"abc\.def") == trigrams("abc.def")

    def test_escaped_class_breaks_run(self):
        # \d is a class, not a literal
        assert extract_required_trigrams(r"ab\dcd") is None  # no run >= 3

    def test_dot_and_anchors_break_runs(self):
        assert extract_required_trigrams(r"^foobar.baz$") == (
            trigrams("foobar") | trigrams("baz")
        )

    def test_alternation_bails(self):
        assert extract_required_trigrams("foobar|bazqux") is None

    def test_group_bails(self):
        assert extract_required_trigrams("(foobar)+baz") is None

    def test_short_pattern_none(self):
        assert extract_required_trigrams("ab") is None
        assert extract_required_trigrams(r"a.b") is None
        assert extract_required_trigrams("") is None

    def test_case_insensitive_lowercased(self):
        # trigrams always lowercased regardless of flag
        assert extract_required_trigrams("FOOBAR", case_insensitive=True) == trigrams(
            "foobar"
        )
        assert extract_required_trigrams("FOOBAR") == trigrams("foobar")


# ---------------------------------------------------------------------------
# Correctness property: for a pattern that yields trigrams, EVERY string that
# matches the pattern must contain ALL of those trigrams (lowercased). If this
# ever fails, the pre-filter could drop a real match.
# ---------------------------------------------------------------------------

_PROPERTY_CASES = [
    # (pattern, [strings that match it])
    (r"class\s+\w*Authenticator", ["class LSAuthenticator", "class  XyzAuthenticator"]),
    ("Authenticator", ["myAuthenticator", "AuthenticatorImpl"]),
    ("colou?r", ["color", "colour", "the color red", "a colour here"]),
    (r"foo[0-9]+barbaz", ["foo1barbaz", "foo12345barbaz here"]),
    (r"abc\.def", ["xabc.defy", "abc.def"]),
    ("abc{2}defgh", ["abccdefgh", "zabccdefghq"]),
    (r"^foobar.baz$", ["foobarxbaz", "foobar baz"]),
    # adversarial optionality: a naive "longest literal run" would false-negate
    ("a?bcdef", ["bcdef", "abcdef", "xxabcdefyy"]),
    ("abcd?efg", ["abcefg", "abcdefg"]),
    (r"\d{3}xyzzy", ["123xyzzy", "  999xyzzy!"]),
    (r"get\w+Value", ["getFooValue", "getValue"[:0] + "getBarValue"]),
]


@pytest.mark.parametrize("pattern,samples", _PROPERTY_CASES)
def test_required_trigrams_are_necessary(pattern, samples):
    req = extract_required_trigrams(pattern)
    if req is None:
        pytest.skip("no trigram constraint derived for this pattern")
    for s in samples:
        assert re.search(pattern, s), f"bad sample: {s!r} does not match {pattern!r}"
        have = trigrams(s.lower())
        missing = req - have
        assert not missing, (
            f"NECESSARY-CONDITION VIOLATION: matching string {s!r} is missing "
            f"required trigrams {missing} for pattern {pattern!r}"
        )
