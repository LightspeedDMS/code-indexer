"""Story #1295 (Epic #1288 final): lifespan wires EmbeddingCacheOtelMetrics.

Replaces test_lifespan_query_embedding_cache_metrics_wiring_1109.py (deleted)
and test_lifespan_metrics_telemetry_disabled_1109.py (deleted) -- both were
built entirely around the retiring QueryEmbeddingCacheMetrics in-process
tracker. EmbeddingCacheOtelMetrics has no in-process tallies and no
process-level accessor to guard: it is a single OTEL-SDK-registered object
built once at startup, so wiring correctness is a source-text + constructor-
argument concern, not an accessor-roundtrip concern.

Guards:
  G1  lifespan.py imports/constructs EmbeddingCacheOtelMetrics
  G2  lifespan.py no longer references the deleted QueryEmbeddingCacheMetrics
  G3  lifespan.py no longer references the deleted clear_query_embedding_cache_metrics
  G4  windowed_metrics_fn source resolves via get_search_embed_event_writer
  G5  total_entries_fn source resolves via the wired query embedding cache
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class TestLifespanConstructsEmbeddingCacheOtelMetrics:
    def test_embedding_cache_otel_metrics_imported(self):
        source = _LIFESPAN_PATH.read_text()
        assert "EmbeddingCacheOtelMetrics" in source, (
            "lifespan.py must construct an EmbeddingCacheOtelMetrics instance "
            "(Story #1295 DB-backed OTEL re-source)"
        )

    def test_deleted_query_embedding_cache_metrics_class_absent(self):
        """No CODE construction of the deleted class -- explanatory comments
        documenting the retired design are fine (and expected)."""
        source = _LIFESPAN_PATH.read_text()
        assert "QueryEmbeddingCacheMetrics(" not in source, (
            "lifespan.py must not construct the deleted QueryEmbeddingCacheMetrics"
        )

    def test_deleted_clear_accessor_absent(self):
        """No CODE call to the deleted teardown function -- explanatory
        comments documenting the retired design are fine (and expected)."""
        source = _LIFESPAN_PATH.read_text()
        assert "clear_query_embedding_cache_metrics()" not in source, (
            "lifespan.py must not call the deleted "
            "clear_query_embedding_cache_metrics() teardown"
        )

    def test_deleted_set_accessor_absent(self):
        source = _LIFESPAN_PATH.read_text()
        assert "set_query_embedding_cache_metrics" not in source, (
            "lifespan.py must not reference the deleted "
            "set_query_embedding_cache_metrics accessor"
        )

    def test_windowed_metrics_source_wired_to_search_embed_event_writer(self):
        source = _LIFESPAN_PATH.read_text()
        assert "get_search_embed_event_writer" in source, (
            "EmbeddingCacheOtelMetrics windowed_metrics_fn must be sourced from "
            "the search-embed-event writer accessor"
        )

    def test_total_entries_fn_wired_to_query_embedding_cache(self):
        source = _LIFESPAN_PATH.read_text()
        # get_query_embedding_cache is used both for the Story #1105 cache wiring
        # AND for total_entries_fn here -- assert the accessor is resolved
        # before the EmbeddingCacheOtelMetrics(...) CONSTRUCTOR CALL (the last
        # occurrence of the name; the first occurrence is its import line).
        otel_construction_pos = source.rfind("EmbeddingCacheOtelMetrics(")
        cache_accessor_pos = source.rfind(
            "get_query_embedding_cache", 0, otel_construction_pos
        )
        assert otel_construction_pos != -1, "EmbeddingCacheOtelMetrics(...) not found"
        assert cache_accessor_pos != -1, (
            "get_query_embedding_cache must be resolved before "
            "EmbeddingCacheOtelMetrics construction (total_entries_fn source)"
        )
