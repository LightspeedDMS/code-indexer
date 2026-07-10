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

    def test_non_ascii_literal_uses_only_ascii_subruns(self):
        # "café" (accented e): the index stores printable-ASCII trigrams only, so
        # a trigram spanning the non-ASCII char ("afé") would have zero document
        # frequency and wrongly prune every indexed file. Only the ASCII sub-run
        # "caf" is a valid required trigram.
        assert extract_required_trigrams("café") == {"caf"}

    def test_non_ascii_splits_run_into_subruns(self):
        # "naïve string" -> ASCII sub-runs "na" (too short) and "ve string".
        assert extract_required_trigrams("naïve string") == trigrams("ve string")

    def test_non_ascii_leaving_no_long_ascii_run_returns_none(self):
        # "aébc" -> ASCII sub-runs "a","bc": none >= 3 -> no safe constraint.
        assert extract_required_trigrams("aébc") is None

    def test_required_trigrams_are_always_index_storable_ascii(self):
        # Whatever the pattern, every required trigram must be printable ASCII so
        # it can actually exist in the (ASCII-only) index.
        for pat in ["naïve string", "résumévalue", "café table"]:
            req = extract_required_trigrams(pat)
            if req is None:
                continue
            for t in req:
                assert all(0x20 <= ord(c) <= 0x7E for c in t), (pat, t)


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
    # non-ASCII literal: only the ASCII sub-run constrains, and it must remain a
    # necessary condition for every match.
    ("café table", ["a café table here", "café table"]),
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
