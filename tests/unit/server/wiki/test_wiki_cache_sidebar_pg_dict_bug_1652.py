"""
Unit tests for Bug #1652: wiki_cache.py crashes with unguarded HTTP 500 on
PostgreSQL JSONB sidebar_json.

wiki_sidebar_cache.sidebar_json is declared JSONB NOT NULL in the real
migration (001_initial_schema.sql). WikiCachePostgresBackend.get_sidebar()
returns the row's raw value, which psycopg already deserializes into a
native python `list` on PostgreSQL (JSONB auto-cast) — but a TEXT/JSON
string on SQLite. WikiCache.get_sidebar() called `json.loads(raw)`
unconditionally with no isinstance check and no try/except, so on a
PostgreSQL-backed deployment every wiki page view raised an unhandled
TypeError ("the JSON object must be str, bytes or bytearray, not list"),
producing an unguarded HTTP 500 (see wiki/routes.py lines 288, 416, 541, 748
which all call cache.get_sidebar()).

wiki_cache.py's get_article() has the analogous latent defect for the
`metadata` column: the parsing is wrapped in try/except (ValueError,
TypeError) so it does not crash, but it silently treats a valid dict
(PostgreSQL JSONB) as malformed, logs a spurious WARNING, and discards the
cached front-matter metadata.

This is the same root-cause class as Bug #1622
(dependency_map_routes.py's _parse_phase_timings_value), and the fix here
follows that exact pattern: accept a value already of the expected type
(list for sidebar_json, dict for metadata) as-is; only call json.loads()
for str/bytes; log a WARNING and return None for anything else that is
malformed — never raise.

Tests target the extracted `_parse_json_cache_value()` helper directly
(smallest correct unit boundary, mirrors test_phase_timings_pg_dict_bug_1622.py),
plus integration-level tests against WikiCache.get_sidebar()/get_article()
using a fake backend that returns already-deserialized dict/list values,
simulating psycopg's real JSONB behavior without requiring a live
PostgreSQL server.
"""

import logging
from pathlib import Path

_WIKI_CACHE_MODULE = "code_indexer.server.wiki.wiki_cache"


def _warnings(caplog):
    return [r for r in caplog.records if r.name == _WIKI_CACHE_MODULE]


class _FakeBackend:
    """Minimal WikiCacheBackend stand-in returning pre-set raw values.

    Only implements the two methods WikiCache.get_sidebar()/get_article()
    call — no isinstance/Protocol conformance needed for these tests.
    """

    def __init__(self, sidebar_raw=None, article_row=None):
        self._sidebar_raw = sidebar_raw
        self._article_row = article_row

    def get_sidebar(self, repo_alias):
        return self._sidebar_raw

    def get_article(self, repo_alias, article_path):
        return self._article_row


class TestParseJsonCacheValueNoneAndPassthrough:
    """None passthrough, and Bug #1652's core fix: matching-type passthrough."""

    def test_none_value_returns_none_without_warning(self, caplog):
        from code_indexer.server.wiki.wiki_cache import _parse_json_cache_value

        with caplog.at_level(logging.WARNING, logger=_WIKI_CACHE_MODULE):
            result = _parse_json_cache_value(None, list, "sidebar_json")

        assert result is None
        assert _warnings(caplog) == []

    def test_list_value_returned_as_is_without_warning(self, caplog):
        """Bug #1652: a list value (PostgreSQL JSONB sidebar_json) must be used directly."""
        from code_indexer.server.wiki.wiki_cache import _parse_json_cache_value

        raw = [{"title": "Home", "path": "home", "children": []}]
        with caplog.at_level(logging.WARNING, logger=_WIKI_CACHE_MODULE):
            result = _parse_json_cache_value(raw, list, "sidebar_json")

        assert result == raw
        assert result is raw
        assert _warnings(caplog) == []

    def test_dict_value_returned_as_is_without_warning(self, caplog):
        """A dict value (PostgreSQL JSONB metadata) must be used directly."""
        from code_indexer.server.wiki.wiki_cache import _parse_json_cache_value

        raw = {"author": "alice", "tags": ["a", "b"]}
        with caplog.at_level(logging.WARNING, logger=_WIKI_CACHE_MODULE):
            result = _parse_json_cache_value(raw, dict, "metadata_json")

        assert result == raw
        assert _warnings(caplog) == []


class TestParseJsonCacheValueStringAndBytes:
    """Regression: SQLite's TEXT column (str, and str's bytes cousin)."""

    def test_json_string_list_still_parses_without_warning(self, caplog):
        from code_indexer.server.wiki.wiki_cache import _parse_json_cache_value

        with caplog.at_level(logging.WARNING, logger=_WIKI_CACHE_MODULE):
            result = _parse_json_cache_value(
                '[{"title": "Home", "path": "home"}]', list, "sidebar_json"
            )

        assert result == [{"title": "Home", "path": "home"}]
        assert _warnings(caplog) == []

    def test_bytes_value_still_parses_without_warning(self, caplog):
        from code_indexer.server.wiki.wiki_cache import _parse_json_cache_value

        with caplog.at_level(logging.WARNING, logger=_WIKI_CACHE_MODULE):
            result = _parse_json_cache_value(
                b'{"author": "bob"}', dict, "metadata_json"
            )

        assert result == {"author": "bob"}
        assert _warnings(caplog) == []


