"""Unit tests for Bug #1486 Fix A (CRITICAL, data loss):
consolidate_collection_in_place() must force chunks.db to be DURABLE on
the actual backing store and re-verify its integrity via a genuinely
FRESH connection BEFORE flipping the chunks_db discriminator and BEFORE
deleting the irreplaceable legacy vector_*.json source.

Confirmed production root cause: the pre-existing read-back verification
reads chunks.db through the SAME client that just wrote it -- on NFS,
that read can report "correct" even though the write has not yet
reached the NFS SERVER durably. The migration deleted the only copy of
the data before the replacement was PROVABLY durable.

All tests use REAL files and REAL SQLite (via the real ChunkStore) --
the only injected "fault" is a deliberate, explicit file-truncation
inside a call-through-wrapped ChunkStore.flush_durable (the real
implementation still runs; the wrapper only ADDS a subsequent
corruption step), simulating a write that looked committed locally but
was corrupted/lost before it reached the backing store. The actual
corruption DETECTION (PRAGMA integrity_check via a fresh connection) is
real, unmocked SQLite behavior against those genuinely corrupted bytes
-- no part of collection_migration.py's own decision logic is mocked.
"""

import json
from pathlib import Path

import pytest

from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)
from code_indexer.storage.shared.collection_migration import (
    ConsolidationDurabilityError,
    ConsolidationVerificationError,
    UnrecoverableConsolidationCorruptionError,
    _write_authoritative_vector_count,
    consolidate_collection_in_place,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore
from tests.unit.storage.shared.test_collection_migration_1458 import (
    _write_collection_meta,
    _write_manifest_for_records,
    _write_vector_json,
)


class TestFreshPathDurabilityCheck:
    """Fix A items 1-3: force durability + fresh-connection integrity
    check BEFORE the discriminator flip and BEFORE legacy deletion."""

    def test_corruption_detected_after_durable_flush_raises_and_preserves_legacy(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Simulate a write that looked fine locally but was
        corrupted/truncated on the actual backing store (e.g. an NFS
        round-trip) -- discovered only by the NEW fresh-connection
        integrity check. Must raise loudly and must NEVER delete the
        legacy source or flip the discriminator."""
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "zz001111", [1.0, 2.0, 3.0, 4.0], chunk_text="original"
        )

        import code_indexer.storage.sqlite_chunk_store as chunk_store_mod

        original_flush = chunk_store_mod.ChunkStore.flush_durable

        def _flush_then_corrupt(self):
            original_flush(self)
            # Simulate corruption discovered only on a fresh re-read:
            # truncate the file to a fraction of its real size, well
            # below a valid SQLite header.
            with open(self.db_path, "r+b") as f:
                f.truncate(20)

        monkeypatch.setattr(
            chunk_store_mod.ChunkStore, "flush_durable", _flush_then_corrupt
        )

        with pytest.raises(ConsolidationDurabilityError):
            consolidate_collection_in_place(tmp_path)

        assert vfile.exists(), (
            "Bug: the legacy source was deleted despite chunks.db failing "
            "the post-durability-flush fresh-connection integrity check."
        )
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.SHARDED_JSON, (
            "Bug: the discriminator was flipped despite a durability/integrity failure."
        )

    def test_durability_failure_removes_the_bad_chunks_db_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(tmp_path, "zz002222", [1.0, 2.0, 3.0, 4.0], chunk_text="x")

        import code_indexer.storage.sqlite_chunk_store as chunk_store_mod

        original_flush = chunk_store_mod.ChunkStore.flush_durable

        def _flush_then_corrupt(self):
            original_flush(self)
            with open(self.db_path, "r+b") as f:
                f.truncate(10)

        monkeypatch.setattr(
            chunk_store_mod.ChunkStore, "flush_durable", _flush_then_corrupt
        )

        with pytest.raises(ConsolidationDurabilityError):
            consolidate_collection_in_place(tmp_path)

        assert not (tmp_path / "chunks.db").exists(), (
            "Bug: a bad/corrupt chunks.db was left on disk after a "
            "durability failure -- a subsequent retry would trip over "
            "it instead of starting clean."
        )

    def test_legacy_deletion_is_conditioned_on_the_integrity_check_passing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Ordering proof (via observable behavior only, no mocking of
        the SUT's decision logic): when the fresh-connection integrity
        check FAILS, the legacy source is provably still present -- i.e.
        deletion never races ahead of / happens before the check.
        Combined with the clean-run test below (which proves deletion
        DOES eventually happen once the check passes), this establishes
        that deletion is strictly conditioned on the check's outcome,
        never merely coincidental with it."""
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "zz003333", [1.0, 2.0, 3.0, 4.0], chunk_text="y"
        )

        import code_indexer.storage.sqlite_chunk_store as chunk_store_mod

        original_flush = chunk_store_mod.ChunkStore.flush_durable

        def _flush_then_corrupt(self):
            original_flush(self)
            with open(self.db_path, "r+b") as f:
                f.truncate(15)

        monkeypatch.setattr(
            chunk_store_mod.ChunkStore, "flush_durable", _flush_then_corrupt
        )

        with pytest.raises(ConsolidationDurabilityError):
            consolidate_collection_in_place(tmp_path)

        assert vfile.exists(), (
            "Bug: legacy was deleted even though the fresh-connection "
            "integrity check (run strictly before any deletion) failed."
        )
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.SHARDED_JSON

    def test_clean_run_still_succeeds_end_to_end(self, tmp_path: Path) -> None:
        """The new durability/integrity gate must not break the ordinary
        successful path -- legacy IS deleted once the check passes."""
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "zz004444", [5.0, 6.0, 7.0, 8.0], chunk_text="z"
        )

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
        assert result.records_written == 1
        assert not vfile.exists()
        with ChunkStore(tmp_path / "chunks.db") as store:
            assert store.count() == 1
            record = store.read("zz004444")
        assert record["chunk_text"] == "z"
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.CHUNKS_DB


