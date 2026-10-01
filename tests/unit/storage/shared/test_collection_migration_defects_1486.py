"""Adversarial-defect regression tests for Bug #1486 (Codex review round).

Defect A (HIGH): the read-only completeness oracle
``verify_collection_fully_migrated()`` (invoked lock-free from scheduler
done-detection / ``get_stats``) mutated storage -- a MISSING chunks.db on a
discriminator-committed collection caused a fresh empty chunks.db to be
CREATED as a side effect of asking the predicate. It must classify and
return WITHOUT creating, deleting, rebuilding, flushing, or rewriting
anything.

Uses REAL files and REAL SQLite via the real
``verify_collection_fully_migrated`` -- the SUT's own decision logic is
never mocked.
"""

import json
import os
from pathlib import Path
from typing import Dict, Tuple

import pytest

from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
    write_chunks_db_discriminator,
)
from code_indexer.storage.shared.collection_migration import (
    ConsolidationDurabilityError,
    consolidate_collection_in_place,
    verify_collection_fully_migrated,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore
from tests.unit.storage.shared.test_collection_migration_1458 import (
    _write_collection_meta,
    _write_vector_json,
)


def _snapshot_dir(root: Path) -> Dict[str, Tuple[bytes, int]]:
    """Map every file under ``root`` to (raw bytes, mtime_ns) -- a
    byte-and-timestamp fingerprint used to prove a read-only predicate
    mutated NOTHING on disk."""
    snapshot: Dict[str, Tuple[bytes, int]] = {}
    for dirpath, _dirnames, filenames in os.walk(str(root)):
        for name in filenames:
            p = Path(dirpath) / name
            rel = str(p.relative_to(root))
            st = p.stat()
            snapshot[rel] = (p.read_bytes(), st.st_mtime_ns)
    return snapshot


class TestDefectAReadOnlyOracleNeverMutates:
    """Defect A: ``verify_collection_fully_migrated()`` is a pure,
    lock-free predicate -- it must NEVER create, delete, rebuild, flush,
    or rewrite anything, even on a corrupt/missing chunks.db."""

    def test_verify_oracle_does_not_create_chunks_db_when_missing(
        self, tmp_path: Path
    ) -> None:
        """Discriminator committed + a legacy-flat content manifest (the
        round-1 shape) + NO chunks.db on disk. The pre-fix oracle created a
        fresh empty chunks.db via a mutable ChunkStore open inside the
        flat-manifest upgrade path before returning False -- a storage
        mutation from a pure predicate, racing real migrations on other
        nodes. It must return False AND leave the directory byte-identical."""
        _write_collection_meta(tmp_path)
        # Round-1 legacy-flat manifest referencing one migrated record.
        (tmp_path / "chunks_db_content_manifest.json").write_text(
            json.dumps({"aaaa1111": "deadbeefdeadbeef"})
        )
        # Commit the chunks_db discriminator so the collection resolves as
        # CHUNKS_DB, but leave NO chunks.db and NO legacy vector_*.json --
        # a "migrated & cleaned" collection whose chunks.db then vanished.
        write_chunks_db_discriminator(tmp_path)

        assert resolve_chunk_layout(tmp_path) == ChunkLayout.CHUNKS_DB
        assert not (tmp_path / "chunks.db").exists()

        before = _snapshot_dir(tmp_path)
        result = verify_collection_fully_migrated(tmp_path)
        after = _snapshot_dir(tmp_path)

        assert result is False, (
            "A missing chunks.db on a committed collection is not 'fully "
            "migrated' -- the oracle must report False."
        )
        assert not (tmp_path / "chunks.db").exists(), (
            "Defect A: the read-only oracle CREATED a chunks.db as a side "
            "effect of a pure predicate -- storage mutation from a lock-free "
            "read path, racing real migrations on other nodes."
        )
        assert before == after, (
            "Defect A: the read-only oracle mutated the collection directory "
            f"(added/removed/rewrote files or changed mtimes): "
            f"before={sorted(before)} after={sorted(after)}"
        )


class TestDefectBIdempotentRetryWithStaleLeftover:
    """Defect B: the fresh consolidation path (discriminator NOT yet set)
    kept a HEALTHY leftover chunks.db from an interrupted earlier attempt.
    Once the authoritative legacy source changed between attempts (a normal
    ``cidx index`` ran), the leftover's stale extra row survived the
    INSERT-OR-REPLACE write loop and tripped the exact-set verification on
    EVERY retry forever. The pre-flip legacy source is authoritative, so a
    stale leftover must be discarded and the retry must succeed."""

    def test_stale_leftover_chunks_db_is_discarded_and_retry_succeeds(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        # Current authoritative legacy source: A + C (an earlier attempt saw
        # A + B; a normal `cidx index` since removed B and added C).
        _write_vector_json(tmp_path, "aaaa0001", [1.0, 2.0, 3.0, 4.0], chunk_text="A")
        _write_vector_json(tmp_path, "cccc0003", [9.0, 8.0, 7.0, 6.0], chunk_text="C")

        # An interrupted earlier attempt left a HEALTHY chunks.db holding
        # A + B (B = a now-removed point). The discriminator was never
        # flipped, so the collection still resolves SHARDED_JSON (fresh
        # path).
        chunks_db_path = tmp_path / "chunks.db"
        with ChunkStore(chunks_db_path, durable_synchronous=True) as store:
            store.write_batch(
                [
                    {
                        "id": "aaaa0001",
                        "vector": [1.0, 2.0, 3.0, 4.0],
                        "metadata": {"language": "python"},
                        "payload": {"path": "src/foo.py", "language": "python"},
                        "chunk_text": "A",
                        "indexed_with_uncommitted_changes": True,
                    },
                    {
                        "id": "bbbb0002",
                        "vector": [5.0, 5.0, 5.0, 5.0],
                        "metadata": {"language": "python"},
                        "payload": {"path": "src/stale.py", "language": "python"},
                        "chunk_text": "STALE",
                        "indexed_with_uncommitted_changes": True,
                    },
                ]
            )

        assert resolve_chunk_layout(tmp_path) == ChunkLayout.SHARDED_JSON

        # Pre-fix: raises ConsolidationVerificationError on every call
        # forever (stale bbbb0002 fails the exact-set check).
        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.CHUNKS_DB

        with ChunkStore(chunks_db_path, immutable=True) as store:
            final_ids = set(store.all_point_ids())
        assert final_ids == {"aaaa0001", "cccc0003"}, (
            "Defect B: the final chunks.db must equal the current legacy "
            f"source exactly (no stale leftover row), got {sorted(final_ids)}"
        )

    def test_stale_leftover_retry_is_repeatable(self, tmp_path: Path) -> None:
        """Two consecutive fresh consolidations against the same collection
        both succeed -- proving the fix is genuinely idempotent, not a
        one-shot papering-over (Codex reproduced BOTH consecutive calls
        raising in the pre-fix code)."""
        _write_collection_meta(tmp_path)
        _write_vector_json(tmp_path, "aaaa0001", [1.0, 2.0, 3.0, 4.0], chunk_text="A")

        chunks_db_path = tmp_path / "chunks.db"
        with ChunkStore(chunks_db_path, durable_synchronous=True) as store:
            store.write_batch(
                [
                    {
                        "id": "aaaa0001",
                        "vector": [1.0, 2.0, 3.0, 4.0],
                        "metadata": {"language": "python"},
                        "payload": {"path": "src/foo.py", "language": "python"},
                        "chunk_text": "A",
                        "indexed_with_uncommitted_changes": True,
                    },
                    {
                        "id": "bbbb0002",
                        "vector": [5.0, 5.0, 5.0, 5.0],
                        "metadata": {"language": "python"},
                        "payload": {"path": "src/stale.py", "language": "python"},
                        "chunk_text": "STALE",
                        "indexed_with_uncommitted_changes": True,
                    },
                ]
            )

        first = consolidate_collection_in_place(tmp_path)
        assert first.status == "consolidated"
        # Second call resumes on the now-committed CHUNKS_DB layout -- must
        # also succeed (cleanup no-op), never raise.
        second = consolidate_collection_in_place(tmp_path)
        assert second.status == "already_consolidated"

        with ChunkStore(chunks_db_path, immutable=True) as store:
            final_ids = set(store.all_point_ids())
        assert final_ids == {"aaaa0001"}


class TestDefectCDurabilityGateExceptionSafety:
    """Defect C: the durability gate deleted the bad chunks.db only when
    the integrity check RETURNED not-ok -- but a RAW exception from the
    fsync/commit sequence (e.g. an ``OSError`` on NFS) propagated
    unchanged, leaving chunks.db on disk as an untrusted artifact and
    handing the caller an ``OSError`` instead of the typed
    ``ConsolidationDurabilityError``."""

    def test_fsync_oserror_becomes_durability_error_and_removes_chunks_db(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "dddd0004", [1.0, 2.0, 3.0, 4.0], chunk_text="D"
        )

        import code_indexer.storage.sqlite_chunk_store as chunk_store_mod

        def _raise_oserror(_fd):
            raise OSError("simulated NFS fsync failure")

        # flush_durable() is the only ChunkStore method that calls
        # nfs_safe_fsync -- the first such call during consolidation happens
        # inside _force_durable_and_integrity_check, exactly the gate under
        # test.
        monkeypatch.setattr(chunk_store_mod, "nfs_safe_fsync", _raise_oserror)

        with pytest.raises(ConsolidationDurabilityError):
            consolidate_collection_in_place(tmp_path)

        assert not (tmp_path / "chunks.db").exists(), (
            "Defect C: an fsync OSError left the untrusted chunks.db on disk "
            "-- a subsequent retry would trip over it instead of starting "
            "clean."
        )
        assert vfile.exists(), (
            "Defect C: the irreplaceable legacy source was affected by a "
            "durability-gate failure -- it must be left completely untouched."
        )
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.SHARDED_JSON, (
            "Defect C: the discriminator was flipped despite the durability "
            "gate failing."
        )
