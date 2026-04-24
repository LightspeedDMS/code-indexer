"""
RED tests for Story #887 AC1 — strip_backticks() pure function.

Validates: wrapper stripping, interior preservation, edge cases, invariant.
"""

from tests.unit.server.services.test_dep_map_887_fixtures import import_hygiene_symbol


class TestStripBackticks:
    """Unit tests for strip_backticks() in dep_map_parser_hygiene."""

    def test_strips_leading_and_trailing(self) -> None:
        fn = import_hygiene_symbol("strip_backticks")
        assert fn("`foo`") == "foo"

    def test_interior_backtick_preserved(self) -> None:
        """Interior backticks are NOT stripped — only outer wrapper."""
        fn = import_hygiene_symbol("strip_backticks")
        assert fn("`foo`bar`") == "foo`bar"

    def test_no_backticks_unchanged(self) -> None:
        fn = import_hygiene_symbol("strip_backticks")
        assert fn("foo") == "foo"

    def test_only_leading(self) -> None:
        fn = import_hygiene_symbol("strip_backticks")
        assert fn("`foo") == "foo"

    def test_only_trailing(self) -> None:
        fn = import_hygiene_symbol("strip_backticks")
        assert fn("foo`") == "foo"

    def test_empty_string(self) -> None:
        fn = import_hygiene_symbol("strip_backticks")
        assert fn("") == ""

    def test_lone_backtick_becomes_empty(self) -> None:
        fn = import_hygiene_symbol("strip_backticks")
        assert fn("`") == ""

    def test_double_wrapper_strips_all_layers(self) -> None:
        """All wrapper backtick layers stripped per AC1 invariant: ``foo`` -> foo."""
        fn = import_hygiene_symbol("strip_backticks")
        assert fn("``foo``") == "foo"

    def test_invariant_result_never_starts_or_ends_with_backtick(self) -> None:
        """Post-condition: result never starts or ends with a backtick."""
        fn = import_hygiene_symbol("strip_backticks")
        for value in ["`foo`", "`bar", "baz`", "`", "``", "`a`b`c`", "normal", ""]:
            result = fn(value)
            assert not result.startswith("`"), (
                f"strip_backticks({value!r}) = {result!r} starts with backtick"
            )
            assert not result.endswith("`"), (
                f"strip_backticks({value!r}) = {result!r} ends with backtick"
            )
