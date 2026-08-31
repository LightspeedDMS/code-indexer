"""Unit tests for backend-aware detect_format() (Bug #1313 Step 8).

In PostgreSQL/cluster mode, temporal_metadata.db never exists on disk (the
metadata lives in PostgreSQL, not SQLite) -- the pre-#1313 detect_format()
would always report "v1" (needs reindex) in cluster mode, which is wrong.

When the registry factory is set (PG mode), detect_format() must instead
report "v2" based on a path-local check: does the collection dir contain at
least one vector_<16-hex>.json file? (v1 cannot exist in a PG cluster since it
postdates Story #669/#1313.) When no factory is set (CLI/solo), behavior is
UNCHANGED: presence of temporal_metadata.db on disk.
"""

import json
import tempfile
from pathlib import Path

import pytest

from code_indexer.storage.shared.chunk_layout import CHUNK_LAYOUT_DISCRIMINATOR_VERSION

#: Story #1457 (2026-07-24 round-4 re-review): shared test constant for
#: the CHUNKS_DB discriminator fixture below -- an arbitrary but
#: consistent vector dimension for these format-detection-only tests
#: (no real vectors are read/written here).
_TEST_VECTOR_SIZE = 4


@pytest.fixture(autouse=True)
def _clear_registry_after_each_test():
    yield
    from code_indexer.storage.temporal_metadata_backend_registry import (
        clear_temporal_metadata_backend_factory,
    )

    clear_temporal_metadata_backend_factory()


class TestDetectFormatClIUnchanged:
    def test_no_factory_v2_when_db_exists(self):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            clear_temporal_metadata_backend_factory,
        )
        from code_indexer.storage.temporal_metadata_store import TemporalMetadataStore

        clear_temporal_metadata_backend_factory()

        with tempfile.TemporaryDirectory() as tmpdir:
            collection_path = Path(tmpdir) / "temporal"
            TemporalMetadataStore(collection_path)

            assert TemporalMetadataStore.detect_format(collection_path) == "v2"

    def test_no_factory_v1_when_db_missing(self):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            clear_temporal_metadata_backend_factory,
        )
        from code_indexer.storage.temporal_metadata_store import TemporalMetadataStore

        clear_temporal_metadata_backend_factory()

        with tempfile.TemporaryDirectory() as tmpdir:
            collection_path = Path(tmpdir) / "temporal"
            collection_path.mkdir()

            assert TemporalMetadataStore.detect_format(collection_path) == "v1"


class TestDetectFormatPgMode:
    def test_factory_set_v2_when_vector_files_present(self):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            set_temporal_metadata_backend_factory,
        )
        from code_indexer.storage.temporal_metadata_store import TemporalMetadataStore

        set_temporal_metadata_backend_factory(lambda path: object())

        with tempfile.TemporaryDirectory() as tmpdir:
            collection_path = Path(tmpdir) / "temporal"
            collection_path.mkdir()
            # 16-hex-char hash prefix vector filename, no temporal_metadata.db
            (collection_path / "vector_0123456789abcdef.json").write_text("{}")

            assert TemporalMetadataStore.detect_format(collection_path) == "v2"

    def test_factory_set_v1_when_no_vector_files_and_no_db(self):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            set_temporal_metadata_backend_factory,
        )
        from code_indexer.storage.temporal_metadata_store import TemporalMetadataStore

        set_temporal_metadata_backend_factory(lambda path: object())

        with tempfile.TemporaryDirectory() as tmpdir:
            collection_path = Path(tmpdir) / "temporal"
            collection_path.mkdir()

            assert TemporalMetadataStore.detect_format(collection_path) == "v1"

    def test_factory_set_never_checks_for_db_file(self):
        """In PG mode temporal_metadata.db never exists on disk -- detect_format
        must NOT rely on Path.exists() for the .db file at all."""
        from code_indexer.storage.temporal_metadata_backend_registry import (
            set_temporal_metadata_backend_factory,
        )
        from code_indexer.storage.temporal_metadata_store import TemporalMetadataStore

        set_temporal_metadata_backend_factory(lambda path: object())

        with tempfile.TemporaryDirectory() as tmpdir:
            collection_path = Path(tmpdir) / "temporal"
            collection_path.mkdir()
            # Simulate a leftover .db file from a prior CLI run migrated into
            # cluster mode -- PG-mode detection must ignore it and look only
            # at vector files.
            (collection_path / "temporal_metadata.db").write_text("")

            assert TemporalMetadataStore.detect_format(collection_path) == "v1"


class TestDetectFormatChunksDbAuthoritative:
    """Story #1457 (2026-07-24 re-review, Codex round-4 finding): a
    consolidated CHUNKS_DB-layout temporal shard (Epic #1454 / Story
    #1457 AC6's sister-location build) has NO legacy vector_<16hex>.json
    files at all -- it is a real chunks.db, not the sharded-JSON format
    detect_format()'s PG-mode branch was written to detect. Without
    consulting resolve_chunk_layout() first, a perfectly healthy
    CHUNKS_DB collection in PG/cluster mode is misreported as "v1"
    needing reindex."""

    def test_chunks_db_layout_is_v2_in_pg_mode_with_zero_vector_files(self):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            set_temporal_metadata_backend_factory,
        )
        from code_indexer.storage.temporal_metadata_store import TemporalMetadataStore

        set_temporal_metadata_backend_factory(lambda path: object())

        with tempfile.TemporaryDirectory() as tmpdir:
            collection_path = Path(tmpdir) / "temporal"
            collection_path.mkdir()
            meta = {
                "name": "temporal",
                "vector_size": _TEST_VECTOR_SIZE,
                "chunks_db": {
                    "enabled": True,
                    "version": CHUNK_LAYOUT_DISCRIMINATOR_VERSION,
                },
            }
            (collection_path / "collection_meta.json").write_text(json.dumps(meta))
            # Deliberately NO vector_<16hex>.json files -- a real CHUNKS_DB
            # collection never has them.

            assert TemporalMetadataStore.detect_format(collection_path) == "v2", (
                "a healthy CHUNKS_DB-layout collection must be detected as "
                "v2 in PG mode even with zero legacy vector files -- "
                "resolve_chunk_layout() must be authoritative"
            )