class TestPreExistingCorruptLeftoverChunksDbFreshPath:
    """Fix A item 4 (fresh path): a leftover corrupt chunks.db from an
    earlier interrupted attempt (discriminator never flipped -- legacy
    is guaranteed fully intact) must be discarded and rebuilt, never
    crash the migration."""

    def test_corrupt_leftover_discarded_and_rebuilt_successfully(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(
            tmp_path, "zz005555", [1.0, 2.0, 3.0, 4.0], chunk_text="rebuilt"
        )
        # A corrupt leftover chunks.db from an earlier interrupted
        # attempt -- discriminator was never flipped.
        (tmp_path / "chunks.db").write_bytes(b"not a valid sqlite file at all")
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.SHARDED_JSON

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
        with ChunkStore(tmp_path / "chunks.db") as store:
            record = store.read("zz005555")
        assert record["chunk_text"] == "rebuilt"
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.CHUNKS_DB


class TestResumePathUnopenableChunksDb:
    """Fix A item 4 (resume path): chunks.db exists (discriminator set)
    but cannot be opened/queried at all -- distinguish recoverable
    (legacy still fully present) from unrecoverable (legacy gone)."""

    def test_recoverable_when_every_record_legacy_source_still_present(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "zz006666", [1.0, 2.0, 3.0, 4.0], chunk_text="recoverable"
        )
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        record = json.loads(vfile.read_text())
        _write_manifest_for_records(tmp_path, {"zz006666": record})
        write_chunks_db_discriminator(tmp_path)
        # chunks.db is corrupt/unopenable, but legacy source is STILL on
        # disk for every manifested record.
        (tmp_path / "chunks.db").write_bytes(b"garbage, not a real sqlite db")

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "already_consolidated"
        assert not vfile.exists()
        with ChunkStore(tmp_path / "chunks.db", immutable=True) as store:
            rebuilt = store.read("zz006666")
        assert rebuilt is not None
        assert rebuilt["chunk_text"] == "recoverable"

    def test_unrecoverable_when_legacy_source_is_gone(self, tmp_path: Path) -> None:
        """The confirmed real-world incident: chunks.db is corrupt, the
        discriminator is set, and the legacy source is ALREADY GONE --
        this must be a distinct, non-retryable terminal error, never a
        plain ConsolidationVerificationError that the scheduler would
        treat as an ordinary retryable failure."""
        _write_collection_meta(tmp_path)
        # No legacy vector_*.json at all -- it was already deleted by a
        # prior, seemingly-successful migration pass.
        _write_manifest_for_records(
            tmp_path,
            {
                "zz007777": {
                    "id": "zz007777",
                    "vector": [1.0, 2.0, 3.0, 4.0],
                    "chunk_text": "irretrievably lost",
                }
            },
        )
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        write_chunks_db_discriminator(tmp_path)
        (tmp_path / "chunks.db").write_bytes(b"corrupted beyond repair")

        with pytest.raises(UnrecoverableConsolidationCorruptionError):
            consolidate_collection_in_place(tmp_path)

    def test_unrecoverable_when_no_manifest_present_at_all(
        self, tmp_path: Path
    ) -> None:
        """Fail closed: without a manifest, recoverability can never be
        PROVEN, even if legacy happens to be present for everything we
        can currently see -- never guess."""
        _write_collection_meta(tmp_path)
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        write_chunks_db_discriminator(tmp_path)
        (tmp_path / "chunks.db").write_bytes(b"corrupted, no manifest exists")

        with pytest.raises(UnrecoverableConsolidationCorruptionError):
            consolidate_collection_in_place(tmp_path)

    def test_unrecoverable_error_is_not_a_consolidation_verification_error(
        self,
    ) -> None:
        """Callers (FleetMigrationScheduler) must be able to distinguish
        this terminal state from an ordinary retryable verification
        failure via except-clause specificity."""
        from code_indexer.storage.shared.collection_migration import (
            ConsolidationVerificationError,
        )

        assert not issubclass(
            UnrecoverableConsolidationCorruptionError, ConsolidationVerificationError
        )

    def test_durability_error_is_a_consolidation_verification_error_subclass(
        self,
    ) -> None:
        """ConsolidationDurabilityError IS a retryable verification
        failure (legacy untouched) -- existing callers that catch
        ConsolidationVerificationError keep working unchanged."""
        from code_indexer.storage.shared.collection_migration import (
            ConsolidationVerificationError,
        )

        assert issubclass(ConsolidationDurabilityError, ConsolidationVerificationError)


def _write_real_chunks_db(chunks_db_path, records: list) -> None:
    """Write real records into a chunks.db via the REAL ChunkStore write
    path (not hand-constructed bytes) -- used to build a big-enough file
    (multiple SQLite pages) that a targeted byte-flip can corrupt an
    INNER data page while leaving the header/schema openable."""
    from code_indexer.storage.sqlite_chunk_store import ChunkStore as _CS

    with _CS(chunks_db_path) as store:
        store.write_batch(records)


def _flip_bytes_at_midpoint(path, span: int = 200) -> None:
    """Corrupt bytes at the file's midpoint -- empirically confirmed
    (real sqlite3, real ChunkStore-written file) to produce a database
    that still OPENS fine and even answers simple queries (e.g.
    ``SELECT COUNT(*)``) with a plausible-looking result, while
    ``PRAGMA integrity_check`` genuinely detects the corruption -- the
    exact "subtly corrupt but openable" scenario Bug #1486 Critical
    Finding 1 requires."""
    size = path.stat().st_size
    with open(path, "r+b") as f:
        f.seek(size // 2)
        data = f.read(span)
        f.seek(size // 2)
        f.write(bytes(b ^ 0xFF for b in data))


class TestCriticalFinding1SubtlyCorruptButOpenable:
    """Bug #1486 Critical Finding 1 (dual review): the resume-path
    cleanup decision must run the SAME mandatory durability+integrity
    gate as the fresh path -- including detecting a chunks.db that OPENS
    fine and even answers simple queries plausibly, but fails a fresh-
    connection PRAGMA integrity_check (a real, empirically-reproduced
    SQLite corruption class, not a hypothetical)."""

    def test_subtly_corrupt_openable_db_with_partial_legacy_is_never_deleted(
        self, tmp_path: Path
    ) -> None:
        """A discriminator-committed collection with (a) a chunks.db
        that opens fine and even returns a plausible row count, but
        fails PRAGMA integrity_check, and (b) legacy PARTIALLY remaining
        (record A's legacy already cleaned up, record B's legacy still
        present -- both tracked in chunks.db AND the manifest) -- must
        classify as UNRECOVERABLE (record A's only surviving source,
        chunks.db, is untrustworthy) and must NEVER delete record B's
        still-present legacy file."""
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        _write_collection_meta(tmp_path)
        # Record A: legacy already cleaned up (simulating a completed
        # prior migration) -- its ONLY source is chunks.db.
        record_a = {
            "id": "aaaa0001",
            "vector": [0.1] * 32,
            "payload": {"path": "src/a.py", "content": "a" * 500},
            "chunk_text": "content-a",
        }
        # Record B: legacy STILL present (a crash mid-cleanup left it) --
        # tracked identically in both chunks.db and the manifest.
        vfile_b = _write_vector_json(
            tmp_path, "bbbb0002", [0.2] * 32, chunk_text="content-b"
        )
        record_b = json.loads(vfile_b.read_text())
        # Padding records purely to grow the file past several SQLite
        # pages, so the midpoint byte-flip lands in a data page, not the
        # header/schema (page 1). These have no legacy (already
        # migrated + cleaned up), matching a real fully-migrated repo.
        padding = [
            {
                "id": f"pad{i:05d}",
                "vector": [0.3] * 32,
                "payload": {"path": f"src/pad{i}.py", "content": "p" * 500},
                "chunk_text": "z" * 500,
            }
            for i in range(200)
        ]

        chunks_db_path = tmp_path / "chunks.db"
        all_records = [record_a, record_b] + padding
        _write_real_chunks_db(chunks_db_path, all_records)
        _write_manifest_for_records(tmp_path, {r["id"]: r for r in all_records})
        write_chunks_db_discriminator(tmp_path)

        # Sanity: confirm the corruption technique produces an OPENABLE
        # db with a plausible-looking count, before even invoking the
        # SUT -- proves this is genuinely the "subtly corrupt but
        # openable" class, not a trivially-unopenable file.
        _flip_bytes_at_midpoint(chunks_db_path)
        import sqlite3 as _sqlite3

        probe_conn = _sqlite3.connect(str(chunks_db_path))
        try:
            probe_count = probe_conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        finally:
            probe_conn.close()
        assert probe_count == (len(all_records),), (
            "Fixture invariant violated: the corrupted file must still "
            "open and answer a plausible COUNT query for this to be a "
            "genuine 'subtly corrupt but openable' repro."
        )

        with pytest.raises(UnrecoverableConsolidationCorruptionError):
            consolidate_collection_in_place(tmp_path)

        assert vfile_b.exists(), (
            "Bug: the still-present legacy file was deleted despite "
            "chunks.db failing the mandatory durability/integrity gate "
            "-- the resume path bypassed the gate that the fresh path "
            "already enforces."
        )


class TestManifestCrossCheckIncident:
    """Bug #1486 Critical Finding 2 (dual review): confirmed real
    incident -- a genuinely corrupt chunks.db + committed discriminator
    + no legacy + an empty ``{}`` manifest returned "already_
    consolidated", replacing the corrupt db with an empty ~24 KiB one --
    permanent silent data loss. An incomplete/empty/tampered-but-
    internally-self-consistent manifest must never silently authorize
    that."""

    def test_empty_manifest_with_vector_count_cross_check_mismatch_is_unrecoverable(
        self, tmp_path: Path
    ) -> None:
        """The EXACT confirmed incident, reproduced faithfully: a
        collection that genuinely held real data (vector_count=5,
        durably recorded in collection_meta.json by THIS module's own
        write path) now has a corrupt, unopenable chunks.db, ZERO
        remaining legacy, and an EMPTY (internally self-consistent)
        manifest -- must raise loudly, never silently rebuild to an
        empty store and report success."""
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        _write_collection_meta(tmp_path)
        meta_path = tmp_path / "collection_meta.json"
        meta = json.loads(meta_path.read_text())
        meta["vector_count"] = 5
        meta_path.write_text(json.dumps(meta))

        write_chunks_db_discriminator(tmp_path)
        (tmp_path / "chunks.db").write_bytes(b"corrupted beyond repair")
        # The exact repro: a syntactically-valid, internally self-
        # consistent EMPTY manifest -- old code accepted this silently.
        (tmp_path / "chunks_db_content_manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "records": {},
                    "expected_count": 0,
                    "root_digest": "0" * 64,
                }
            )
        )

        with pytest.raises(UnrecoverableConsolidationCorruptionError):
            consolidate_collection_in_place(tmp_path)

        # The corrupt chunks.db must NOT have been silently replaced
        # with a fresh empty one.
        with open(tmp_path / "chunks.db", "rb") as f:
            assert f.read() == b"corrupted beyond repair", (
                "Bug: the corrupt chunks.db was silently replaced with "
                "an empty, 'successfully verified' one."
            )


class TestManifestStructuralValidation:
    """Bug #1486 Critical Finding 2: structural self-validation of the
    versioned manifest envelope, independent of the cross-check field."""

    def test_manifest_missing_required_envelope_keys_is_unrecoverable(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        _write_collection_meta(tmp_path)
        write_chunks_db_discriminator(tmp_path)
        (tmp_path / "chunks.db").write_bytes(b"corrupted beyond repair")
        # Missing "expected_count"/"root_digest" -- an old-format flat
        # manifest, or any malformed envelope.
        (tmp_path / "chunks_db_content_manifest.json").write_text(
            json.dumps({"some_id": "some_digest"})
        )

        with pytest.raises(UnrecoverableConsolidationCorruptionError):
            consolidate_collection_in_place(tmp_path)

    def test_manifest_with_tampered_root_digest_is_unrecoverable(
        self, tmp_path: Path
    ) -> None:
        """Self-consistency alone (expected_count == len(records)) is
        NOT sufficient -- the whole-key-set root digest must also match,
        catching a manifest whose records were tampered/truncated in a
        way that preserved the count but not the actual content."""
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        _write_collection_meta(tmp_path)
        write_chunks_db_discriminator(tmp_path)
        (tmp_path / "chunks.db").write_bytes(b"corrupted beyond repair")
        (tmp_path / "chunks_db_content_manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "records": {"ghost0001": "deadbeef" * 8},
                    "expected_count": 1,
                    # Deliberately wrong -- does not match a recomputed
                    # fold of the "records" entry above.
                    "root_digest": "0" * 64,
                }
            )
        )

        with pytest.raises(UnrecoverableConsolidationCorruptionError):
            consolidate_collection_in_place(tmp_path)


