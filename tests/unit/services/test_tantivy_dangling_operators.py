"""Dangling '-' / '+' operators in FTS queries.

Tantivy's parse_query() rejects a '+'/'-' with no term after it ("Syntax
Error: -") and a query made only of excluded terms ("Only excluding terms
given"). The sanitizer always drops operator-only tokens (and any boolean
operator that removal orphans).

Queries WITH valid boolean operators keep their '-'/'+' prefixes and Tantivy
meaning ('foo AND -bar' excludes bar). Only queries without boolean operators,
which are split into per-term queries where '-bar' alone fails, have leading
operators stripped so the literal word is searched ('-OR' becomes the word
'or', never a live operator).
"""

import logging

import pytest

from code_indexer.services.tantivy_index_manager import (
    TantivyIndexManager,
    sanitize_fts_query,
)


class TestSanitizeDanglingOperators:
    """sanitize_fts_query() never lets a dangling +/- reach parse_query()."""

    @pytest.mark.parametrize(
        "query, expected",
        [
            ("-", ""),
            ("+", ""),
            ("-+-", ""),
            ("foo -", "foo"),
            ("foo - bar", "foo bar"),
            ("- foo", "foo"),
            ("foo + bar", "foo bar"),
            ("--verbose", "verbose"),
            ("-bar", "bar"),
            ("foo -bar", "foo bar"),
            ("+foo", "foo"),
            ("+-foo", "foo"),
            # A stripped '-OR' is the literal word, never a live operator.
            ("foo -OR bar", "foo or bar"),
        ],
    )
    def test_dangling_and_leading_operators_removed(self, query, expected):
        assert sanitize_fts_query(query) == expected

    @pytest.mark.parametrize("query", ["foo-bar", "a--b", "foo-", "foo+"])
    def test_inner_and_trailing_operators_preserved(self, query):
        assert sanitize_fts_query(query) == query

    @pytest.mark.parametrize("query", ["foo AND -bar", "foo OR +bar"])
    def test_operator_prefix_kept_when_query_has_valid_boolean_ops(self, query):
        # A valid boolean query goes whole to parse_query(), which handles
        # '-term'/'+term' itself, so the prefix must survive.
        assert sanitize_fts_query(query) == query

    @pytest.mark.parametrize(
        "query, expected",
        [
            ("foo OR -", "foo"),
            ("- AND foo", "foo"),
            ("foo OR - OR bar", "foo OR bar"),
        ],
    )
    def test_boolean_operator_orphaned_by_dropped_token_is_removed(
        self, query, expected
    ):
        # Not lowercased into the literal word 'or'/'and': that would make the
        # word a required term and the query would match nothing.
        assert sanitize_fts_query(query) == expected


_DOCS = {
    "src/only_foo.py": "foo alpha",
    "src/only_bar.py": "bar beta",
    "src/foo_and_bar.py": "foo bar gamma",
    "src/cli.py": "verbose flag",
    # One-character token: an empty fuzzy term (distance >= 1) would match it,
    # so an operator-only query must short-circuit before any query is built.
    "src/short.py": "x = 1",
    # The first word of the sanitized query ('HashMap', 'ns') appears on line
    # 1; the raw query ('HashMap::new', 'ns:Cls') only on line 2.
    "src/map.rs": "use HashMap;\nlet m = HashMap::new();",
    "src/ns.py": "ns and Cls\nns:Cls target",
}

_LOGGER = "code_indexer.services.tantivy_index_manager"


@pytest.fixture
def indexed_manager(tmp_path):
    """A real Tantivy index holding _DOCS."""
    manager = TantivyIndexManager(tmp_path / "tantivy_index")
    manager.initialize_index(create_new=True)
    for path, text in _DOCS.items():
        manager.add_document(
            {
                "path": path,
                "content": text,
                "content_raw": text,
                "identifiers": [],
                "line_start": 1,
                "line_end": 1,
                "language": "python",
            }
        )
    manager.commit()
    return manager


def _search_paths(manager, query, **kwargs):
    results = manager.search(query_text=query, limit=10, **kwargs)
    return {r["path"] for r in results}


def _warnings(caplog):
    return [
        r for r in caplog.records if r.levelno >= logging.WARNING and r.name == _LOGGER
    ]


