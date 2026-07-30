"""Story #1488 fourth-pass adversarial-review (Codex Finding D) remediation
for the shared per-collection consolidation engine
(``collection_migration.py``).

Real files, REAL SQLite (the real ``ChunkStore``), NO mocking of the code
under test. The mid-reconcile crash is reproduced by directly constructing
the genuine post-crash on-disk STATE (using the real ``ChunkStore`` purely
as a fixture harness to establish that state, exactly as a real rebuild
would have committed it) and then running the completely-unmodified
``consolidate_collection_in_place`` resume against it -- rather than
monkeypatching any internal production function.

Codex Finding D (permanent completion-loss + irreplaceable legacy delete):
the prior ``_reconcile_manifest_after_resume_rebuild`` rewrote the persisted
manifest only when a record was rebuilt THIS pass (``rebuilt_any``) OR the
manifest key-SET diverged. That cheap gate is the bug. Terminal sequence:

  1. Resume rebuilds a MISMATCHED still-present row -- the corrected row is
     committed to chunks.db -- then FAULTS (crash / SIGKILL / NFS write
     fault) before the manifest rewrite. That first attempt correctly raises
     before cleanup; legacy is intact, retryable. Good so far.
  2. RETRY: chunks.db already holds the corrected row (from attempt 1), so
     the row now MATCHES its legacy source -> ``rebuilt_any=False``, and the
     manifest key-SET is unchanged (same ids) -> the cheap gate takes the
     early return and SKIPS the manifest rewrite. But the persisted manifest
     still holds the STALE digest for that row.
  3. Cleanup then DELETES the (irreplaceable) legacy source over the stale
     manifest.
  4. chunks.db is correct, the manifest has a stale digest for it, legacy is
     gone -> ``verify_collection_fully_migrated()`` returns FALSE forever,
     and the next automatic retry raises the terminal
     ``UnrecoverableConsolidationCorruptionError``. Row DATA survives in
     chunks.db but the collection can NEVER be marked complete.

Fix (unconditional re-derive-and-rewrite-when-legacy-present): BEFORE
deleting any legacy source on resume, the persisted manifest MUST be proven
to match chunks.db exactly. The reconcile now re-derives + rewrites the full
manifest + authoritative ``vector_count`` from the ACTUAL chunks.db contents
WHENEVER legacy files still remain (they are about to be deleted), never
gated on ``rebuilt_any`` or a key-set change -- structurally eliminating the
matching-key-set-but-stale-digest class the cheap gate let slip through.

The two crux assertions below (``verify_collection_fully_migrated() is True``
after the retry, and no terminal ``UnrecoverableConsolidationCorruptionError``
on any subsequent run) FAIL on the pre-fix code (RED) and PASS after (GREEN).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)
from code_indexer.storage.shared.collection_migration import (
    UnrecoverableConsolidationCorruptionError,
    _CONTENT_MANIFEST_FILENAME,
    _VECTOR_COUNT_META_KEY,
    _compute_record_content_digest,
    consolidate_collection_in_place,
    verify_collection_fully_migrated,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore


# --------------------------------------------------------------------------
# Helpers (mirror the real sharded record shape -- same convention as
# test_collection_migration_codex_1488_round3.py).
# --------------------------------------------------------------------------
def _bb_record(chunk_text: str) -> dict:
    return {
        "id": "bb000000",
        "vector": [5.0, 6.0, 7.0, 8.0],
        "metadata": {"language": "python"},
        "payload": {"path": "src/foo.py", "language": "python"},
        "chunk_text": chunk_text,
        "indexed_with_uncommitted_changes": True,
    }


def _write_vector_json(
    collection_dir: Path,
    point_id: str,
    vector,
    *,
    path: str = "src/foo.py",
    chunk_text: str = "chunk",
) -> Path:
    record = {
        "id": point_id,
        "vector": vector,
        "metadata": {"language": "python"},
        "payload": {"path": path, "language": "python"},
        "chunk_text": chunk_text,
        "indexed_with_uncommitted_changes": True,
    }
    shard_dir = collection_dir / point_id[:2] / point_id[2:4]
    shard_dir.mkdir(parents=True, exist_ok=True)
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))
    return file_path


def _write_collection_meta(collection_dir: Path, vector_size: int = 4) -> None:
    collection_dir.mkdir(parents=True, exist_ok=True)
    (collection_dir / "collection_meta.json").write_text(
        json.dumps({"name": collection_dir.name, "vector_size": vector_size})
    )


def _read_manifest_expected_count(collection_dir: Path) -> int:
    raw = json.loads((collection_dir / _CONTENT_MANIFEST_FILENAME).read_text())
    return int(raw["expected_count"])


def _manifest_recorded_digest(collection_dir: Path, point_id: str) -> str:
    raw = json.loads((collection_dir / _CONTENT_MANIFEST_FILENAME).read_text())
    return str(raw["records"][point_id])


def _read_authoritative_count(collection_dir: Path) -> int:
    raw = json.loads((collection_dir / "collection_meta.json").read_text())
    return int(raw[_VECTOR_COUNT_META_KEY])


def _build_post_crash_state(coll: Path) -> Path:
    """Construct the EXACT on-disk state a crash-after-rebuild-commit-before-
    manifest-rewrite leaves during a resume, using only real files and the
    real ``ChunkStore`` as a fixture harness (the SUT is never touched):

      * a committed CHUNKS_DB collection (aa, bb) with legacy retained (bake
        window) -- so the persisted manifest holds bb's ORIGINAL digest;
      * bb's still-present legacy source EDITED to a new content (the
        mismatch a resume rebuild would correct);
      * chunks.db's bb row overwritten with that EDITED content via a real
        ``ChunkStore.write_batch`` -- byte-for-byte what
        ``_rebuild_missing_or_mismatched_still_present_records`` commits
        before the reconcile runs;
      * the persisted manifest LEFT STALE (still bb's original digest) -- the
        rewrite is exactly the step the simulated crash interrupted.

    Returns bb's legacy file path.
    """
    _write_collection_meta(coll)
    _write_vector_json(coll, "aa000000", [1.0, 2.0, 3.0, 4.0])
    bb = _write_vector_json(
        coll, "bb000000", [5.0, 6.0, 7.0, 8.0], chunk_text="original"
    )

    # Build+verify+flip, legacy retained (bake window). Manifest holds bb's
    # ORIGINAL digest at this point.
    consolidate_collection_in_place(coll, deletion_authorized=False)
    assert resolve_chunk_layout(coll) == ChunkLayout.CHUNKS_DB

    # bb's still-present legacy source is edited so it MISMATCHES chunks.db.
    bb.write_text(json.dumps(_bb_record("EDITED-DIFFERENT-CONTENT")))

    # The corrected bb row is committed to chunks.db -- exactly what a resume
    # rebuild does BEFORE the reconcile step the crash interrupted. Uses the
    # real ChunkStore as a fixture harness; the SUT is not involved.
    with ChunkStore(coll / "chunks.db", durable_synchronous=True) as store:
        store.write_batch([_bb_record("EDITED-DIFFERENT-CONTENT")])
        store.flush_durable()

    return bb


class TestResumeReconcileIsRetrySafeAfterMidReconcileCrash:
    def test_retry_after_crash_before_manifest_rewrite_reaches_completion(
        self, tmp_path: Path
    ) -> None:
        """Codex Finding D exact terminal sequence at the retry point.

        A resume from the genuine post-crash state (chunks.db corrected,
        manifest STALE, legacy present) must rewrite the manifest to match
        chunks.db, run cleanup, and reach completion -- NEVER the permanent
        block. A third run is a clean no-op.
        """
        coll = tmp_path / "code-index-finding-d"
        bb = _build_post_crash_state(coll)

        # Precondition: this IS the poisonous state -- legacy intact, chunks.db
        # already corrected, and the persisted manifest STALE (its recorded bb
        # digest does not match chunks.db's current bb content).
        assert bb.exists()
        with ChunkStore(coll / "chunks.db") as store:
            stored_bb = store.read("bb000000")
            assert stored_bb is not None
            assert stored_bb["chunk_text"] == "EDITED-DIFFERENT-CONTENT"
            actual_bb_digest = _compute_record_content_digest(stored_bb)
        assert _manifest_recorded_digest(coll, "bb000000") != actual_bb_digest, (
            "test setup did not produce a stale manifest -- cannot reproduce Finding D"
        )

        # RETRY: rebuilt_any is now FALSE (chunks.db already matches legacy)
        # and the key-SET is unchanged -- the exact conditions the buggy cheap
        # gate used to SKIP the rewrite over a STALE-DIGEST manifest. The fix
        # must rewrite unconditionally because legacy is still present and
        # about to be deleted.
        result = consolidate_collection_in_place(coll, deletion_authorized=True)
        assert result.status == "already_consolidated"

        # chunks.db unchanged (2 rows, bb still EDITED); manifest + count now
        # match chunks.db exactly; legacy is gone.
        with ChunkStore(coll / "chunks.db") as store:
            assert store.count() == 2
            assert store.read("bb000000")["chunk_text"] == "EDITED-DIFFERENT-CONTENT"
        assert _read_manifest_expected_count(coll) == 2
        assert _read_authoritative_count(coll) == 2
        assert _manifest_recorded_digest(coll, "bb000000") == actual_bb_digest, (
            "the resume did not rewrite the stale manifest digest to match "
            "chunks.db before deleting legacy"
        )
        assert next(coll.rglob("vector_*.json"), None) is None

        # THE crux: completion is reachable, NOT permanently blocked.
        assert verify_collection_fully_migrated(coll) is True

        # A third run is a clean no-op -- never the terminal corruption error.
        result3 = consolidate_collection_in_place(coll, deletion_authorized=True)
        assert result3.status == "already_consolidated"
        assert verify_collection_fully_migrated(coll) is True

    def test_retry_never_raises_terminal_corruption(self, tmp_path: Path) -> None:
        """Focused guard: the SAME post-crash state must never let a
        subsequent retry raise the terminal
        ``UnrecoverableConsolidationCorruptionError`` -- the row data is fully
        present in chunks.db and its legacy source survived, so this is a
        recoverable retry, not unrecoverable corruption."""
        coll = tmp_path / "code-index-finding-d-terminal"
        _build_post_crash_state(coll)

        try:
            consolidate_collection_in_place(coll, deletion_authorized=True)
            consolidate_collection_in_place(coll, deletion_authorized=True)
        except UnrecoverableConsolidationCorruptionError as exc:  # pragma: no cover
            pytest.fail(
                "retry after a mid-reconcile crash raised the terminal "
                f"UnrecoverableConsolidationCorruptionError: {exc}"
            )
        assert verify_collection_fully_migrated(coll) is True
