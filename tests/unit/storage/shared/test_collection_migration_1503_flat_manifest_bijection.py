"""Issue #1503: fail-closed SUBSET upgrade path for content-integrity
manifests (both the legacy pre-envelope FLAT `{point_id: digest}` shape
and a modern ENVELOPE manifest) that are a STALE SUBSET of chunks.db's
real row set -- the routine, healthy "an ordinary refresh added rows
after the manifest was last written" case -- while still hard-refusing
any manifest that claims a key chunks.db does not actually have, or
whose claimed digest for a covered entry does not match.

Bug #1486 Round 3 Finding D originally required an EXACT bijection
between a legacy flat manifest and chunks.db's real `all_point_ids()`.
Issue #1503 (confirmed live on staging) proved that requirement too
strict: a fully-migrated, fully-cleaned-up collection whose manifest
was never regenerated after a later ordinary `cidx index` refresh added
new rows was being permanently branded UNRECOVERABLE, even though every
manifested row was still perfectly correct. The fix loosens the
bijection check to a SUBSET check (manifest keys subset-of chunks.db's
real ids, every covered digest correct) -- both for the legacy flat
shape and for an already-envelope-shaped-but-stale manifest, via a
generalized subset-upgrade primitive wired into `_load_verified_manifest`.

The two genuine hard-refusal directions are unchanged and still
verified here:
  (a) the manifest has an EXTRA/PHANTOM key chunks.db does NOT have --
      either the manifest is lying, or a row was silently deleted.
  (b) ANY digest mismatch on an entry present in both -- genuine
      corruption/tampering, never tolerated.

All tests use REAL files and REAL SQLite (via the real ChunkStore) --
no mocking of the code under test.

Dual-review follow-up (Claude code-reviewer + independent Codex) added
two more test classes:
  - TestContradictoryReadOnlyAllowUpgradeFlags (Codex CRITICAL Finding
    1): the write gate must never silently resolve a contradictory
    read_only=True + allow_manifest_upgrade=True combination -- it must
    fail loudly instead.
  - TestManifestUpgradeWriteFailurePropagates (Codex HIGH #7 + Claude
    MEDIUM, independently converged): a genuine write/IO failure during
    the subset-upgrade's manifest-rewrite step must propagate as its
    own distinct, retryable error -- never silently converted into
    re-raising the original UnrecoverableConsolidationCorruptionError.
"""

import json
import os
from pathlib import Path

import pytest

