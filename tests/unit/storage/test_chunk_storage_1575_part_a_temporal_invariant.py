"""Bug #1575 Part A (AC5) -- Codex review follow-up (finding 7).

IMPORTANT, read before treating anything below as an "invariant proof":
this file is an EMPIRICAL SURVEY of today's two real production writers,
NOT a falsifiable invariant proof, and NOT the safety mechanism that
protects `distinct_content_paths()`. It exercises the ACTUAL production
functions:
  - `temporal_point_builder.build_chunk_payload()` (the real temporal
    per-commit chunk payload builder, Story #1290/Epic #1289) through
    `FilesystemVectorStore.upsert_points()` into a real CHUNKS_DB
    collection.
  - `GitAwareMetadataSchema.create_git_aware_metadata()` (the real
    semantic/multimodal content payload builder) through the same
    `upsert_points()` path, for contrast.

Why this cannot be made genuinely falsifiable: a falsifiable test needs a
REAL writer code path that produces a non-"content"-typed record WITH
`payload.path` set, so the test can assert it is correctly excluded.
Confirmed directly against the source: `GitAwareMetadataSchema.
create_git_aware_metadata()` hardcodes `"type": "content"` unconditionally
(`metadata_schema.py`, both content-record construction sites) -- there is
no parameter, branch, or code path in this codebase that lets it (or any
other current writer) produce a non-content record with a path. With no
such writer to call, no genuinely discriminating test can be constructed
against real code -- inventing a synthetic non-content-with-path record
(as `test_chunk_storage_1575_part_a_ac5.py` does) is a DIFFERENT test:
it exercises `ChunkStore`'s own filtering contract directly, not "what do
today's real writers do".

The ACTUAL safety mechanism against a hypothetical FUTURE writer that
violates this pairing is the indexed `type`-column filter in
`distinct_content_paths()` (`sqlite_chunk_store.py`), proven separately and
directly in `test_chunk_storage_1575_part_a_ac5.py`'s
`TestDistinctContentPathsFiltersByType` -- THAT test would still correctly
exclude such a row even if this file's premise ever stopped holding. This
file exists only to document, and pin via regression, what today's two
real writers are confirmed to do -- it is a survey/regression guard, never
cited as proof that a future writer cannot violate the pairing.
"""

import sqlite3

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.sqlite_chunk_store import ChunkStore
from code_indexer.services.metadata_schema import GitAwareMetadataSchema
from code_indexer.services.temporal.contextual_chunker import AggregatedChunk
from code_indexer.services.temporal.models import CommitInfo
from code_indexer.services.temporal.temporal_point_builder import (
    build_chunk_payload,
    build_point_id,
)

VECTOR_DIM = 16


def _vector(seed: int = 0):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist()


class TestTemporalCommitChunkNeverSetsPath:
    """The real temporal writer (`build_chunk_payload`) never populates
    `payload.path` -- it uses `paths`/`primary_path` instead -- so its
    `type == "commit_chunk"` rows are stored with a NULL `path` column and
    are naturally invisible to any path-keyed enumeration, real writer
    behavior confirmed end-to-end through the actual CHUNKS_DB write path.
    """

    def test_commit_chunk_payload_has_no_path_key(self):
        commit = CommitInfo(
            hash="abc123",
            timestamp=1700000000,
            author_name="Test Author",
            author_email="test@example.com",
            message="Fix the thing",
            parent_hashes="def456",
        )
        chunk = AggregatedChunk(
            text="diff content here",
            chunk_index=0,
            char_start=0,
            char_end=18,
            is_head=True,
            paths=["src/foo.py", "src/bar.py"],
            primary_path="src/foo.py",
        )

        payload = build_chunk_payload(commit, chunk, project_id="proj1")

        assert payload["type"] == "commit_chunk"
        assert "path" not in payload

    def test_upserted_commit_chunk_row_has_null_path_column(self, tmp_path):
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        collection_name = "code-indexer-temporal-embed-2025-q1"
        store.create_collection(collection_name, vector_size=VECTOR_DIM)
        collection_path = store._get_collection_path(collection_name)

        commit = CommitInfo(
            hash="abc123",
            timestamp=1700000000,
            author_name="Test Author",
            author_email="test@example.com",
            message="Fix the thing",
            parent_hashes="",
        )
        chunk = AggregatedChunk(
            text="diff content here",
            chunk_index=0,
            char_start=0,
            char_end=18,
            is_head=True,
            paths=["src/foo.py"],
            primary_path="src/foo.py",
        )
        payload = build_chunk_payload(commit, chunk, project_id="proj1")
        point_id = build_point_id("proj1", commit.hash, chunk.chunk_index)

        store.begin_indexing(collection_name)
        store.upsert_points(
            collection_name,
            [{"id": point_id, "vector": _vector(1), "payload": payload}],
        )
        store.end_indexing(collection_name)

        conn = sqlite3.connect(str(collection_path / "chunks.db"))
        try:
            row = conn.execute(
                "SELECT path, type FROM chunks WHERE point_id = ?", (point_id,)
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        stored_path, stored_type = row
        assert stored_path is None
        assert stored_type == "commit_chunk"


class TestSemanticContentRecordAlwaysSetsPathAndContentType:
    """Contrast case: the real semantic content writer
    (`GitAwareMetadataSchema.create_git_aware_metadata`) ALWAYS sets both
    `type == "content"` and `payload.path` together.
    """

    def test_upserted_content_row_has_path_and_content_type(self, tmp_path):
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("semantic-coll", vector_size=VECTOR_DIM)
        collection_path = store._get_collection_path("semantic-coll")

        payload = GitAwareMetadataSchema.create_git_aware_metadata(
            path="src/real_file.py",
            content="def foo(): pass",
            language="python",
            file_size=15,
            chunk_index=0,
            total_chunks=1,
            project_id="proj1",
            file_hash="deadbeef",
        )

        store.begin_indexing("semantic-coll")
        store.upsert_points(
            "semantic-coll",
            [{"id": "point-1", "vector": _vector(2), "payload": payload}],
        )
        store.end_indexing("semantic-coll")

        chunk_store = ChunkStore(collection_path / "chunks.db")
        try:
            result = chunk_store.distinct_content_paths()
        finally:
            chunk_store.close()

        assert result == {"src/real_file.py"}
