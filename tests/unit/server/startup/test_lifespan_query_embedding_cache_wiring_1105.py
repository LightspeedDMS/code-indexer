"""Story #1105 regression guard: lifespan wires the query embedding cache.

Mirrors the pattern of test_lifespan_coalescer_registry_wiring.py.

The QueryEmbeddingCache is constructed ONCE in server lifespan startup via
``set_query_embedding_cache(QueryEmbeddingCache(...))`` sourced from
``backend_registry.query_embedding_cache`` (both SQLite and PG modes),
and cleared on shutdown via ``clear_query_embedding_cache()``.

Source-order guards: set must be BEFORE the ``yield`` boundary, clear AFTER.
Argument guard: ``backend_registry.query_embedding_cache`` must appear as the
backend source (not a hardcoded alternative).
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class TestLifespanQueryEmbeddingCacheWiring:
    def test_set_query_embedding_cache_present_in_startup(self):
        source = _LIFESPAN_PATH.read_text()
        assert "set_query_embedding_cache" in source, (
            "lifespan.py must install the query embedding cache via "
            "set_query_embedding_cache(...) on startup"
        )

    def test_clear_query_embedding_cache_present_in_shutdown(self):
        source = _LIFESPAN_PATH.read_text()
        assert "clear_query_embedding_cache" in source, (
            "lifespan.py must clear the query embedding cache on shutdown via "
            "clear_query_embedding_cache()"
        )

    def test_set_before_yield_and_clear_after_yield(self):
        source = _LIFESPAN_PATH.read_text()
        yield_pos = source.find("yield  # Server is now running")
        set_pos = source.find("set_query_embedding_cache")
        clear_pos = source.find("clear_query_embedding_cache")

        assert yield_pos != -1, "could not locate the lifespan yield boundary"
        assert set_pos != -1, "set_query_embedding_cache not found in lifespan.py"
        assert clear_pos != -1, "clear_query_embedding_cache not found in lifespan.py"
        assert set_pos < yield_pos, (
            "set_query_embedding_cache must run during STARTUP (before the yield)"
        )
        assert clear_pos > yield_pos, (
            "clear_query_embedding_cache must run during SHUTDOWN (after the yield)"
        )

    def test_backend_registry_query_embedding_cache_is_source(self):
        source = _LIFESPAN_PATH.read_text()
        assert "backend_registry.query_embedding_cache" in source, (
            "lifespan.py must source the cache backend from "
            "backend_registry.query_embedding_cache (both SQLite and PG modes)"
        )

    def test_query_embedding_cache_service_constructed(self):
        source = _LIFESPAN_PATH.read_text()
        assert "QueryEmbeddingCache" in source, (
            "lifespan.py must construct a QueryEmbeddingCache instance on startup"
        )
