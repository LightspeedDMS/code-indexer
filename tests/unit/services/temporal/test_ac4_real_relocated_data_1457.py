"""AC4 re-validation against REAL relocated data (Story #1457).

Round 9 proved the "activated-repo-to-golden-repo" wiring is threaded
correctly (mocked at the _execute_temporal_query boundary). Round 10 built
AC1's real relocation trigger. This file closes the gap the coordinator
flagged: exercise AC4's fix against a REAL refresh cycle rather than
prerequisite-only wiring.

End-to-end, real infrastructure, zero mocking of the code under test:
  1. A golden repo's quarter shard is built in-repo (simulating ordinary
     indexing) with a known row + real 4-dim vector.
  2. maybe_relocate_shard_to_sister_location() is called (AC1's actual
     trigger, Story #1457 round 10) -- publishing the SAME data to the
     sister location via AC6's build+publish machinery.
  3. AC2's build_dedicated_temporal_read_store() constructs the dedicated
     read-side store an ACTIVATED repo's query would use -- rooted at the
     golden repo's sister location, with NO dependency on any activated
     repo's own local files at all (the legacy_index_path passed here is
     a directory that is never populated -- proving the read path finds
     the data purely via the sister pointer).
  4. FilesystemVectorStore.search() with a precomputed_query_vector
     (bypassing the real embedding API -- HNSW search takes a raw vector,
     genuinely exercising the resolver + collection_exists + HNSW load +
     ChunkStore read path) returns the SAME row that was originally
     indexed -- proving AC4: an activated-repo-style query now finds real
     relocated golden-repo data end-to-end.
"""

from __future__ import annotations

import json

from code_indexer.server.storage.postgres.temporal_child_wiring import (
    CIDX_SERVER_REFRESH_CONTEXT_ENV,
)
from code_indexer.services.temporal.temporal_dedicated_store import (
    build_dedicated_temporal_read_store,
)
from code_indexer.services.temporal.temporal_relocation_trigger import (
    maybe_relocate_shard_to_sister_location,
)


def _write_local_shard_row(shard_dir, hash_prefix, point_id, commit_hash, vector):
    shard_dir.mkdir(parents=True, exist_ok=True)
    row = {
        "id": point_id,
        "vector": vector,
        "payload": {
            "commit_hash": commit_hash,
            "path": "src/auth.py",
            "content": "def authenticate(user): ...",
        },
    }
    (shard_dir / f"vector_{hash_prefix}.json").write_text(json.dumps(row))


def test_activated_repo_style_query_finds_real_relocated_golden_repo_data(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")
    monkeypatch.setenv("CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED", "1")

    golden_repos_dir = tmp_path / "golden-repos"
    codebase_dir = golden_repos_dir / "evolution"
    shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"
    local_shard_dir = codebase_dir / ".code-indexer" / "index" / shard_name

    known_vector = [0.11, 0.22, 0.33, 0.44]
    _write_local_shard_row(
        local_shard_dir, "aaaa1111", "proj:commit:c0:0", "c0", known_vector
    )

    # Step 1+2: AC1's real relocation trigger fires, exactly as it does
    # inside TemporalIndexer._index_one_embedder during ordinary refresh.
    maybe_relocate_shard_to_sister_location(
        codebase_dir=codebase_dir,
        shard_name=shard_name,
        local_shard_dir=local_shard_dir,
        new_commit_hashes=["c0"],
        vector_dim=4,
    )

    # Step 3: AC2's dedicated read-side store construction -- the SAME
    # primitive an activated-repo query would use, rooted ONLY at the
    # golden repo's sister location. legacy_index_path points at a
    # directory that is NEVER populated -- proving the data is found
    # purely via the sister pointer, not any activated-repo-local files.
    never_populated_activated_repo_index = (
        tmp_path
        / "activated-repos"
        / "someuser"
        / "evolution-clone"
        / ".code-indexer"
        / "index"
    )
    store = build_dedicated_temporal_read_store(
        golden_repos_dir, "evolution", never_populated_activated_repo_index
    )

    # Step 4: real HNSW search with a precomputed vector (bypasses the
    # embedding API call; still genuinely exercises resolver ->
    # collection_exists -> HNSW load -> ChunkStore read).
    results = store.search(
        query="authenticate user",
        embedding_provider=None,
        collection_name=shard_name,
        precomputed_query_vector=known_vector,
        limit=5,
    )

    assert len(results) == 1, f"expected the relocated row back, got {results}"
    assert results[0]["id"] == "proj:commit:c0:0"
    assert results[0]["payload"]["commit_hash"] == "c0"