class TestParseJsonCacheValueMalformed:
    """Regression: genuinely malformed values must still WARN and return None."""

    def test_malformed_json_string_warns_and_returns_none(self, caplog):
        from code_indexer.server.wiki.wiki_cache import _parse_json_cache_value

        with caplog.at_level(logging.WARNING, logger=_WIKI_CACHE_MODULE):
            result = _parse_json_cache_value("not-valid-json{", list, "sidebar_json")

        assert result is None
        assert len(_warnings(caplog)) == 1

    def test_unexpected_scalar_type_warns_and_returns_none(self, caplog):
        from code_indexer.server.wiki.wiki_cache import _parse_json_cache_value

        with caplog.at_level(logging.WARNING, logger=_WIKI_CACHE_MODULE):
            result = _parse_json_cache_value(42, list, "sidebar_json")

        assert result is None
        assert len(_warnings(caplog)) == 1

    def test_wrong_shape_json_value_warns_and_returns_none(self, caplog):
        """A JSON object string when a list was expected must still warn."""
        from code_indexer.server.wiki.wiki_cache import _parse_json_cache_value

        with caplog.at_level(logging.WARNING, logger=_WIKI_CACHE_MODULE):
            result = _parse_json_cache_value('{"not": "a list"}', list, "sidebar_json")

        assert result is None
        assert len(_warnings(caplog)) == 1


class TestGetSidebarWithPostgresDictBackend:
    """Integration-level: WikiCache.get_sidebar() against a fake PG-shaped backend."""

    def test_get_sidebar_does_not_raise_when_backend_returns_native_list(self):
        """
        Bug #1652 core reproduction: before the fix, this call raised
        TypeError("the JSON object must be str, bytes or bytearray, not list")
        because get_sidebar() called json.loads() unconditionally.
        """
        from code_indexer.server.wiki.wiki_cache import WikiCache

        sidebar_data = [{"title": "Home", "path": "home", "children": []}]
        backend = _FakeBackend(sidebar_raw=sidebar_data)
        cache = WikiCache(db_path=":memory:", storage_backend=backend)

        result = cache.get_sidebar("my-repo", Path("/nonexistent"))

        assert result == sidebar_data

    def test_get_sidebar_returns_none_when_backend_has_no_row(self):
        from code_indexer.server.wiki.wiki_cache import WikiCache

        backend = _FakeBackend(sidebar_raw=None)
        cache = WikiCache(db_path=":memory:", storage_backend=backend)

        assert cache.get_sidebar("my-repo", Path("/nonexistent")) is None

    def test_get_sidebar_still_parses_json_string_from_sqlite_shaped_backend(self):
        """Regression: a TEXT-column backend returning a JSON string must still work."""
        from code_indexer.server.wiki.wiki_cache import WikiCache

        backend = _FakeBackend(sidebar_raw='[{"title": "Home", "path": "home"}]')
        cache = WikiCache(db_path=":memory:", storage_backend=backend)

        result = cache.get_sidebar("my-repo", Path("/nonexistent"))

        assert result == [{"title": "Home", "path": "home"}]


class TestGetArticleWithPostgresDictBackendMetadata:
    """Integration-level: WikiCache.get_article() metadata parsing must accept a dict."""

    def test_get_article_returns_native_dict_metadata_without_warning(
        self, tmp_path, caplog
    ):
        """
        Bug #1652 (analogous latent defect): before the fix, a dict value for
        metadata_json (PostgreSQL JSONB) hit the try/except's TypeError branch,
        logged a spurious WARNING, and returned metadata=None — silently
        discarding valid cached front-matter metadata on PostgreSQL.
        """
        from code_indexer.server.wiki.wiki_cache import WikiCache

        article_file = tmp_path / "article.md"
        article_file.write_text("# Hello")
        stat = article_file.stat()

        metadata = {"author": "alice", "tags": ["x", "y"]}
        row = {
            "rendered_html": "<h1>Hello</h1>",
            "title": "Hello",
            "file_mtime": stat.st_mtime,
            "file_size": stat.st_size,
            "metadata_json": metadata,
        }
        backend = _FakeBackend(article_row=row)
        cache = WikiCache(db_path=":memory:", storage_backend=backend)

        with caplog.at_level(logging.WARNING, logger=_WIKI_CACHE_MODULE):
            result = cache.get_article("my-repo", "article", article_file)

        assert result is not None
        assert result["metadata"] == metadata
        assert _warnings(caplog) == []

    def test_get_article_still_parses_json_string_metadata_from_sqlite_shaped_backend(
        self, tmp_path
    ):
        from code_indexer.server.wiki.wiki_cache import WikiCache
        import json

        article_file = tmp_path / "article.md"
        article_file.write_text("# Hello")
        stat = article_file.stat()

        metadata = {"author": "bob"}
        row = {
            "rendered_html": "<h1>Hello</h1>",
            "title": "Hello",
            "file_mtime": stat.st_mtime,
            "file_size": stat.st_size,
            "metadata_json": json.dumps(metadata),
        }
        backend = _FakeBackend(article_row=row)
        cache = WikiCache(db_path=":memory:", storage_backend=backend)

        result = cache.get_article("my-repo", "article", article_file)

        assert result is not None
        assert result["metadata"] == metadata