class TestManifestValidationDuringRecoverableRebuild:
    """Bug #1486 Critical Finding 2: manifest validation must run even
    on the recoverable-rebuild path (never bypassed just because legacy
    happens to still be present), and the cross-check field this whole
    fix depends on must actually be produced by real fresh
    consolidation."""

    def test_recoverable_rebuild_still_enforces_manifest_self_validation(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        _write_collection_meta(tmp_path)
        _write_vector_json(
            tmp_path, "cccc0003", [1.0, 2.0, 3.0, 4.0], chunk_text="recoverable"
        )
        write_chunks_db_discriminator(tmp_path)
        (tmp_path / "chunks.db").write_bytes(b"corrupted beyond repair")
        # Malformed/incomplete manifest -- must be rejected even though
        # legacy is fully present for this specific record.
        (tmp_path / "chunks_db_content_manifest.json").write_text("{}")

        with pytest.raises(UnrecoverableConsolidationCorruptionError):
            consolidate_collection_in_place(tmp_path)

    def test_fresh_consolidation_writes_authoritative_vector_count(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        for i in range(3):
            _write_vector_json(
                tmp_path, f"dddd000{i}", [1.0, 2.0, 3.0, 4.0], chunk_text=f"x{i}"
            )

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
        meta = json.loads((tmp_path / "collection_meta.json").read_text())
        assert meta.get("vector_count") == 3, (
            "Bug: fresh consolidation did not durably record the "
            "authoritative vector_count cross-check field."
        )


class TestSameIdContentDivergenceRegression:
    """Regression proof under the NEW manifest schema: existing
    rebuild-from-legacy behavior for content divergence must survive
    the manifest-format change unchanged."""

    def test_same_id_legacy_content_differs_from_manifest_triggers_rebuild(
        self, tmp_path: Path
    ) -> None:
        """A still-present legacy record whose content genuinely
        differs from what chunks.db/manifest currently reflect
        (corruption in chunks.db, not in legacy) must still trigger a
        correct rebuild-from-legacy, exactly as before."""
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )
        from code_indexer.storage.sqlite_chunk_store import ChunkStore as _CS

        _write_collection_meta(tmp_path)
        legacy = _write_vector_json(
            tmp_path, "eeee0004", [1.0, 2.0, 3.0, 4.0], chunk_text="correct-content"
        )
        legacy_record = json.loads(legacy.read_text())

        # chunks.db has the id, but with CORRUPTED content relative to
        # the still-present legacy source.
        corrupted_stored = dict(legacy_record)
        corrupted_stored["chunk_text"] = "WRONG-CONTENT"
        db_path = tmp_path / "chunks.db"
        with _CS(db_path) as store:
            store.write_batch([corrupted_stored])

        _write_manifest_for_records(tmp_path, {"eeee0004": legacy_record})
        write_chunks_db_discriminator(tmp_path)

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "already_consolidated"
        assert not legacy.exists()
        with ChunkStore(db_path, immutable=True) as store:
            rebuilt = store.read("eeee0004")
        assert rebuilt["chunk_text"] == "correct-content"


class TestFinalGateFlushBeforeIntegrityCheckOrdering:
    """Bug #1486 Round 3 Finding A (CRITICAL): the RESUME-path final
    durability gate in _verify_chunks_db_before_resume_cleanup() ran its
    fresh-connection PRAGMA integrity_check BEFORE flush_durable()'s
    explicit fsync -- validating pre-fsync/cached state instead of what
    is actually durable on the backing store. A corruption that only
    manifests AFTER the durable flush (the exact NFS close-to-open
    scenario this whole bug is about) was therefore invisible to this
    final gate, and the caller went on to authorize deleting the
    irreplaceable legacy source over a chunks.db that was never actually
    re-verified post-flush."""

    def test_corruption_revealed_only_after_flush_is_never_authorized_for_cleanup(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        _write_collection_meta(tmp_path)
        # A committed, mixed-layout collection -- exactly the real
        # production resume state: chunks.db already has the record AND
        # the legacy vector_*.json is still present on disk (e.g. a
        # crash between the durable flip and cleanup completing).
        vfile = _write_vector_json(
            tmp_path, "ff010101", [1.0, 2.0, 3.0, 4.0], chunk_text="mixed-layout"
        )
        record = json.loads(vfile.read_text())
        chunks_db_path = tmp_path / "chunks.db"
        with ChunkStore(chunks_db_path) as store:
            store.write_batch([record])
        _write_manifest_for_records(tmp_path, {"ff010101": record})
        # A real fresh-consolidation always writes the authoritative
        # vector_count alongside the manifest -- match that here so this
        # test exercises ONLY the ordering bug, not an unrelated
        # missing-cross-check-field condition.
        _write_authoritative_vector_count(tmp_path, 1)
        write_chunks_db_discriminator(tmp_path)

        import code_indexer.storage.sqlite_chunk_store as chunk_store_mod

        original_flush = chunk_store_mod.ChunkStore.flush_durable

        def _flush_then_corrupt(self):
            original_flush(self)
            # Simulate corruption that only manifests once the write is
            # forced durable (e.g. an NFS close-to-open race) --
            # UNCONDITIONALLY, on every flush_durable() call (including
            # any repair attempt's own re-flush), so a fix that merely
            # swaps the check-then-flush order into flush-then-check but
            # still trusts a lucky one-shot repair cannot mask the bug.
            with open(self.db_path, "r+b") as f:
                f.truncate(20)

        monkeypatch.setattr(
            chunk_store_mod.ChunkStore, "flush_durable", _flush_then_corrupt
        )

        with pytest.raises(ConsolidationVerificationError):
            consolidate_collection_in_place(tmp_path)

        assert vfile.exists(), (
            "Bug: the legacy source was deleted even though chunks.db's "
            "post-flush fresh-connection integrity check was never "
            "actually run before authorizing cleanup."
        )


class TestFinding2MissingAuthoritativeCountFailsClosed:
    """Bug #1486 Round 3 Finding B (CRITICAL): a MISSING (never merely
    mismatched) authoritative vector_count on a discriminator-committed
    collection whose chunks.db has ALREADY failed its durability/
    integrity gate must also fail closed -- the pre-fix code silently
    SKIPPED the cross-check entirely when the field was absent, so a
    self-consistent-but-entirely-wrong manifest (e.g. a genuinely empty
    envelope) was accepted with nothing independent to contradict it,
    silently rebuilding to an empty chunks.db and reporting success."""

    def test_missing_vector_count_with_valid_empty_envelope_is_unrecoverable(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        _write_collection_meta(tmp_path)  # deliberately NO vector_count key
        write_chunks_db_discriminator(tmp_path)
        (tmp_path / "chunks.db").write_bytes(b"corrupted beyond repair")
        # A self-consistent, structurally-valid EMPTY envelope manifest --
        # internally correct, but with nothing independent to cross-check
        # it against since vector_count was never recorded.
        (tmp_path / "chunks_db_content_manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "records": {},
                    "expected_count": 0,
                    "root_digest": "0" * 64,
                }
            )
        )

        with pytest.raises(UnrecoverableConsolidationCorruptionError):
            consolidate_collection_in_place(tmp_path)

        with open(tmp_path / "chunks.db", "rb") as f:
            assert f.read() == b"corrupted beyond repair", (
                "Bug: the corrupt chunks.db was silently replaced with a "
                "fresh empty one because the missing (not merely "
                "mismatched) vector_count field was treated as 'nothing "
                "to cross-check against' instead of failing closed."
            )


class TestFinding4LegacyFlatManifestUpgrade:
    """Bug #1486 Round 3 Finding D (HIGH), coupled with Finding B: a
    genuinely healthy round-1 collection (chunks.db populated and
    passing integrity, discriminator committed, legacy already fully
    cleaned up) whose content manifest is the OLD pre-envelope FLAT
    {point_id: digest} shape (round-1 never wrote vector_count either)
    must be recognized as a clean, already-migrated no-op -- upgraded to
    the new self-validating envelope + authoritative vector_count --
    never permanently misclassified as unrecoverable corruption."""

    def test_healthy_round1_flat_manifest_is_upgraded_not_unrecoverable(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )
        from code_indexer.storage.shared.collection_migration import (
            _compute_record_content_digest,
        )

        _write_collection_meta(tmp_path)  # no vector_count -- round-1 never wrote it
        record_a = {
            "id": "r1aaaa01",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "payload": {"path": "src/a.py"},
            "chunk_text": "content-a",
        }
        record_b = {
            "id": "r1bbbb02",
            "vector": [0.5, 0.6, 0.7, 0.8],
            "payload": {"path": "src/b.py"},
            "chunk_text": "content-b",
        }
        chunks_db_path = tmp_path / "chunks.db"
        _write_real_chunks_db(chunks_db_path, [record_a, record_b])

        with ChunkStore(chunks_db_path, immutable=True) as store:
            stored_a = store.read("r1aaaa01")
            stored_b = store.read("r1bbbb02")
        legacy_flat_manifest = {
            "r1aaaa01": _compute_record_content_digest(stored_a),
            "r1bbbb02": _compute_record_content_digest(stored_b),
        }
        (tmp_path / "chunks_db_content_manifest.json").write_text(
            json.dumps(legacy_flat_manifest)
        )
        write_chunks_db_discriminator(tmp_path)
        # No legacy vector_*.json at all -- round-1 already fully
        # cleaned up before round-2's manifest mechanism ever existed.

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "already_consolidated"
        assert result.old_files_deleted == 0

        # The manifest must have been durably upgraded to the new
        # self-validating envelope, and the authoritative vector_count
        # cross-check field must now be recorded.
        upgraded = json.loads(
            (tmp_path / "chunks_db_content_manifest.json").read_text()
        )
        assert upgraded["version"] == 1
        assert set(upgraded["records"].keys()) == {"r1aaaa01", "r1bbbb02"}
        assert upgraded["expected_count"] == 2

        meta = json.loads((tmp_path / "collection_meta.json").read_text())
        assert meta.get("vector_count") == 2

        # chunks.db itself must be completely untouched (still resolves
        # CHUNKS_DB, still has both original records, unmodified).
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.CHUNKS_DB
        with ChunkStore(chunks_db_path, immutable=True) as store:
            assert store.read("r1aaaa01")["chunk_text"] == "content-a"
            assert store.read("r1bbbb02")["chunk_text"] == "content-b"

    def test_flat_manifest_not_matching_healthy_chunks_db_stays_unrecoverable(
        self, tmp_path: Path
    ) -> None:
        """Fail-closed proof: a flat manifest whose claimed digest does
        NOT match the healthy chunks.db's actual content must NOT be
        upgraded/trusted -- it must fall through to the same structural
        failure as before."""
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        _write_collection_meta(tmp_path)
        record = {
            "id": "r2aaaa01",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "payload": {"path": "src/a.py"},
            "chunk_text": "content-a",
        }
        chunks_db_path = tmp_path / "chunks.db"
        _write_real_chunks_db(chunks_db_path, [record])
        # A flat manifest with a WRONG digest for the one real record.
        (tmp_path / "chunks_db_content_manifest.json").write_text(
            json.dumps({"r2aaaa01": "0" * 64})
        )
        write_chunks_db_discriminator(tmp_path)

        with pytest.raises(UnrecoverableConsolidationCorruptionError):
            consolidate_collection_in_place(tmp_path)


class TestFinding3ReadOnlyOracleNoMutation:
    """Bug #1486 Codex Finding #3 (HIGH): verify_collection_fully_migrated
    is documented and relied upon (get_stats(), is_repo_already_migrated(),
    scheduler done-detection) as a SIDE-EFFECT-FREE, pure read-only
    predicate -- and it runs WITHOUT holding the repo write lock. It must
    therefore NEVER perform the legacy-flat-manifest UPGRADE write: doing
    so both violates its read-only contract and opens a cross-node TOCTOU
    (a concurrent writer adding a row between the upgrade's validate and
    its manifest/vector_count rewrite would enshrine an undercounted-but-
    self-consistent envelope, defeating the Finding-B cross-check on a
    later corruption). The upgrade WRITE is legitimate only on the genuine
    migration path (consolidate_collection_in_place), which holds the
    cluster-wide write lock."""

    def test_read_only_oracle_does_not_upgrade_flat_manifest(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )
        from code_indexer.storage.shared.collection_migration import (
            _compute_record_content_digest,
            verify_collection_fully_migrated,
        )

        _write_collection_meta(tmp_path)  # no vector_count (round-1 shape)
        record_a = {
            "id": "r3aaaa01",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "payload": {"path": "src/a.py"},
            "chunk_text": "content-a",
        }
        record_b = {
            "id": "r3bbbb02",
            "vector": [0.5, 0.6, 0.7, 0.8],
            "payload": {"path": "src/b.py"},
            "chunk_text": "content-b",
        }
        chunks_db_path = tmp_path / "chunks.db"
        _write_real_chunks_db(chunks_db_path, [record_a, record_b])

        with ChunkStore(chunks_db_path, immutable=True) as store:
            stored_a = store.read("r3aaaa01")
            stored_b = store.read("r3bbbb02")
        legacy_flat_manifest = {
            "r3aaaa01": _compute_record_content_digest(stored_a),
            "r3bbbb02": _compute_record_content_digest(stored_b),
        }
        manifest_path = tmp_path / "chunks_db_content_manifest.json"
        manifest_path.write_text(json.dumps(legacy_flat_manifest))
        write_chunks_db_discriminator(tmp_path)
        # No legacy vector_*.json -- genuinely already migrated & clean.

        # The read-only predicate must recognize this as fully migrated...
        assert verify_collection_fully_migrated(tmp_path) is True

        # ...WITHOUT mutating anything. The manifest must remain the
        # original FLAT shape (never rewritten to the envelope), and no
        # authoritative vector_count may have been written by this
        # side-effect-free call.
        after = json.loads(manifest_path.read_text())
        assert after == legacy_flat_manifest
        assert "version" not in after
        assert "records" not in after
        meta = json.loads((tmp_path / "collection_meta.json").read_text())
        assert "vector_count" not in meta

    def test_migration_path_still_upgrades_flat_manifest_under_lock(
        self, tmp_path: Path
    ) -> None:
        """Companion: the genuine migration WRITE path
        (consolidate_collection_in_place, which holds the write lock) MUST
        still upgrade the flat manifest -- the read-only gate narrows ONLY
        the side-effect-free predicate, never the real migration."""
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )
        from code_indexer.storage.shared.collection_migration import (
            _compute_record_content_digest,
        )

        _write_collection_meta(tmp_path)
        record = {
            "id": "r3cccc03",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "payload": {"path": "src/c.py"},
            "chunk_text": "content-c",
        }
        chunks_db_path = tmp_path / "chunks.db"
        _write_real_chunks_db(chunks_db_path, [record])
        with ChunkStore(chunks_db_path, immutable=True) as store:
            stored = store.read("r3cccc03")
        manifest_path = tmp_path / "chunks_db_content_manifest.json"
        manifest_path.write_text(
            json.dumps({"r3cccc03": _compute_record_content_digest(stored)})
        )
        write_chunks_db_discriminator(tmp_path)

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "already_consolidated"
        upgraded = json.loads(manifest_path.read_text())
        assert upgraded["version"] == 1
        assert set(upgraded["records"].keys()) == {"r3cccc03"}
        meta = json.loads((tmp_path / "collection_meta.json").read_text())
        assert meta.get("vector_count") == 1