from code_indexer.storage.shared.chunk_layout import (
    write_chunks_db_discriminator,
)
from code_indexer.storage.shared.collection_migration import (
    ConsolidationVerificationError,
    UnrecoverableConsolidationCorruptionError,
    _compute_record_content_digest,
    _load_verified_manifest,
    _validate_manifest_envelope,
    _verify_manifest_root_digest,
    _write_authoritative_vector_count,
    consolidate_collection_in_place,
    verify_collection_fully_migrated,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore
from tests.unit.storage.shared.test_collection_migration_1458 import (
    _write_collection_meta,
    _write_manifest_for_records,
)


def _write_real_chunks_db(chunks_db_path: Path, records: list) -> None:
    with ChunkStore(chunks_db_path) as store:
        store.write_batch(records)


def _build_two_record_collection(tmp_path: Path) -> "tuple[Path, dict, dict]":
    """A genuinely healthy, discriminator-committed, fully-cleaned-up
    (zero legacy files) CHUNKS_DB collection with two real records --
    matches the confirmed staging shape (chunks.db integrity=ok, 0 legacy
    files, valid discriminator)."""
    _write_collection_meta(tmp_path)  # round-1 shape: no vector_count
    record_a = {
        "id": "bija0001",
        "vector": [0.1, 0.2, 0.3, 0.4],
        "payload": {"path": "src/a.py"},
        "chunk_text": "content-a",
    }
    record_b = {
        "id": "bijb0002",
        "vector": [0.5, 0.6, 0.7, 0.8],
        "payload": {"path": "src/b.py"},
        "chunk_text": "content-b",
    }
    chunks_db_path = tmp_path / "chunks.db"
    _write_real_chunks_db(chunks_db_path, [record_a, record_b])
    write_chunks_db_discriminator(tmp_path)
    return chunks_db_path, record_a, record_b


class TestBijectionTruncatedFlatManifest:
    """Direction (a) corrected per Issue #1503: a flat manifest MISSING
    an entry chunks.db actually has is the routine "ordinary refresh
    added rows after the manifest was last written" case, and is now
    ACCEPTED + the manifest is REGENERATED to cover the full live set --
    the opposite of the pre-#1503 permanent-refusal behavior."""

    def test_truncated_flat_manifest_missing_entry_is_accepted_and_regenerated(
        self, tmp_path: Path
    ) -> None:
        chunks_db_path, record_a, record_b = _build_two_record_collection(tmp_path)

        with ChunkStore(chunks_db_path, immutable=True) as store:
            stored_a = store.read("bija0001")
        # Deliberately omit record_b's entry -- a STALE (not truncated-
        # corrupt) flat manifest: record_b was added to chunks.db by an
        # ordinary refresh after the manifest was last written.
        stale_manifest = {"bija0001": _compute_record_content_digest(stored_a)}
        manifest_path = tmp_path / "chunks_db_content_manifest.json"
        manifest_path.write_text(json.dumps(stale_manifest))

        before_chunks_db_bytes = chunks_db_path.read_bytes()

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "already_consolidated"
        assert result.old_files_deleted == 0

        # chunks.db itself is untouched by a pure manifest-regeneration.
        assert chunks_db_path.read_bytes() == before_chunks_db_bytes

        # The manifest on disk must have been REWRITTEN to cover BOTH
        # live rows, not just bija0001 -- closing the staleness
        # permanently rather than rediscovering it on every future call.
        regenerated_raw = json.loads(manifest_path.read_text())
        assert set(regenerated_raw["records"].keys()) == {"bija0001", "bijb0002"}
        assert regenerated_raw["expected_count"] == 2

        # The regenerated manifest must independently re-validate via the
        # UNMODIFIED envelope validator + root-digest check -- a
        # genuinely well-formed envelope, not a special-cased bypass.
        records, expected_count, root_digest = _validate_manifest_envelope(
            tmp_path, regenerated_raw
        )
        _verify_manifest_root_digest(tmp_path, records, root_digest)
        assert expected_count == 2

        meta = json.loads((tmp_path / "collection_meta.json").read_text())
        assert meta.get("vector_count") == 2

    def test_verify_collection_fully_migrated_returns_true_for_stale_flat_manifest(
        self, tmp_path: Path
    ) -> None:
        chunks_db_path, record_a, record_b = _build_two_record_collection(tmp_path)

        with ChunkStore(chunks_db_path, immutable=True) as store:
            stored_a = store.read("bija0001")
        stale_manifest = {"bija0001": _compute_record_content_digest(stored_a)}
        manifest_path = tmp_path / "chunks_db_content_manifest.json"
        manifest_path.write_text(json.dumps(stale_manifest))

        before_manifest_bytes = manifest_path.read_bytes()
        before_chunks_db_bytes = chunks_db_path.read_bytes()

        assert verify_collection_fully_migrated(tmp_path) is True

        # Read-only oracle contract: never mutates, even when it accepts.
        assert manifest_path.read_bytes() == before_manifest_bytes
        assert chunks_db_path.read_bytes() == before_chunks_db_bytes
        assert "vector_count" not in (tmp_path / "collection_meta.json").read_text()


class TestBijectionExtraKeyFlatManifest:
    """Direction (b), unchanged by Issue #1503: the manifest has an
    EXTRA/PHANTOM key chunks.db does NOT have -- always a hard refusal,
    never acceptable."""

    def test_extra_key_flat_manifest_stays_unrecoverable(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        record = {
            "id": "extr0001",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "payload": {"path": "src/a.py"},
            "chunk_text": "content-a",
        }
        chunks_db_path = tmp_path / "chunks.db"
        _write_real_chunks_db(chunks_db_path, [record])
        write_chunks_db_discriminator(tmp_path)

        with ChunkStore(chunks_db_path, immutable=True) as store:
            stored = store.read("extr0001")
        # Extra point_id that chunks.db has never seen, with a
        # syntactically-plausible (but meaningless) 64-hex digest value.
        extra_key_manifest = {
            "extr0001": _compute_record_content_digest(stored),
            "ghost0001": "a" * 64,
        }
        manifest_path = tmp_path / "chunks_db_content_manifest.json"
        manifest_path.write_text(json.dumps(extra_key_manifest))

        before_manifest_bytes = manifest_path.read_bytes()
        before_chunks_db_bytes = chunks_db_path.read_bytes()

        with pytest.raises(UnrecoverableConsolidationCorruptionError):
            consolidate_collection_in_place(tmp_path)

        assert manifest_path.read_bytes() == before_manifest_bytes
        assert chunks_db_path.read_bytes() == before_chunks_db_bytes

    def test_verify_collection_fully_migrated_returns_false_for_extra_key_flat_manifest(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        record = {
            "id": "extr0002",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "payload": {"path": "src/a.py"},
            "chunk_text": "content-a",
        }
        chunks_db_path = tmp_path / "chunks.db"
        _write_real_chunks_db(chunks_db_path, [record])
        write_chunks_db_discriminator(tmp_path)

        with ChunkStore(chunks_db_path, immutable=True) as store:
            stored = store.read("extr0002")
        extra_key_manifest = {
            "extr0002": _compute_record_content_digest(stored),
            "ghost0002": "b" * 64,
        }
        manifest_path = tmp_path / "chunks_db_content_manifest.json"
        manifest_path.write_text(json.dumps(extra_key_manifest))

        before_manifest_bytes = manifest_path.read_bytes()
        before_chunks_db_bytes = chunks_db_path.read_bytes()

        assert verify_collection_fully_migrated(tmp_path) is False

        assert manifest_path.read_bytes() == before_manifest_bytes
        assert chunks_db_path.read_bytes() == before_chunks_db_bytes


def _build_stale_subset_envelope_fixture(tmp_path: Path) -> "tuple[Path, dict]":
    """The real Issue #1503 incident shape: a genuine ENVELOPE manifest
    covering 5 records, then an ordinary refresh added 2 more rows
    DIRECTLY to chunks.db (7 live rows total) without ever touching the
    manifest -- and collection_meta.json's independent `vector_count`
    WAS kept in sync by the refresh path (now reads 7), producing the
    confirmed live-staging mismatch against the manifest's own
    `expected_count` (5)."""
    _write_collection_meta(tmp_path)
    original_records = {}
    for i in range(5):
        pid = f"orig000{i}"
        original_records[pid] = {
            "id": pid,
            "vector": [0.1 * i, 0.2, 0.3, 0.4],
            "payload": {"path": f"src/{i}.py"},
            "chunk_text": f"content-{i}",
        }
    chunks_db_path = tmp_path / "chunks.db"
    _write_real_chunks_db(chunks_db_path, list(original_records.values()))
    _write_manifest_for_records(tmp_path, original_records)
    write_chunks_db_discriminator(tmp_path)
    _write_authoritative_vector_count(tmp_path, 5)

    # Ordinary refresh: 2 new rows added directly to chunks.db, manifest
    # deliberately left untouched (that is the point of this scenario).
    extra_records = [
        {
            "id": "new00001",
            "vector": [0.9, 0.8, 0.7, 0.6],
            "payload": {"path": "src/new1.py"},
            "chunk_text": "new-content-1",
        },
        {
            "id": "new00002",
            "vector": [0.5, 0.4, 0.3, 0.2],
            "payload": {"path": "src/new2.py"},
            "chunk_text": "new-content-2",
        },
    ]
    with ChunkStore(chunks_db_path) as store:
        store.write_batch(extra_records)
    # The refresh path keeps collection_meta.json's vector_count in sync
    # with the live chunks.db -- now 7, disagreeing with the manifest's
    # stale expected_count of 5.
    _write_authoritative_vector_count(tmp_path, 7)

    return chunks_db_path, original_records


class TestEnvelopeStaleSubsetManifest:
    """Issue #1503's real remaining bug: an already-ENVELOPE-shaped
    manifest that is a STALE SUBSET of chunks.db's live row set (routine
    refresh added rows after the manifest was last written) must now be
    ACCEPTED + REGENERATED, while a phantom key or a digest mismatch on
    a covered entry must stay a hard refusal."""

    def test_stale_subset_envelope_is_accepted_and_regenerated(
        self, tmp_path: Path
    ) -> None:
        chunks_db_path, _original_records = _build_stale_subset_envelope_fixture(
            tmp_path
        )
        before_chunks_db_bytes = chunks_db_path.read_bytes()

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "already_consolidated"
        assert result.old_files_deleted == 0
        assert chunks_db_path.read_bytes() == before_chunks_db_bytes

        manifest_path = tmp_path / "chunks_db_content_manifest.json"
        regenerated_raw = json.loads(manifest_path.read_text())
        assert set(regenerated_raw["records"].keys()) == {
            "orig0000",
            "orig0001",
            "orig0002",
            "orig0003",
            "orig0004",
            "new00001",
            "new00002",
        }
        assert regenerated_raw["expected_count"] == 7

        records, expected_count, root_digest = _validate_manifest_envelope(
            tmp_path, regenerated_raw
        )
        _verify_manifest_root_digest(tmp_path, records, root_digest)
        assert expected_count == 7

        meta = json.loads((tmp_path / "collection_meta.json").read_text())
        assert meta.get("vector_count") == 7

    def test_read_only_oracle_accepts_stale_subset_without_mutation(
        self, tmp_path: Path
    ) -> None:
        chunks_db_path, _original_records = _build_stale_subset_envelope_fixture(
            tmp_path
        )
        manifest_path = tmp_path / "chunks_db_content_manifest.json"
        meta_path = tmp_path / "collection_meta.json"

        before_manifest_bytes = manifest_path.read_bytes()
        before_chunks_db_bytes = chunks_db_path.read_bytes()
        before_meta_bytes = meta_path.read_bytes()

        assert verify_collection_fully_migrated(tmp_path) is True

        # Read-only oracle: byte-identical before/after, on ALL three files.
        assert manifest_path.read_bytes() == before_manifest_bytes
        assert chunks_db_path.read_bytes() == before_chunks_db_bytes
        assert meta_path.read_bytes() == before_meta_bytes

    def test_stale_subset_with_phantom_key_stays_unrecoverable(
        self, tmp_path: Path
    ) -> None:
        chunks_db_path, original_records = _build_stale_subset_envelope_fixture(
            tmp_path
        )
        # Inject a phantom key into the manifest that chunks.db has never
        # seen -- this must ALWAYS be refused, both before and after the
        # subset-acceptance fix.
        manifest_path = tmp_path / "chunks_db_content_manifest.json"
        raw = json.loads(manifest_path.read_text())
        raw["records"]["ghost0000"] = "a" * 64
        raw["expected_count"] = len(raw["records"])
        manifest_path.write_text(json.dumps(raw))

        before_manifest_bytes = manifest_path.read_bytes()
        before_chunks_db_bytes = chunks_db_path.read_bytes()

        with pytest.raises(UnrecoverableConsolidationCorruptionError):
            consolidate_collection_in_place(tmp_path)

        assert manifest_path.read_bytes() == before_manifest_bytes
        assert chunks_db_path.read_bytes() == before_chunks_db_bytes

    def test_stale_subset_with_wrong_digest_on_covered_entry_stays_unrecoverable(
        self, tmp_path: Path
    ) -> None:
        chunks_db_path, original_records = _build_stale_subset_envelope_fixture(
            tmp_path
        )
        # Corrupt ONE covered entry's digest -- genuine tampering/
        # corruption of an already-migrated row, distinct from mere
        # staleness. Must always be refused.
        manifest_path = tmp_path / "chunks_db_content_manifest.json"
        raw = json.loads(manifest_path.read_text())
        raw["records"]["orig0000"] = "0" * 64
        manifest_path.write_text(json.dumps(raw))

        before_manifest_bytes = manifest_path.read_bytes()
        before_chunks_db_bytes = chunks_db_path.read_bytes()

        with pytest.raises(UnrecoverableConsolidationCorruptionError):
            consolidate_collection_in_place(tmp_path)

        assert manifest_path.read_bytes() == before_manifest_bytes
        assert chunks_db_path.read_bytes() == before_chunks_db_bytes


class TestContradictoryReadOnlyAllowUpgradeFlags:
    """Dual-review Finding 1 (Codex CRITICAL): the manifest-upgrade write
    gate is currently keyed ONLY on ``allow_write``, ignoring
    ``read_only`` entirely. A hypothetical future caller that passed
    BOTH ``read_only=True`` (a read-only-contract declaration) AND
    ``allow_manifest_upgrade=True``/``allow_write=True`` (a write
    permission grant) simultaneously must never have that contradiction
    silently resolved either way -- it must fail loudly instead
    (Messi Rule #13 anti-silent-failure)."""

    def test_load_verified_manifest_rejects_contradictory_flags(
        self, tmp_path: Path
    ) -> None:
        chunks_db_path, record_a, record_b = _build_two_record_collection(tmp_path)
        manifest_path = tmp_path / "chunks_db_content_manifest.json"
        meta_path = tmp_path / "collection_meta.json"

        before_manifest_exists = manifest_path.exists()
        before_chunks_db_bytes = chunks_db_path.read_bytes()
        before_meta_bytes = meta_path.read_bytes()

        with pytest.raises(ValueError, match="read_only.*allow_manifest_upgrade"):
            _load_verified_manifest(
                tmp_path,
                chunks_db_path,
                require_authoritative_count=False,
                allow_manifest_upgrade=True,
                read_only=True,
            )

        # Fails BEFORE doing any work at all -- no manifest is created,
        # chunks.db and collection_meta.json are untouched.
        assert manifest_path.exists() == before_manifest_exists
        assert chunks_db_path.read_bytes() == before_chunks_db_bytes
        assert meta_path.read_bytes() == before_meta_bytes

    def test_non_contradictory_flags_still_work_normally(self, tmp_path: Path) -> None:
        """Regression guard: the new contradiction guard must not affect
        any legitimate, non-contradictory flag combination -- e.g. the
        genuine migration path's default (allow_manifest_upgrade=True,
        read_only=False) on a perfectly healthy, already-consistent
        manifest."""
        chunks_db_path, record_a, record_b = _build_two_record_collection(tmp_path)
        # A fresh, fully self-consistent envelope manifest -- the strict
        # pipeline succeeds directly, subset-upgrade never even engages.
        _write_manifest_for_records(
            tmp_path, {"bija0001": record_a, "bijb0002": record_b}
        )
        _write_authoritative_vector_count(tmp_path, 2)
        result = consolidate_collection_in_place(tmp_path)
        assert result.status == "already_consolidated"

        manifest = _load_verified_manifest(
            tmp_path,
            chunks_db_path,
            require_authoritative_count=False,
            allow_manifest_upgrade=True,
            read_only=False,
        )
        assert set(manifest.keys()) == {"bija0001", "bijb0002"}


class TestManifestUpgradeWriteFailurePropagates:
    """Dual-review Finding 2 (Codex HIGH #7 + Claude MEDIUM,
    independently converged): a genuine write/IO failure while
    persisting the upgraded manifest must propagate as its own distinct,
    retryable error -- NEVER silently converted into re-raising the
    ORIGINAL UnrecoverableConsolidationCorruptionError, which would
    permanently brand a perfectly-recoverable collection unrecoverable."""

    def test_write_failure_during_upgrade_propagates_not_masked_as_unrecoverable(
        self, tmp_path: Path
    ) -> None:
        chunks_db_path, record_a, record_b = _build_two_record_collection(tmp_path)

        with ChunkStore(chunks_db_path, immutable=True) as store:
            stored_a = store.read("bija0001")
        # A genuinely stale-but-valid flat manifest -- the exact shape
        # Issue #1503 taught us to ACCEPT and upgrade, provided the
        # rewrite itself succeeds.
        stale_manifest = {"bija0001": _compute_record_content_digest(stored_a)}
        manifest_path = tmp_path / "chunks_db_content_manifest.json"
        manifest_path.write_text(json.dumps(stale_manifest))

        # Force a REAL OS-level write failure for the manifest-rewrite
        # step: strip write permission from the collection directory so
        # `_write_content_manifest`'s `tempfile.mkstemp(dir=collection_dir)`
        # genuinely raises PermissionError. Validation (pure reads) is
        # unaffected -- only the write fails.
        original_mode = tmp_path.stat().st_mode
        os.chmod(tmp_path, 0o555)
        try:
            with pytest.raises(ConsolidationVerificationError) as excinfo:
                consolidate_collection_in_place(tmp_path)

            # The MISLEADING permanent-unrecoverable verdict must NEVER
            # be what the caller sees for a transient write failure.
            assert not isinstance(
                excinfo.value, UnrecoverableConsolidationCorruptionError
            )
        finally:
            os.chmod(tmp_path, original_mode)

        # Nothing was actually mutated by the failed write attempt.
        assert manifest_path.read_bytes() == json.dumps(stale_manifest).encode()
