"""Per-collection base-clone consolidation primitive (Story #1458 AC3/AC4/AC6,
Epic #1454 "Fleet Migration").

``consolidate_collection_in_place()`` converts ONE collection directory's
legacy sharded ``vector_*.json`` layout into the consolidated ``chunks.db``
layout, IN PLACE, as a pure-addition -> verify -> durable-flip -> individual
-cleanup sequence:

  1. READ chunk data from the existing sharded ``vector_*.json`` files via
     the SIDE-EFFECT-FREE scan primitive
     (``IDIndexManager.scan_vectors_for_id_map`` -- Story #1458 AC3 step 1;
     NEVER ``rebuild_from_vectors()``, which would recreate the retired
     ``id_index.bin``).
  2. WRITE a new ``chunks.db`` into the SAME collection directory via
     :class:`~code_indexer.storage.sqlite_chunk_store.ChunkStore` -- a pure
     addition; no old file is touched yet.
  3. READ-BACK VERIFY the new store field-for-field against the originals
     (payload, vector, point id -- not row counts only) BEFORE anything is
     deleted or flagged.
  4. Durably FLIP the ``chunks_db`` discriminator in ``collection_meta.json``
     via the existing atomic+fsync+``os.replace``+dir-fsync writer
     (:func:`~code_indexer.storage.shared.chunk_layout.write_chunks_db_discriminator`).
  5. DELETE the old sharded files individually (never the collection root,
     never a whole-directory replace).

Crash-safety (AC4) falls entirely out of this ordering: a crash at any point
before step 4 leaves the old sharded representation authoritative (the flag
was never set) and a re-run simply redoes steps 1-4 (a pure-addition
re-write is always safe -- ``ChunkStore.write_batch`` is INSERT OR REPLACE).
A crash after step 4 leaves only harmless orphan old files; a re-run detects
the ``CHUNKS_DB`` layout via :func:`resolve_chunk_layout` and proceeds
directly to step 5's cleanup, never re-doing steps 1-4.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Union

from code_indexer.storage.id_index_manager import IDIndexManager
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
    write_chunks_db_discriminator,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore
from code_indexer.utils.file_locking import nfs_safe_fsync

logger = logging.getLogger(__name__)

CHUNKS_DB_FILENAME = "chunks.db"

# Codex Finding #8 (HIGH): bound how many records are held in memory at
# once during write+verify, instead of materializing the entire
# collection's records/originals (a real OOM risk at millions-of-chunks
# scale). Deliberately a plain module global (not a function default) so
# tests can monkeypatch it to a small value and prove the batching
# mechanism itself without needing million-record fixtures.
_MIGRATION_BATCH_SIZE = 500

# Cap on how many missing/extra point IDs to include in a diagnostic error
# message -- avoids an unbounded log/exception message for a large mismatch.
_MAX_MISSING_SAMPLE_SIZE = 5

# Codex CRITICAL finding (round 4): crash-durable per-record content-digest
# manifest, written BEFORE the legacy source is deleted so post-cleanup
# verification (when there is no still-present legacy record left to
# compare field-for-field against) has something real to compare STORED
# CONTENT against -- not merely key presence. Skipped by name during the
# legacy-vector scan (id_index_manager.py's
# _CHUNKS_DB_CONTENT_MANIFEST_FILENAME, kept in sync with this literal).
_CONTENT_MANIFEST_FILENAME = "chunks_db_content_manifest.json"


def _compute_record_content_digest(record: Dict[str, Any]) -> str:
    """Deterministic content digest of a record as READ BACK from
    chunks.db (never the raw legacy JSON -- vectors are float32-quantized
    on store, so digesting the STORED form is what post-cleanup
    verification can reproduce byte-for-byte on a later fresh read).

    Codex CRITICAL finding (round 5): the vector is NEVER passed through
    ``json.dumps``/``str()`` -- ``str(ndarray)`` truncates large arrays
    with an ellipsis (NumPy's print-summarization threshold, which a
    realistic 1024-dim embedding vector exceeds), making interior-element
    corruption completely undetectable. Instead the vector is hashed via
    its raw ``<f4`` (little-endian float32) byte encoding -- the EXACT
    dtype :class:`~code_indexer.storage.sqlite_chunk_store.ChunkStore`
    itself uses for storage (``_VECTOR_DTYPE``) -- so every element,
    including the middle of a 1024-dim array, is covered byte-for-byte.
    The non-vector fields (payload/metadata/...) never contain large
    arrays, so they safely go through JSON as before.
    """
    import numpy as np

    vector = record.get("vector")
    vector_bytes = np.asarray(vector, dtype="<f4").tobytes()

    non_vector = {k: v for k, v in record.items() if k != "vector"}
    non_vector_json = json.dumps(non_vector, sort_keys=True, default=str)

    hasher = hashlib.sha256()
    hasher.update(vector_bytes)
    hasher.update(non_vector_json.encode("utf-8"))
    return hasher.hexdigest()


def _content_manifest_path(collection_dir: Path) -> Path:
    return collection_dir / _CONTENT_MANIFEST_FILENAME


def _atomic_write_json(target_path: Path, data: Dict[str, Any]) -> None:
    """Atomic + durable JSON write (temp file in the SAME directory,
    flush+fsync, ``os.replace``, then an ``nfs_safe_fsync`` of the
    containing directory) -- mirrors
    :func:`~code_indexer.storage.shared.chunk_layout.write_chunks_db_discriminator`'s
    own durability pattern."""
    collection_dir = target_path.parent
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(collection_dir), suffix=".tmp")
    fd_owned = False
    try:
        try:
            tmp_f = os.fdopen(tmp_fd, "w")
            fd_owned = True
            with tmp_f:
                json.dump(data, tmp_f)
                tmp_f.flush()
                nfs_safe_fsync(tmp_f.fileno())
            os.replace(tmp_path, str(target_path))
        finally:
            if not fd_owned:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    dir_fd = os.open(str(collection_dir), os.O_RDONLY)
    try:
        nfs_safe_fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _write_content_manifest(
    collection_dir: Path, chunks_db_path: Path, point_ids: "set"
) -> None:
    """Persist the crash-durable content-integrity manifest for every
    ``point_ids`` entry, via a FRESH reopen (never the handle that just
    wrote the batch -- Finding #3's own standard). Called ONLY after the
    exact-set verification has already proven chunks.db content is
    correct, and ONLY BEFORE the durable discriminator flip -- if this
    write fails, the flip never happens and the collection stays safely
    SHARDED_JSON (retryable), matching this module's existing crash-safety
    ordering.

    Codex round-6 HIGH finding #8: streams each (point_id, digest) entry
    directly to the temp file as it is computed -- never accumulating a
    full ``point_id -> digest`` dict in memory. For a millions-of-chunks
    production collection, a fully-materialized manifest dict (plus a
    subsequent whole-dict ``json.dump``) is real, unbounded memory
    growth; this keeps peak memory O(1) regardless of collection size.
    Mirrors :func:`_atomic_write_json`'s exact crash-durability pattern
    (temp file in the SAME directory, flush+fsync, ``os.replace``, then
    an ``nfs_safe_fsync`` of the containing directory) -- only the
    serialization strategy differs (hand-written incremental JSON object
    syntax instead of one whole-dict ``json.dump`` call), so the
    resulting file is still byte-for-byte a standard JSON object,
    unchanged for :func:`_read_content_manifest`'s ``json.load`` reader.
    """
    manifest_path = _content_manifest_path(collection_dir)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(collection_dir), suffix=".tmp")
    fd_owned = False
    try:
        try:
            tmp_f = os.fdopen(tmp_fd, "w")
            fd_owned = True
            with tmp_f:
                tmp_f.write("{")
                with ChunkStore(chunks_db_path) as store:
                    first = True
                    for point_id in point_ids:
                        stored = store.read(point_id)
                        if stored is None:
                            raise ConsolidationVerificationError(
                                f"Content-manifest write refused for "
                                f"{collection_dir}: point {point_id!r} "
                                f"unexpectedly missing from a fresh "
                                f"reopen immediately after exact-set "
                                f"verification succeeded"
                            )
                        digest = _compute_record_content_digest(stored)
                        if not first:
                            tmp_f.write(",")
                        first = False
                        tmp_f.write(json.dumps(point_id))
                        tmp_f.write(":")
                        tmp_f.write(json.dumps(digest))
                tmp_f.write("}")
                tmp_f.flush()
                nfs_safe_fsync(tmp_f.fileno())
            os.replace(tmp_path, str(manifest_path))
        finally:
            if not fd_owned:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    dir_fd = os.open(str(collection_dir), os.O_RDONLY)
    try:
        nfs_safe_fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _read_content_manifest(collection_dir: Path) -> Optional[Dict[str, str]]:
    """Read the content-integrity manifest, or None if absent/unreadable/
    malformed -- never raises (callers decide how to treat a missing
    manifest)."""
    manifest_path = _content_manifest_path(collection_dir)
    try:
        with open(manifest_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _batched(items: Iterable[Any], size: int) -> Iterable[list]:
    """Yield successive bounded-size lists accumulated from ``items``.

    Accepts any iterable (not just a sized/sliceable sequence) -- items are
    accumulated one at a time and flushed once ``size`` is reached, so this
    works equally over a list, a generator, or a dict_items view.
    """
    if size <= 0:
        raise ValueError(f"_batched: size must be positive, got {size}")
    batch: list = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


class ConsolidationVerificationError(Exception):
    """Raised when field-for-field read-back verification (AC3 step 3)
    detects a mismatch between an original sharded record and what was
    persisted into ``chunks.db``. Always raised BEFORE the durable
    discriminator flip (step 4) -- a verification failure therefore never
    leaves the collection in a state where the flag is set but the data is
    wrong."""


class ConsolidationCleanupError(Exception):
    """Codex MEDIUM finding: raised when the destructive legacy-file
    cleanup step (AC3 step 5 / AC4 resume-cleanup) fails to delete one or
    more legacy files for a reason OTHER than the file already being gone
    (a race against a prior partial cleanup pass -- harmless and
    expected). E.g. permission denied, read-only filesystem, disk I/O
    error. Never silently reported as a "consolidated"/"already_
    consolidated" success (Messi Rule #13 anti-silent-failure) -- the
    caller sees this explicitly, and a retry on a later pass naturally
    re-attempts cleanup (idempotent, matching this module's existing
    crash-safety design: the discriminator is already durably set by this
    point, so a retry resumes directly into cleanup, exactly as if the
    process had crashed immediately after this point)."""


@dataclass
class ConsolidationResult:
    """Outcome of one :func:`consolidate_collection_in_place` call.

    status:
        - "consolidated": steps 1-5 all ran successfully this call.
        - "already_consolidated": the collection was already CHUNKS_DB on
          entry (either a genuinely prior run, or an AC4 resume after a
          crash mid-step-5); only cleanup (step 5) ran, if anything
          remained to clean up.
        - "skipped_insufficient_disk": the disk-headroom preflight failed;
          the collection was left completely untouched in its legacy
          sharded state.
    """

    status: str
    records_written: int = 0
    old_files_deleted: int = 0
    detail: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


def _estimate_bytes_needed(json_paths: Iterable[Path]) -> int:
    total = 0
    for p in json_paths:
        try:
            total += p.stat().st_size
        except OSError:
            continue
    return total


#: Codex round-6 MEDIUM finding: the raw legacy-JSON byte estimate
#: (_estimate_bytes_needed) only counts the sharded vector_*.json files
#: themselves -- it ignores the transient chunks.db (SQLite page/index
#: overhead), the content-integrity manifest, and SQLite WAL/journal
#: files that coexist alongside the legacy source during the migration
#: window. A safety margin multiplier closes that underestimate.
_DISK_HEADROOM_SAFETY_MULTIPLIER = 2.0


def _has_disk_headroom(collection_dir: Path, estimated_bytes: int) -> bool:
    """os.statvfs-based free-space preflight (AC3, PT-7.23), with a
    safety margin multiplier applied to the raw byte estimate.

    Codex round-6 MEDIUM finding: for this specific DESTRUCTIVE,
    UNATTENDED scheduled job (fleet migration), a statvfs failure must
    fail CLOSED (skip, source stays untouched) rather than OPEN --
    unlike a generic best-effort advisory guard, this job runs against
    real production disks with no operator watching in real time, so a
    transient failure here (e.g. an NFS hiccup) must never be silently
    treated as "plenty of room". Read-back verification (step 3) and the
    crash-safety ordering (AC4) remain the real correctness guarantees;
    this preflight is a genuine capacity safeguard, not a substitute for
    either.
    """
    try:
        stat = os.statvfs(str(collection_dir))
    except OSError as exc:
        logger.error(
            "consolidate_collection_in_place: statvfs failed for %s (%s); "
            "failing CLOSED (skipping consolidation, collection stays "
            "untouched in its legacy sharded state) -- this destructive "
            "unattended job must never treat a failed capacity check as "
            "'plenty of room'",
            collection_dir,
            exc,
        )
        return False
    available_bytes = stat.f_bavail * stat.f_frsize
    return available_bytes >= estimated_bytes * _DISK_HEADROOM_SAFETY_MULTIPLIER


def _read_record(json_path: Path) -> Dict[str, Any]:
    with open(json_path) as f:
        record: Dict[str, Any] = json.load(f)
    return record


def _vector_values_equal(original_vector: Any, stored_vector: Any) -> bool:
    """Compare vectors as float32 (chunks.db's on-disk precision) so a
    float64 Python-list original and a float32-round-tripped stored value
    compare correctly rather than spuriously mismatching on precision."""
    import numpy as np

    try:
        original_arr = np.asarray(original_vector, dtype="<f4")
        stored_arr = np.asarray(stored_vector, dtype="<f4")
    except (TypeError, ValueError):
        return False
    return bool(
        original_arr.shape == stored_arr.shape
        and np.array_equal(original_arr, stored_arr)
    )


def _verify_record_field_for_field(
    point_id: str, original: Dict[str, Any], stored: Optional[Dict[str, Any]]
) -> None:
    if stored is None:
        raise ConsolidationVerificationError(
            f"Read-back verification failed for point {point_id!r}: point "
            f"missing from chunks.db after write"
        )

    if stored.get("id") != original.get("id"):
        raise ConsolidationVerificationError(
            f"Read-back verification failed for point {point_id!r}: id "
            f"mismatch ({original.get('id')!r} != {stored.get('id')!r})"
        )

    if not _vector_values_equal(original.get("vector"), stored.get("vector")):
        raise ConsolidationVerificationError(
            f"Read-back verification failed for point {point_id!r}: vector mismatch"
        )

    # Codex round-6 CRITICAL finding #1: a per-key `stored.get(key) !=
    # expected_value` comparison alone cannot distinguish a key that is
    # genuinely `None` in `original` from a key that is MISSING entirely
    # from `stored` -- both make `stored.get(key)` default to `None`, so
    # a silently DROPPED field is invisible to verification. Require
    # exact non-reserved key-SET equality FIRST, so a missing (or
    # unexpectedly extra) key is always caught regardless of what value
    # it would otherwise have compared against.
    reserved_keys = {"id", "vector"}
    original_keys = set(original.keys()) - reserved_keys
    stored_keys = set(stored.keys()) - reserved_keys
    if original_keys != stored_keys:
        missing_from_stored = sorted(original_keys - stored_keys)
        extra_in_stored = sorted(stored_keys - original_keys)
        raise ConsolidationVerificationError(
            f"Read-back verification failed for point {point_id!r}: "
            f"field key-set mismatch -- missing from stored: "
            f"{missing_from_stored}, extra in stored: {extra_in_stored}"
        )

    _missing_field_sentinel = object()
    for key, expected_value in original.items():
        if key in reserved_keys:
            continue
        stored_value = stored.get(key, _missing_field_sentinel)
        if stored_value != expected_value:
            raise ConsolidationVerificationError(
                f"Read-back verification failed for point {point_id!r}: "
                f"field {key!r} mismatch (expected {expected_value!r}, "
                f"got {stored_value!r})"
            )


def _write_and_verify_batch(chunks_db_path: Path, batch_items: list) -> None:
    """Write ONE batch, then verify it via a FRESH reopen -- Codex Finding
    #3 ("not the same already-open handle") combined with Finding #8
    (bounded memory: only this batch's originals are ever held, discarded
    immediately after verification).

    Args:
        chunks_db_path: Path to the chunks.db being built.
        batch_items: A list of ``(point_id, json_path)`` pairs for this
            batch only.
    """
    batch_originals: Dict[str, Dict[str, Any]] = {}
    records = []
    for point_id, json_path in batch_items:
        record = _read_record(json_path)
        batch_originals[point_id] = record
        records.append(record)

    with ChunkStore(chunks_db_path) as write_store:
        write_store.write_batch(records)

    # Finding #3: a FRESH ChunkStore instance (new connection), never the
    # handle that just performed the write -- proves the batch is actually
    # durable on disk, not merely reflected in an in-process cache.
    with ChunkStore(chunks_db_path) as verify_store:
        for point_id, original in batch_originals.items():
            stored = verify_store.read(point_id)
            _verify_record_field_for_field(point_id, original, stored)


def _verify_chunks_db_before_resume_cleanup(
    collection_dir: Path, still_present_id_map: Dict[str, Path]
) -> None:
    """Codex Finding #2 (CRITICAL): on resume (discriminator already
    set), NEVER trust the flag alone for the destructive legacy-cleanup
    decision -- "present-and-verified", not "flag says so".

    Reopens chunks.db fresh (a NEW connection, matching Finding #3's own
    "never the handle that just wrote it" standard) and, for every legacy
    record STILL ON DISK (``still_present_id_map`` -- a fresh re-scan, so
    this also naturally covers the case where cleanup already fully
    finished on a prior resume: an empty map makes every check below a
    no-op that still proves chunks.db itself opens cleanly), verifies BOTH
    (a) an entry exists in chunks.db, AND (b) that entry's field content
    matches the legacy source exactly (a present-but-corrupted record is
    exactly as dangerous as a missing one -- ID-membership alone is not
    enough).

    A missing/unreadable store is always an unrecoverable hard failure (the
    whole database is gone -- there is no partial state to repair). A
    still-present legacy record that is missing or field-mismatched in
    chunks.db is RECOVERABLE (the legacy source is right there): this
    function attempts a targeted rebuild of exactly those records (reusing
    :func:`_write_and_verify_batch`, Finding #3's own write+verify
    mechanism) before refusing -- never permanently raising over data that
    can be safely regenerated from its still-intact legacy source.
    """
    chunks_db_path = collection_dir / CHUNKS_DB_FILENAME
    if not chunks_db_path.exists():
        raise ConsolidationVerificationError(
            f"Resume-cleanup refused for {collection_dir}: the chunks_db "
            f"discriminator is set but {CHUNKS_DB_FILENAME} does not exist "
            f"on disk -- refusing to delete legacy data against a flag "
            f"that does not match reality"
        )

    try:
        with ChunkStore(chunks_db_path) as store:
            actual_ids = store.all_point_ids()
    except Exception as exc:
        raise ConsolidationVerificationError(
            f"Resume-cleanup refused for {collection_dir}: "
            f"{CHUNKS_DB_FILENAME} exists but failed to open/query "
            f"cleanly: {exc}"
        ) from exc

    missing = set(still_present_id_map.keys()) - actual_ids
    present_ids = set(still_present_id_map.keys()) - missing
    mismatched: set = set()
    if present_ids:
        with ChunkStore(chunks_db_path) as verify_store:
            for point_id in present_ids:
                original = _read_record(still_present_id_map[point_id])
                stored = verify_store.read(point_id)
                try:
                    _verify_record_field_for_field(point_id, original, stored)
                except ConsolidationVerificationError:
                    mismatched.add(point_id)

    to_rebuild = missing | mismatched
    if to_rebuild:
        logger.warning(
            "_verify_chunks_db_before_resume_cleanup: %d still-present "
            "legacy record(s) missing/mismatched in %s (missing=%d, "
            "mismatched=%d) -- attempting rebuild from the still-intact "
            "legacy source before refusing cleanup",
            len(to_rebuild),
            chunks_db_path,
            len(missing),
            len(mismatched),
        )
        rebuild_items = [(pid, still_present_id_map[pid]) for pid in to_rebuild]
        # Reuses Finding #3's exact fresh-reopen write+verify mechanism --
        # a failure here (e.g. a rebuild item's legacy JSON is itself
        # unreadable) propagates as ConsolidationVerificationError, an
        # unrecoverable hard failure.
        _write_and_verify_batch(chunks_db_path, rebuild_items)

        with ChunkStore(chunks_db_path) as recheck_store:
            actual_ids = recheck_store.all_point_ids()
        still_missing = set(still_present_id_map.keys()) - actual_ids
        if still_missing:
            sample = sorted(still_missing)[:_MAX_MISSING_SAMPLE_SIZE]
            raise ConsolidationVerificationError(
                f"Resume-cleanup refused for {collection_dir}: rebuild "
                f"attempt still left {len(still_missing)} legacy record(s) "
                f"without a {CHUNKS_DB_FILENAME} counterpart (sample: "
                f"{sample}) -- refusing to delete legacy files that have "
                f"no verified consolidated counterpart"
            )

    # Codex CRITICAL finding (round 4/5): every id with NO still-present
    # legacy source is UNRECOVERABLE if its stored content is wrong --
    # there is nothing left to rebuild from. Key-presence alone is not
    # enough; verify against the crash-durable content manifest persisted
    # before the legacy source was ever deleted.
    #
    # Round 5 hardening: this check is now UNCONDITIONAL (never gated on
    # "is there something currently present to flag"). A per-key digest
    # check that only iterates over ids CURRENTLY in chunks.db has TWO
    # blind spots: (a) a row that vanishes ENTIRELY (deleted) is simply
    # absent from that iteration, so its disappearance goes unnoticed;
    # (b) if the ONLY unrecoverable row that ever existed is deleted, the
    # "unrecoverable" set computed purely from actual_ids collapses to
    # empty too, hiding the deletion even from a set-based check that is
    # only run conditionally. Every resume-verify call therefore requires
    # AND validates the manifest, unconditionally: exact set-equality
    # between "what the manifest promises is unrecoverable"
    # (manifest_keys - still_present_keys) and "what chunks.db actually
    # has that's unrecoverable" (actual_ids - still_present_keys), THEN
    # per-row digest verification for that set.
    still_present_keys = set(still_present_id_map.keys())
    actual_unrecoverable = actual_ids - still_present_keys

    manifest = _read_content_manifest(collection_dir)
    if manifest is None:
        raise ConsolidationVerificationError(
            f"Resume-cleanup refused for {collection_dir}: the "
            f"content-integrity manifest is missing entirely (the "
            f"chunks_db discriminator is set, so a manifest MUST exist) "
            f"-- refusing to treat this collection as verified without it"
        )

    manifest_keys = set(manifest.keys())
    manifest_unrecoverable = manifest_keys - still_present_keys
    if manifest_unrecoverable != actual_unrecoverable:
        missing_rows = manifest_unrecoverable - actual_unrecoverable
        extra_rows = actual_unrecoverable - manifest_unrecoverable
        sample_missing = sorted(missing_rows)[:_MAX_MISSING_SAMPLE_SIZE]
        sample_extra = sorted(extra_rows)[:_MAX_MISSING_SAMPLE_SIZE]
        raise ConsolidationVerificationError(
            f"Resume-cleanup refused for {collection_dir}: manifest/"
            f"{CHUNKS_DB_FILENAME} row-SET mismatch for unrecoverable "
            f"records -- {len(missing_rows)} manifested record(s) are "
            f"missing from {CHUNKS_DB_FILENAME} (sample: {sample_missing}), "
            f"{len(extra_rows)} record(s) in {CHUNKS_DB_FILENAME} are not "
            f"accounted for in the manifest (sample: {sample_extra}) -- "
            f"refusing to treat this collection as verified"
        )

    corrupted: list = []
    if actual_unrecoverable:
        with ChunkStore(chunks_db_path) as content_store:
            for point_id in sorted(actual_unrecoverable):
                stored = content_store.read(point_id)
                actual_digest = (
                    _compute_record_content_digest(stored)
                    if stored is not None
                    else None
                )
                if actual_digest != manifest.get(point_id):
                    corrupted.append(point_id)
    if corrupted:
        sample = corrupted[:_MAX_MISSING_SAMPLE_SIZE]
        raise ConsolidationVerificationError(
            f"Resume-cleanup refused for {collection_dir}: {len(corrupted)} "
            f"record(s) in {CHUNKS_DB_FILENAME} failed content-integrity "
            f"verification against the persisted manifest (sample: "
            f"{sample}) -- their legacy source is already gone, so this "
            f"corruption is UNRECOVERABLE and requires manual "
            f"intervention"
        )


def _scan_or_fail_on_rejected_records(
    collection_dir: Path,
) -> Dict[str, Path]:
    """Codex Finding #4 (CRITICAL, Messi Rule #13 anti-silent-failure):
    fail LOUDLY, never flip/cleanup, if any legacy record was rejected as
    malformed -- a genuinely-empty source and an all-rejected source must
    never be treated identically."""
    # Explicit LHS annotation works around a pre-existing project mypy
    # module-identity quirk (this file resolves under a src.-prefixed
    # module identity when checked from the repo root, which otherwise
    # infers this cross-module return as Any despite the callee's
    # correctly-annotated Tuple[Dict[str, Path], int] return type).
    id_map: Dict[str, Path]
    rejected_count: int
    id_map, rejected_count = IDIndexManager().scan_vectors_for_id_map_verbose(
        collection_dir
    )
    if rejected_count > 0:
        raise ConsolidationVerificationError(
            f"Migration refused for {collection_dir}: {rejected_count} "
            f"legacy record(s) were rejected as malformed during the scan "
            f"(see WARNING logs above for each file) -- refusing to "
            f"proceed, since consolidating around silently-dropped data "
            f"would be a silent data-loss bug. Fix or remove the malformed "
            f"file(s) and retry."
        )
    return id_map


def _remove_empty_subdirs(collection_dir: Path) -> None:
    """Bottom-up removal of now-empty hash-shard subdirectories, e.g.
    ``aa/bb/cc/dd/`` -- generic (no assumption about shard depth), never
    removes ``collection_dir`` itself."""
    for dirpath, _dirnames, _filenames in os.walk(str(collection_dir), topdown=False):
        if Path(dirpath) == collection_dir:
            continue
        p = Path(dirpath)
        try:
            if not any(p.iterdir()):
                p.rmdir()
        except OSError:
            pass  # Not empty (race) or already gone -- harmless, skip


def _cleanup_old_sharded_files(collection_dir: Path) -> int:
    """AC3 step 5 / AC4 resume-cleanup: delete every remaining legacy
    ``vector_*.json`` file individually, unlink the retired
    ``id_index.bin`` if present, then remove now-empty shard
    subdirectories. Idempotent -- safe to call on a collection that has
    already been fully cleaned up (no-op).

    Codex MEDIUM finding (round 4, hardened): a failed unlink is ONLY
    treated as harmless when unlink() ITSELF raises ``FileNotFoundError``
    (a race against a prior partial cleanup pass -- proof of ENOENT
    directly from the syscall). A SECOND ``Path.exists()`` probe to
    decide this is NOT used -- that probe is itself fallible (can raise,
    e.g. ELOOP, or return a false negative), so it is not proof of
    anything. Every OTHER ``OSError`` (permission denied, read-only
    filesystem, disk I/O error, ...) is a real cleanup failure and raises
    :class:`ConsolidationCleanupError` -- never silently swallowed and
    reported as success.
    """
    deleted = 0
    failed_paths: list = []
    stray_id_map = IDIndexManager().scan_vectors_for_id_map(collection_dir)
    for json_path in stray_id_map.values():
        try:
            json_path.unlink()
            deleted += 1
        except FileNotFoundError:
            pass  # Already gone (race/prior partial cleanup) -- harmless
        except OSError as exc:
            logger.warning(
                "_cleanup_old_sharded_files: failed to delete %s: %s",
                json_path,
                exc,
            )
            failed_paths.append(json_path)

    # Codex MEDIUM finding (round 5): attempt the unlink UNCONDITIONALLY
    # -- no fallible Path.exists() pre-check -- mirroring the stray
    # vector_*.json loop above (round 4). A false-negative .exists()
    # (transient stat error, or a race where the file appears between
    # the check and the unlink) would otherwise silently skip a real
    # stale id_index.bin. Only a direct FileNotFoundError raised BY
    # unlink() itself is proof of a benign already-gone race.
    id_index_bin = collection_dir / IDIndexManager.INDEX_FILENAME
    try:
        id_index_bin.unlink()
    except FileNotFoundError:
        pass  # Already gone (race/prior partial cleanup) -- harmless
    except OSError as exc:
        logger.warning(
            "_cleanup_old_sharded_files: failed to delete %s: %s",
            id_index_bin,
            exc,
        )
        failed_paths.append(id_index_bin)

    _remove_empty_subdirs(collection_dir)

    if failed_paths:
        sample = [str(p) for p in failed_paths[:_MAX_MISSING_SAMPLE_SIZE]]
        raise ConsolidationCleanupError(
            f"Cleanup incomplete for {collection_dir}: failed to delete "
            f"{len(failed_paths)} legacy file(s) (sample: {sample}) -- "
            f"refusing to silently report success; will be retried on a "
            f"later pass"
        )

    return deleted


def verify_collection_fully_migrated(collection_dir: Union[str, Path]) -> bool:
    """Codex CRITICAL Finding #2 (round 2): the reusable, side-effect-free
    completeness oracle fleet-migration discovery MUST invoke -- never
    reinvent a weaker, discriminator-flag-only check.

    True iff ALL of the following hold, each proven fresh from disk:
      (a) the ``chunks_db`` discriminator is set (:func:`resolve_chunk_layout`);
      (b) ZERO legacy sharded files remain on disk. A flag set with legacy
          files STILL present means a crash happened between the durable
          flip and cleanup completing -- this collection is NOT yet done,
          regardless of the flag, so callers must re-attempt the real
          migration/resume path (closes the "flag alone is sufficient"
          crash-recovery gap);
      (c) chunks.db opens cleanly via a FRESH reopen (reuses
          :func:`_verify_chunks_db_before_resume_cleanup`'s exact
          verification -- Finding #3's "new connection, not the handle
          that wrote it" standard). With zero legacy files left there is
          nothing left to compare record-for-record against, but this
          still proves chunks.db itself is not corrupt/missing.

    NEVER raises: an unreadable/malformed legacy record (Finding #4) or
    any verification failure is treated as "not yet fully migrated" (safe
    to re-attempt) rather than propagating an exception through what
    callers (get_stats(), is_repo_already_migrated()) treat as a pure
    read-only predicate.
    """
    collection_dir = Path(collection_dir)
    if resolve_chunk_layout(collection_dir) != ChunkLayout.CHUNKS_DB:
        return False

    try:
        still_present_id_map = _scan_or_fail_on_rejected_records(collection_dir)
    except ConsolidationVerificationError:
        return False

    if still_present_id_map:
        # Legacy files still on disk: cleanup did not fully complete.
        return False

    try:
        _verify_chunks_db_before_resume_cleanup(collection_dir, still_present_id_map)
    except ConsolidationVerificationError:
        return False

    return True


def consolidate_collection_in_place(
    collection_dir: Union[str, Path],
) -> ConsolidationResult:
    """Consolidate ONE collection's sharded ``vector_*.json`` layout into a
    ``chunks.db`` written into the SAME directory, in place.

    Discriminator-driven and idempotent (AC4): if the collection already
    resolves to ``ChunkLayout.CHUNKS_DB`` on entry, steps 1-4 are NEVER
    redone -- only step 5's cleanup runs (a no-op if already clean).

    Args:
        collection_dir: The collection directory to consolidate (a
            collection inside the target repo's MUTABLE BASE CLONE, per
            AC3 -- this function itself is location-agnostic and simply
            operates on whatever path it is given).

    Returns:
        A :class:`ConsolidationResult` describing what happened.

    Raises:
        ConsolidationVerificationError: read-back verification (step 3)
            found a mismatch. The discriminator is NEVER set in this case.
    """
    collection_dir = Path(collection_dir)

    if resolve_chunk_layout(collection_dir) == ChunkLayout.CHUNKS_DB:
        # Resume path. Finding #4: fail loudly on any rejected legacy
        # record found during the fresh re-scan (never silently proceed).
        still_present_id_map = _scan_or_fail_on_rejected_records(collection_dir)
        # Finding #2: never trust the discriminator flag alone -- reopen
        # chunks.db fresh and verify it actually has every still-present
        # legacy record before deleting anything.
        _verify_chunks_db_before_resume_cleanup(collection_dir, still_present_id_map)
        deleted = _cleanup_old_sharded_files(collection_dir)
        return ConsolidationResult(
            status="already_consolidated", old_files_deleted=deleted
        )

    # Step 1: side-effect-free scan -- trustworthy point_id -> json_path
    # map. Finding #4: fail loudly on any rejected record, never flip.
    id_map = _scan_or_fail_on_rejected_records(collection_dir)

    estimated_bytes = _estimate_bytes_needed(id_map.values())
    if not _has_disk_headroom(collection_dir, estimated_bytes):
        logger.error(
            "consolidate_collection_in_place: insufficient disk headroom "
            "for %s (estimated %d bytes needed for the transient "
            "chunks.db + sharded-files coexistence) -- skipping "
            "consolidation; collection remains in its legacy sharded "
            "state, fully authoritative",
            collection_dir,
            estimated_bytes,
        )
        return ConsolidationResult(
            status="skipped_insufficient_disk",
            detail=f"estimated {estimated_bytes} bytes needed, insufficient free space",
        )

    chunks_db_path = collection_dir / CHUNKS_DB_FILENAME
    # Ensure chunks.db (and its schema) exists even when id_map is empty
    # (a genuinely empty collection) -- the batch loop below may run zero
    # iterations in that case.
    ChunkStore(chunks_db_path).close()

    # Steps 2-3, batched (Finding #8: bounded memory -- write a batch,
    # verify it via a fresh reopen (Finding #3), discard, move to the
    # next). Only a lean point_id set (not full records) is retained
    # across batches, for the final exact-set check below.
    expected_ids: set = set()
    for batch in _batched(list(id_map.items()), _MIGRATION_BATCH_SIZE):
        _write_and_verify_batch(chunks_db_path, batch)
        expected_ids.update(point_id for point_id, _json_path in batch)

    # Finding #3: a GLOBAL exact-set comparison (same count, same IDs, no
    # extras) via a FRESH reopen -- catches a stale/resurrected row left
    # over from a prior interrupted run that per-batch verification alone
    # (checking only "this batch's originals are present") cannot see.
    with ChunkStore(chunks_db_path) as final_store:
        actual_ids = final_store.all_point_ids()
        actual_count = final_store.count()

    if actual_ids != expected_ids or actual_count != len(expected_ids):
        extra = actual_ids - expected_ids
        missing = expected_ids - actual_ids
        raise ConsolidationVerificationError(
            f"Exact-set verification failed for {collection_dir}: expected "
            f"{len(expected_ids)} record(s), chunks.db has {actual_count} "
            f"(extra={len(extra)}, missing={len(missing)}) -- refusing to "
            f"flip the discriminator over a store that does not exactly "
            f"match the legacy source"
        )

    # Codex CRITICAL finding (round 4): persist the crash-durable content
    # manifest BEFORE the flip -- a set discriminator therefore always
    # guarantees the manifest exists (a crash between these two lines
    # leaves the flag unset, so a retry safely redoes this whole fresh
    # path rather than resuming into cleanup with no manifest).
    _write_content_manifest(collection_dir, chunks_db_path, expected_ids)

    # Step 4: durable discriminator flip -- only after full verification.
    write_chunks_db_discriminator(collection_dir)

    # Step 5: delete the old files individually.
    deleted = _cleanup_old_sharded_files(collection_dir)

    return ConsolidationResult(
        status="consolidated",
        records_written=len(id_map),
        old_files_deleted=deleted,
    )
