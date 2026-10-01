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

Code-review remediation (F3, on the original #1652/#1655 commit): the
normalization logic itself is no longer duplicated in wiki_cache.py — it
was extracted to the shared `parse_json_column()` helper in
server/storage/json_column.py (see test_json_column.py for the canonical,
comprehensive unit coverage of that helper, including malformed/wrong-shape
cases and the WARNING-truncation fix). This file now keeps ONLY
integration-level tests proving WikiCache.get_sidebar()/get_article()
correctly wire that shared helper in — i.e. that a fake backend returning
already-deserialized dict/list values (simulating psycopg's real JSONB
behavior) is handled without raising, without requiring a live PostgreSQL
server.
"""

import logging
from pathlib import Path

_WIKI_CACHE_MODULE = "code_indexer.server.wiki.wiki_cache"
_JSON_COLUMN_MODULE = "code_indexer.server.storage.json_column"


def _warnings(caplog):
    return [
        r for r in caplog.records if r.name in (_WIKI_CACHE_MODULE, _JSON_COLUMN_MODULE)
    ]


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

        with caplog.at_level(logging.WARNING):
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
