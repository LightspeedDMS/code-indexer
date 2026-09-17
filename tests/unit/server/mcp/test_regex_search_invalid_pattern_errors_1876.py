"""Bug #1876 item 4: regex_search's front door must turn invalid
include_patterns/exclude_patterns into a structured error, never silently
skip or narrow/widen the search.

`_validate_regex_args` (the sole validation gate `handle_regex_search`
calls) previously only checked that the parameter was a list -- a
non-string item (`[None]`) or a malformed pattern (unbalanced brace,
gitignore negation/comment syntax) passed straight through to the search
engine, where an exclude failing open would silently widen the search.
"""

from __future__ import annotations

from code_indexer.server.mcp.handlers.search import _validate_regex_args


def _args(**overrides):
    base = {"pattern": "needle", "repository_alias": "some-repo"}
    base.update(overrides)
    return base


def test_non_string_include_pattern_item_is_a_structured_error():
    repository_alias, err = _validate_regex_args(_args(include_patterns=[None]))
    assert repository_alias is None
    assert err is not None
    assert err["content"][0]["text"]


def test_non_string_exclude_pattern_item_is_a_structured_error():
    repository_alias, err = _validate_regex_args(_args(exclude_patterns=[123]))
    assert repository_alias is None
    assert err is not None


def test_unbalanced_brace_include_pattern_is_a_structured_error():
    repository_alias, err = _validate_regex_args(_args(include_patterns=["*.{ts,md"]))
    assert repository_alias is None
    assert err is not None


def test_negation_syntax_exclude_pattern_is_a_structured_error():
    repository_alias, err = _validate_regex_args(_args(exclude_patterns=["!*.md"]))
    assert repository_alias is None
    assert err is not None


def test_comment_syntax_include_pattern_is_a_structured_error():
    repository_alias, err = _validate_regex_args(_args(include_patterns=["#x"]))
    assert repository_alias is None
    assert err is not None


def test_valid_patterns_still_pass_validation():
    repository_alias, err = _validate_regex_args(
        _args(include_patterns=["*.{ts,md}"], exclude_patterns=["docs"])
    )
    assert err is None
    assert repository_alias == "some-repo"
