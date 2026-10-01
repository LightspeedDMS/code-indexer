"""Confirmation test for Issue #1459 item 2 (AC1/AC5).

TemporalMetadataStore.detect_format already routes CHUNKS_DB-layout
collections through the canonical `resolve_chunk_layout` resolver FIRST
(Story #1457 round-4 Codex finding, see the docstring in
temporal_metadata_store.py) and only falls back to the legacy
temporal_metadata.db / vector-file detection for SHARDED_JSON collections.

This test does NOT change production code -- it proves the existing
resolver-first dispatch is authoritative by deliberately leaving out both
legacy v2 markers (no `temporal_metadata.db`, no `vector_<16-hex>.json`
file) that the fallback path would need to also say "v2". If detect_format
returns "v2" here, it can only be via the resolve_chunk_layout() check.
"""

from pathlib import Path

from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.temporal_metadata_store import TemporalMetadataStore


def test_detect_format_returns_v2_for_chunks_db_layout_without_legacy_markers(
    tmp_path: Path,
):
    collection_path = tmp_path / "code-indexer-temporal-voyage_3"
    collection_path.mkdir()

    # Minimal collection_meta.json (required by write_chunks_db_discriminator).
    (collection_path / "collection_meta.json").write_text(
        '{"name": "code-indexer-temporal-voyage_3", "vector_size": 4}'
    )
    write_chunks_db_discriminator(collection_path)

    # Deliberately absent: temporal_metadata.db (legacy CLI/solo v2 marker)
    # and any vector_<16-hex>.json file (legacy PG-cluster v2 marker). If the
    # legacy fallback were reached instead of the resolver-first check, this
    # would report "v1".
    assert not (collection_path / TemporalMetadataStore.METADATA_DB_NAME).exists()

    format_version = TemporalMetadataStore.detect_format(collection_path)

    assert format_version == "v2"
