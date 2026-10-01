"""Story #1488 fifth-pass adversarial-review (Codex New-High) remediation for
the shared per-collection consolidation engine (``collection_migration.py``).

Real files, REAL SQLite (the real ``ChunkStore``), NO mocking of the code
under test. The ONLY instrumentation is a call-THROUGH spy counter on the
module-level helper ``_write_content_manifest`` (it still runs the real
implementation -- it is an observation, never a stub), used to prove whether
the expensive O(N) manifest re-derive was ENTERED at all on a given pass.

Codex New-High finding (O(N) manifest rebuild on every DELETION-GATED
resume): ``_reconcile_manifest_after_resume_rebuild`` was made to rewrite the
FULL content manifest UNCONDITIONALLY whenever legacy sharded files still
remain (Codex Finding D, round 4 -- correctly closing the stale-digest
terminal-completion bug). But the reconcile runs from inside
``_verify_chunks_db_before_resume_cleanup``, which the resume path calls
BEFORE the ``deletion_authorized`` gate. So on a Story #1460 gated/bake-window
resume (``deletion_authorized=False``, where NO legacy deletion happens), it
STILL performed the full O(N) rewrite every pass -- one ``ChunkStore.read()``
+ vector decompression + content hashing PER POINT, an O(N) manifest write +
fsync -- pure waste, since nothing will be deleted.

Fix: gate the unconditional rewrite on "ACTUALLY about to delete legacy" --
``deletion_authorized=True`` AND legacy still present -- not merely "legacy
present". A ``deletion_authorized=False`` resume skips the full rewrite
ENTIRELY (mixed-layout is the legitimate bake-window state; the next
AUTHORIZED pass reconciles before it deletes, so Finding D is fully
preserved).

The two crux assertions below FAIL on the pre-fix code (RED -- the reconcile
enters ``_write_content_manifest`` on the gated pass) and PASS after (GREEN --
it is skipped), while the AUTHORIZED pass still enters it (Finding D's
rewrite-before-delete preserved).
"""

from __future__ import annotations

import json
from pathlib import Path

from code_indexer.storage.shared import collection_migration as cm
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)
from code_indexer.storage.shared.collection_migration import (
    consolidate_collection_in_place,
    verify_collection_fully_migrated,
)


# --------------------------------------------------------------------------
# Helpers (mirror the real sharded record shape -- same convention as the
# round3/round4 suites).
# --------------------------------------------------------------------------
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


def _build_bake_window_collection(coll: Path) -> None:
    """Build+verify+flip a CHUNKS_DB collection with legacy RETAINED (the
    Story #1460 gated/bake-window state): chunks.db committed, a clean
    content manifest persisted, and the legacy sharded files still on disk.
    """
    _write_collection_meta(coll)
    _write_vector_json(coll, "aa000000", [1.0, 2.0, 3.0, 4.0])
    _write_vector_json(coll, "bb000000", [5.0, 6.0, 7.0, 8.0])

    result = consolidate_collection_in_place(coll, deletion_authorized=False)
    assert result.status == "consolidated"
    assert result.deletion_gated is True
    assert resolve_chunk_layout(coll) == ChunkLayout.CHUNKS_DB
    # Bake window: legacy files remain physically present.
    assert next(coll.rglob("vector_*.json"), None) is not None


def _install_manifest_write_spy(monkeypatch) -> dict:
    """Install a call-THROUGH spy on the module-level
    ``_write_content_manifest`` -- it still executes the real implementation
    (never a stub), only counting entries. Returns a mutable counter dict.
    """
    counter = {"n": 0}
    real = cm._write_content_manifest

    def _spy(*args, **kwargs):
        counter["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(cm, "_write_content_manifest", _spy)
    return counter


# --------------------------------------------------------------------------
# The New-High perf finding: gated resume must NOT enter the O(N) rewrite.
# --------------------------------------------------------------------------
class TestGatedResumeSkipsManifestRewrite:
    def test_gated_resume_does_not_enter_manifest_rewrite(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A ``deletion_authorized=False`` resume of a bake-window collection
        (legacy present, clean manifest) must NOT perform the O(N) full
        manifest re-derive -- nothing is about to be deleted, so the
        stale-manifest-before-delete hazard (Finding D) cannot bite.

        RED before the fix: the reconcile runs unconditionally because legacy
        is present, so the spy counts >= 1 entry. GREEN after: the gated pass
        skips the rewrite entirely, so the counter stays at 0.
        """
        coll = tmp_path / "code-index-gated-noop"
        _build_bake_window_collection(coll)

        counter = _install_manifest_write_spy(monkeypatch)

        result = consolidate_collection_in_place(coll, deletion_authorized=False)

        assert counter["n"] == 0, (
            "the gated (deletion_authorized=False) resume entered the O(N) "
            "manifest re-derive even though NO legacy deletion happens -- "
            f"_write_content_manifest was called {counter['n']} time(s)"
        )
        # No legacy deleted; the collection legitimately stays mixed-layout.
        assert result.status == "already_consolidated"
        assert result.deletion_gated is True
        assert next(coll.rglob("vector_*.json"), None) is not None

    def test_authorized_resume_still_enters_rewrite_before_delete(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Finding D preservation: a ``deletion_authorized=True`` resume IS
        about to delete legacy, so it MUST still re-derive+rewrite the manifest
        BEFORE deleting -- the spy counts >= 1 entry, legacy is then removed,
        and completion is reached.
        """
        coll = tmp_path / "code-index-authorized-rewrite"
        _build_bake_window_collection(coll)

        counter = _install_manifest_write_spy(monkeypatch)

        result = consolidate_collection_in_place(coll, deletion_authorized=True)

        assert counter["n"] >= 1, (
            "the authorized resume did NOT enter the manifest re-derive before "
            "deleting legacy -- Finding D's rewrite-before-delete invariant is "
            "broken"
        )
        assert result.status == "already_consolidated"
        assert next(coll.rglob("vector_*.json"), None) is None
        assert verify_collection_fully_migrated(coll) is True
