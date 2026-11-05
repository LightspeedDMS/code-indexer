"""Unit tests for the escape-aware regex-literal lowercaser.

The Tantivy FTS ``content`` field is lowercased at index time, so FTS regex is
made case-insensitive by lowercasing the pattern's *literals* before it reaches
Tantivy's ``regex_query`` -- WITHOUT corrupting regex escapes (a blind
``.lower()`` would flip ``\\W`` -> ``\\w``, inverting the pattern).

These are fast, pure-function tests: no Tantivy index required.
"""

from code_indexer.services.tantivy_index_manager import lowercase_regex_literals


class TestLowercaseRegexLiterals:
    """Escape-aware lowercasing of regex literal characters."""

    def test_plain_word_is_lowercased(self):
        # `Config` can never match the lowercased index; lowercase it.
        assert lowercase_regex_literals("Config") == "config"

    def test_uppercase_character_class_range_is_lowercased(self):
        # [A-Z][a-z]+ -> [a-z][a-z]+ (range endpoints are literals).
        assert lowercase_regex_literals("[A-Z][a-z]+") == "[a-z][a-z]+"

    def test_alternation_of_uppercase_tokens(self):
        assert lowercase_regex_literals("TODO|FIXME") == "todo|fixme"

    def test_already_lowercase_is_unchanged(self):
        assert lowercase_regex_literals("conf.*") == "conf.*"
        assert lowercase_regex_literals("[a-z]+tion") == "[a-z]+tion"
        assert lowercase_regex_literals("handl.*") == "handl.*"

    def test_mixed_literals_and_metacharacters(self):
        # Metacharacters ( ) | + * . { } [ ] ^ $ ? and digits are untouched.
        assert lowercase_regex_literals("(Foo|Bar){2,3}.*Baz") == "(foo|bar){2,3}.*baz"

    # --- Escape sequences must be preserved VERBATIM (never .lower()'d) ---

    def test_word_and_digit_class_escapes_preserved(self):
        # The critical corruption case: \W must NOT become \w, \D must NOT
        # become \d, etc.
        assert lowercase_regex_literals(r"\w") == r"\w"
        assert lowercase_regex_literals(r"\d") == r"\d"
        assert lowercase_regex_literals(r"\s") == r"\s"
        assert lowercase_regex_literals(r"\W") == r"\W"
        assert lowercase_regex_literals(r"\D") == r"\D"
        assert lowercase_regex_literals(r"\S") == r"\S"

    def test_word_boundary_escapes_preserved(self):
        assert lowercase_regex_literals(r"\b") == r"\b"
        assert lowercase_regex_literals(r"\B") == r"\B"

    def test_word_boundary_around_literal_lowercases_only_literal(self):
        # \bFoo\B -> literals lowercased, boundaries intact.
        assert lowercase_regex_literals(r"\bFoo\B") == r"\bfoo\B"
        # The mandatory case: \bfoo\b stays exactly as-is.
        assert lowercase_regex_literals(r"\bfoo\b") == r"\bfoo\b"

    def test_unicode_property_escape_preserved(self):
        # \p{Lu} (uppercase-letter property) must survive intact -- lowercasing
        # it to \p{lu} would change/break the property name.
        assert lowercase_regex_literals(r"\p{Lu}") == r"\p{Lu}"
        assert lowercase_regex_literals(r"\P{Nd}") == r"\P{Nd}"

    def test_single_letter_unicode_property_preserved(self):
        # \pL must not become \pl.
        assert lowercase_regex_literals(r"\pL") == r"\pL"

    def test_property_escape_surrounded_by_literals(self):
        assert lowercase_regex_literals(r"Name\p{Lu}End") == r"name\p{Lu}end"

    def test_hex_escape_preserved_verbatim(self):
        # \xHH -- hex digits (incl. uppercase A-F) preserved verbatim; the
        # following literal is still lowercased.
        assert lowercase_regex_literals(r"\xAB") == r"\xAB"
        assert lowercase_regex_literals(r"\x41B") == r"\x41b"  # \x41 + literal B

    def test_braced_hex_escape_preserved(self):
        assert lowercase_regex_literals(r"\x{1F600}") == r"\x{1F600}"

    def test_unicode_codepoint_escape_preserved(self):
        # \uXXXX -- 4 fixed hex digits preserved verbatim; a trailing literal
        # after the fixed-width escape is still lowercased. Build the escape
        # via an explicit backslash so the literal contains \u..., not the
        # decoded codepoint.
        bs = "\\"
        assert lowercase_regex_literals(bs + "uABCD") == bs + "uABCD"
        assert lowercase_regex_literals(bs + "u0041Z") == bs + "u0041z"
        assert lowercase_regex_literals(bs + "u{1F600}") == bs + "u{1F600}"

    def test_escaped_metacharacter_preserved(self):
        # \. \( \[ etc. -- the escaped char stays literal (and is not a letter
        # to lowercase anyway); ensure surrounding letters lowercase.
        assert lowercase_regex_literals(r"A\.B") == r"a\.b"

    def test_dangling_trailing_backslash_is_preserved(self):
        assert lowercase_regex_literals("Foo\\") == "foo\\"

    def test_empty_pattern(self):
        assert lowercase_regex_literals("") == ""

    def test_idempotent_on_already_lowercased_output(self):
        pattern = r"[A-Z]\w+\p{Lu}TODO"
        once = lowercase_regex_literals(pattern)
        twice = lowercase_regex_literals(once)
        assert once == twice
