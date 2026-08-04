"""A NATIVELY-built ``chunks.db`` collection is fully consolidated -- found
while verifying Bug #1528 with a real ``cidx index --index-commits`` run.

``chunks_db_content_manifest.json`` is written ONLY by the migration engine
(``collection_migration.py``). Its whole purpose is to make the DESTRUCTIVE
deletion of legacy sharded files safe. A collection built directly in the
consolidated layout -- every server-provisioned collection, since Story
#1488 stamps ``--new-collection-layout=chunks_db`` on every server-spawned
`cidx index` child, and now every temporal collection (Bug #1528) -- never
had legacy files and therefore never gets a manifest.

Before this fix such a collection was misjudged:

  * ``verify_collection_fully_migrated()`` returned False, so fleet
    migration's ``is_repo_already_migrated()`` kept reporting the repo as
    unmigrated; and
  * ``consolidate_collection_in_place()`` then took its resume path and
    raised ``UnrecoverableConsolidationCorruptionError`` ("the manifest is
    missing entirely"), the terminal loud-failure path -- for a collection
    that is in fact perfectly consolidated and has nothing to delete.

The guard must be preserved exactly where it matters: a genuine crashed
migration (discriminator set, legacy files STILL on disk, no manifest) is a
real destructive-decision situation and must still fail loudly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.collection_migration import (
    UnrecoverableConsolidationCorruptionError,
    consolidate_collection_in_place,
    verify_collection_fully_migrated,
)

COLLECTION = "code-indexer-voyage-code-3"
VECTOR_SIZE = 8
RNG_SEED = 1528
LEGACY_SHARD_DEPTH_NAME = "ab"


def _points() -> List[Dict[str, Any]]:
    # Any: a point record is the store's own heterogeneous public contract.
    rng = np.random.default_rng(RNG_SEED)
    return [
        {
            "id": "p0",
            "vector": rng.standard_normal(VECTOR_SIZE).astype(np.float64).tolist(),
            "payload": {"path": "src/a.py"},
            "chunk_text": "x",
        }
    ]


def _build_native_chunks_db(index_path: Path) -> Path:
    store = FilesystemVectorStore(
        base_path=index_path, use_chunks_db_for_new_collections=True
    )
    store.create_collection(COLLECTION, vector_size=VECTOR_SIZE)
    store.begin_indexing(COLLECTION)
    store.upsert_points(COLLECTION, _points())
    store.end_indexing(COLLECTION)

    collection_dir = index_path / COLLECTION
    assert (collection_dir / "chunks.db").is_file()
    assert not (collection_dir / "chunks_db_content_manifest.json").exists(), (
        "the native build path unexpectedly writes a migration manifest -- "
        "update this test if that contract changed"
    )
    return collection_dir


class TestNativelyBuiltChunksDbIsFullyConsolidated:
    def test_verify_reports_fully_migrated(self, tmp_path: Path) -> None:
        collection_dir = _build_native_chunks_db(tmp_path / "index")

        assert verify_collection_fully_migrated(collection_dir) is True

    def test_consolidation_is_a_no_op_instead_of_raising(self, tmp_path: Path) -> None:
        collection_dir = _build_native_chunks_db(tmp_path / "index")

        result = consolidate_collection_in_place(
            collection_dir, deletion_authorized=True
        )

        assert result.status == "already_consolidated"
        assert result.old_files_deleted == 0
        # The data is untouched and still readable.
        reader = FilesystemVectorStore(base_path=collection_dir.parent)
        assert reader.get_point("p0", COLLECTION) is not None


class TestCrashedMigrationStillFailsLoudly:
    def test_discriminator_set_with_legacy_files_and_no_manifest_still_raises(
        self, tmp_path: Path
    ) -> None:
        """The manifest guard must keep protecting the case it exists for:
        legacy files STILL present means a real destructive decision, and
        without a manifest that decision cannot be made safely."""
        collection_dir = _build_native_chunks_db(tmp_path / "index")

        # Simulate a crash between the durable discriminator flip and
        # cleanup completing: a legacy sharded record is still on disk.
        shard = collection_dir / LEGACY_SHARD_DEPTH_NAME / LEGACY_SHARD_DEPTH_NAME
        shard.mkdir(parents=True)
        (shard / "vector_leftover1.json").write_text(
            json.dumps(
                {
                    "id": "leftover1",
                    "vector": [0.0] * VECTOR_SIZE,
                    "metadata": {},
                    "payload": {"path": "src/b.py"},
                    "chunk_text": "y",
                }
            )
        )

        assert verify_collection_fully_migrated(collection_dir) is False

        with pytest.raises(UnrecoverableConsolidationCorruptionError):
            consolidate_collection_in_place(collection_dir, deletion_authorized=True)