class TestSearchWithDanglingOperators:
    """search() against a real index: dangling operators never wipe results."""

    def test_trailing_dash_searches_remaining_term(self, indexed_manager, caplog):
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            paths = _search_paths(indexed_manager, "foo -")
        assert paths == {"src/only_foo.py", "src/foo_and_bar.py"}
        assert _warnings(caplog) == []

    def test_dash_between_terms_keeps_multi_term_semantics(self, indexed_manager):
        assert _search_paths(indexed_manager, "foo - bar") == {"src/foo_and_bar.py"}

    def test_leading_dash_term_is_literal_not_exclusion(self, indexed_manager):
        # 'foo -bar' is 'foo bar' (AND), not 'foo excluding bar'.
        assert _search_paths(indexed_manager, "foo -bar") == {"src/foo_and_bar.py"}

    @pytest.mark.parametrize(
        "query, expected",
        [
            ("foo AND -bar", {"src/only_foo.py"}),
            ("foo OR -bar", {"src/only_foo.py", "src/foo_and_bar.py"}),
            ("alpha OR foo -bar", {"src/only_foo.py"}),
            ("gamma OR -zeta", {"src/foo_and_bar.py"}),
            ("foo OR +bar", {"src/only_bar.py", "src/foo_and_bar.py"}),
        ],
    )
    def test_boolean_query_keeps_operator_prefix_semantics(
        self, indexed_manager, query, expected
    ):
        # Expected sets are what these queries returned before the sanitizer
        # learned about dangling operators: the boolean path is unchanged.
        assert _search_paths(indexed_manager, query) == expected

    @pytest.mark.parametrize(
        "query, expected",
        [
            ("foo OR -", {"src/only_foo.py", "src/foo_and_bar.py"}),
            ("- AND foo", {"src/only_foo.py", "src/foo_and_bar.py"}),
            (
                "foo OR - OR bar",
                {"src/only_foo.py", "src/only_bar.py", "src/foo_and_bar.py"},
            ),
        ],
    )
    def test_boolean_operator_orphaned_by_dropped_token(
        self, indexed_manager, query, expected
    ):
        assert _search_paths(indexed_manager, query) == expected

    def test_stripped_boolean_word_keeps_and_semantics(self, indexed_manager):
        # '-OR' strips to the literal word 'or' (a required term), so the
        # query must not turn into 'foo OR bar' and match foo-only/bar-only.
        paths = _search_paths(indexed_manager, "foo -OR bar")
        assert paths == _search_paths(indexed_manager, "foo or bar")
        assert not paths & {"src/only_foo.py", "src/only_bar.py"}

    def test_double_dash_flag_finds_word(self, indexed_manager):
        assert _search_paths(indexed_manager, "--verbose") == {"src/cli.py"}

    @pytest.mark.parametrize(
        "query, match_text, snippet_text",
        [
            ("--verbose", "verbose", "verbose flag"),
            ("foo - bar", "foo bar", "foo bar gamma"),
        ],
    )
    def test_snippet_uses_sanitized_text(
        self, indexed_manager, query, match_text, snippet_text
    ):
        results = indexed_manager.search(query_text=query, limit=10, snippet_lines=3)
        assert len(results) == 1
        assert results[0]["match_text"] == match_text
        assert snippet_text in results[0]["snippet"]

    @pytest.mark.parametrize(
        "query, edit_distance, path",
        [
            ("HashMap::new", 0, "src/map.rs"),
            ("Hashmap::new", 1, "src/map.rs"),
            ("ns:Cls", 0, "src/ns.py"),
        ],
    )
    def test_snippet_matches_raw_query_first(
        self, indexed_manager, query, edit_distance, path
    ):
        # The raw text is the precise location; the sanitized first word
        # ('HashMap', 'ns') would land on the earlier line 1.
        results = indexed_manager.search(
            query_text=query, limit=10, snippet_lines=3, edit_distance=edit_distance
        )
        hit = next(r for r in results if r["path"] == path)
        assert hit["line"] == 2
        assert hit["match_text"] == query

    @pytest.mark.parametrize("query", ["-", "+", " - + "])
    @pytest.mark.parametrize("edit_distance", [0, 1])
    def test_operator_only_query_returns_empty_without_warning(
        self, indexed_manager, caplog, query, edit_distance
    ):
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            paths = _search_paths(indexed_manager, query, edit_distance=edit_distance)
        assert paths == set()
        assert _warnings(caplog) == []

    def test_unparseable_required_term_returns_empty_with_one_warning(
        self, indexed_manager, caplog
    ):
        # '^' survives sanitization but Tantivy rejects it ("Syntax Error: ^").
        # Every term is required (AND), so dropping it would silently broaden
        # the query to 'foo' alone: the search fails loudly instead.
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            paths = _search_paths(indexed_manager, "foo ^")
        assert paths == set()
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "'^'" in warnings[0].getMessage()

    def test_all_terms_unparseable_returns_empty_with_one_warning(
        self, indexed_manager, caplog
    ):
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            paths = _search_paths(indexed_manager, "^ *foo")
        assert paths == set()
        assert len(_warnings(caplog)) == 1
