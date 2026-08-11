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
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Union

from code_indexer.storage.id_index_manager import IDIndexManager
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
    write_chunks_db_discriminator,
)
from code_indexer.storage.shared.collection_dedup_repair import (
    DedupRepairAmbiguousError,
    clear_stale_repair_marker,
    repair_duplicate_and_shifted_points,
)
from code_indexer.storage.sqlite_chunk_store import (
    ChunkStore,
    InvalidVectorError,
    NonFiniteVectorError,
)
from code_indexer.utils.file_locking import nfs_safe_fsync

logger = logging.getLogger(__name__)

#: Bug #1562: fleet-migration jobs reported a constant progress=25 for
#: their entire multi-hour lifetime, indistinguishable from a hang, because
#: this module -- where ALL the time is spent -- had zero progress
#: instrumentation. `ProgressCallback` matches the shape
#: `BackgroundJobManager.submit_job()` already injects into any worker
#: function that declares a `progress_callback` parameter
#: (`progress_callback(progress: int, phase: Optional[str] = None,
#: detail: Optional[str] = None)`, server/repositories/background_jobs.py),
#: so a callback threaded all the way down from
#: `FleetMigrationScheduler._run_next_candidate` can be handed to this
#: module's functions without any adapter.
ProgressCallback = Callable[[int, Optional[str], Optional[str]], None]


def _emit_progress(
    progress_callback: Optional[ProgressCallback],
    progress: int,
    *,
    phase: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    """Bug #1562: best-effort progress emission. A no-op when
    `progress_callback` is None (every pre-existing caller, byte-
    identical). Never lets a callback failure interrupt the actual
    migration -- this is a pure observability side-channel, not business
    logic, so a raising callback (e.g. a broken dashboard-side handler)
    must never abort or corrupt a real, multi-hour consolidation."""
    if progress_callback is None:
        return
    try:
        progress_callback(progress, phase, detail)
    except Exception:
        logger.warning(
            "consolidate_collection_in_place: progress_callback raised -- "
            "ignoring (observability only, must never interrupt migration)",
            exc_info=True,
        )


def _phase_scoped_callback(
    progress_callback: Optional[ProgressCallback],
    *,
    base: int,
    span: int,
    phase_prefix: str,
) -> Optional[ProgressCallback]:
    """Bug #1562: wrap `progress_callback` so a callee's own LOCAL 0-100
    progress value is rescaled into `[base, base + span]`, and its phase
    name is namespaced under `phase_prefix`. This is how a long inner loop
    (e.g. the write+verify batch loop, which knows only its own
    records-written/total-records ratio) reports genuine intra-phase
    progress within the outer function's overall phase allocation --
    exactly the guidance that a naive equal-weight-per-phase mapping would
    still look stuck during the real staging incident's dominant ~2h11m
    scan+write phase.

    Returns None (a no-op) when `progress_callback` is None, so every
    caller can pass the result straight through without a None-check.

    Raises:
        ValueError: `base`/`span` fall outside the valid `[0, 100]`
            percentage range, or `base + span` exceeds 100 -- a defensive
            precondition (Messi Rule #15), enforced UNCONDITIONALLY
            (before the `progress_callback is None` check) since every
            call site in this module passes literal constants, so a
            violation here is a genuine programming error, never live/
            user-supplied data.
    """
    if not (0 <= base <= 100):
        raise ValueError(f"_phase_scoped_callback: base={base} out of [0, 100]")
    if not (0 <= span <= 100):
        raise ValueError(f"_phase_scoped_callback: span={span} out of [0, 100]")
    if base + span > 100:
        raise ValueError(
            f"_phase_scoped_callback: base={base} + span={span} exceeds 100"
        )
    if progress_callback is None:
        return None

    def _scoped(
        progress: int, phase: Optional[str] = None, detail: Optional[str] = None
    ) -> None:
        clamped = max(0, min(100, progress))
        mapped = base + int(span * clamped / 100)
        scoped_phase = phase_prefix if phase is None else f"{phase_prefix}:{phase}"
        _emit_progress(progress_callback, mapped, phase=scoped_phase, detail=detail)

    return _scoped


#: Bug #1562: named percentage checkpoints for `consolidate_collection_in_
#: place`'s FRESH path (scan -> write -> finalize -> delete). Named rather
#: than inline literals so the overall phase allocation is documented in
#: one place instead of scattered magic numbers at each call site.
_PROGRESS_AFTER_SCAN = 15
_PROGRESS_WRITE_PHASE_BASE = 20
_PROGRESS_WRITE_PHASE_SPAN = 60
_PROGRESS_AFTER_FINALIZE = _PROGRESS_WRITE_PHASE_BASE + _PROGRESS_WRITE_PHASE_SPAN + 5
_PROGRESS_DELETE_PHASE_BASE = _PROGRESS_AFTER_FINALIZE
_PROGRESS_DELETE_PHASE_SPAN = 100 - _PROGRESS_DELETE_PHASE_BASE - 1


CHUNKS_DB_FILENAME = "chunks.db"

_COLLECTION_META_FILENAME = "collection_meta.json"

#: The top-level ``collection_meta.json`` discriminator key
#: (:mod:`chunk_layout` owns the canonical schema; this literal mirrors its
#: private ``_DISCRIMINATOR_KEY`` so the Finding 3 resume-repair path can
#: transiently CLEAR the committed discriminator via an atomic+durable
#: read-modify-write, without importing a private symbol).
_CHUNKS_DB_DISCRIMINATOR_KEY = "chunks_db"

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

# Bug #1486 Critical Finding 2 (dual review): the manifest is now a
# versioned, SELF-VALIDATING envelope --
# ``{"version": 1, "records": {...}, "expected_count": N,
# "root_digest": "<64 hex chars>"}`` -- rather than a bare flat
# point_id->digest dict. A bare `{}` (or any truncated-but-syntactically-
# -valid subset) was, under the OLD flat format, entirely self-
# consistent and silently accepted, which is the confirmed root cause of
# a real incident: a corrupt chunks.db + an empty manifest authorized
# rebuilding to an EMPTY store and reporting success, permanently
# destroying ~20,135 real vectors.
_MANIFEST_SCHEMA_VERSION = 1

_REQUIRED_MANIFEST_ENVELOPE_KEYS = (
    "version",
    "records",
    "expected_count",
    "root_digest",
)

#: Independent authoritative cross-check field, durably recorded into
#: collection_meta.json by THIS module's own fresh-consolidation write
#: path (see _write_authoritative_vector_count) -- self-consistency of
#: the manifest ALONE (its own expected_count matching its own records
#: count, and its own root_digest matching its own records) can never
#: detect a manifest that is entirely, uniformly wrong (e.g. genuinely
#: empty) in a way that still satisfies both internal checks. A SEPARATE
#: file, written at a different time via a different code path, is
#: required to catch that. Matches the field name CLAUDE.md documents as
#: the "collection_meta.json contract" every downstream reader already
#: trusts (vector_count/unique_file_count/points_count).
_VECTOR_COUNT_META_KEY = "vector_count"


def _empty_fold_accumulator() -> bytes:
    """Bug #1486 Critical Finding 2: the identity element for the
    whole-key-set root-digest fold below (all-zero bytes, XOR's own
    identity)."""
    return b"\x00" * hashlib.sha256().digest_size


def _fold_manifest_entry(accumulator: bytes, point_id: str, digest: str) -> bytes:
    """Bug #1486 Critical Finding 2: commutative, associative
    (XOR-based) fold of one manifest entry into the running whole-key-
    set root-digest accumulator.

    XOR-folding a per-entry SHA256 hash (rather than sorting all
    entries and hashing them in a fixed order) makes the final
    aggregate digest INDEPENDENT of processing/insertion order while
    still requiring only O(1) extra memory beyond the running
    accumulator itself -- no need to hold the full (point_id, digest)
    pair set in memory to compute a canonical whole-set digest,
    preserving this module's existing bounded-memory streaming write
    (Codex round-6 HIGH finding #8).
    """
    entry_hash = hashlib.sha256(f"{point_id}:{digest}".encode("utf-8")).digest()
    return bytes(a ^ b for a, b in zip(accumulator, entry_hash))


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


def _is_natively_built_chunks_db(collection_dir: Path) -> bool:
    """True iff this collection was built DIRECTLY in the consolidated
    layout and therefore has nothing to migrate (found while verifying Bug
    #1528 against a real indexing run).

    The content-integrity manifest is written ONLY by this module, purely to
    make the DESTRUCTIVE deletion of legacy sharded files safe. A collection
    created with the chunks_db layout from the start -- every
    server-provisioned collection since Story #1488 stamps
    ``--new-collection-layout=chunks_db`` on every server-spawned `cidx
    index` child, and every temporal collection since Bug #1528 -- never had
    legacy files, so it never gets a manifest and there is no destructive
    decision to guard. Requiring one there made
    :func:`verify_collection_fully_migrated` report a perfectly consolidated
    collection as unmigrated, and made
    :func:`consolidate_collection_in_place` raise
    :class:`UnrecoverableConsolidationCorruptionError` over it.

    Deliberately conservative -- ALL FIVE conditions must hold:
      * the ``chunks_db`` discriminator is committed;
      * ``chunks.db`` physically exists;
      * NO manifest exists (a manifest means a real migration DID happen
        here, so the normal verified/resume path must govern);
      * NO top-level authoritative ``vector_count`` in
        ``collection_meta.json`` (only the migration engine writes it, so
        its presence proves a migration ran here -- which makes a MISSING
        manifest a LOST manifest, not a native build);
      * ZERO legacy ``vector_*.json`` files remain anywhere beneath the
        collection. This is the load-bearing condition: a crashed migration
        (flag flipped, legacy files still on disk, manifest absent) is a
        genuine destructive-decision situation and must keep failing loudly.
    """
    if resolve_chunk_layout(collection_dir) != ChunkLayout.CHUNKS_DB:
        return False
    if not (collection_dir / CHUNKS_DB_FILENAME).is_file():
        return False
    if _content_manifest_path(collection_dir).exists():
        return False
    if _read_authoritative_vector_count(collection_dir) is not None:
        # Only the migration engine writes collection_meta.json's top-level
        # authoritative `vector_count`. Its presence PROVES a migration ran
        # here, so an absent manifest means the manifest was LOST (tamper /
        # corruption), not that it never existed -- that must keep failing
        # loudly instead of being mistaken for a native build.
        return False
    return next(collection_dir.rglob("vector_*.json"), None) is None


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
    resulting file is still byte-for-byte a standard JSON object.

    Bug #1486 Critical Finding 2: writes the versioned, self-validating
    envelope (``version``/``records``/``expected_count``/
    ``root_digest``) rather than a bare flat dict -- ``expected_count``
    and ``root_digest`` are only known once every entry has been
    streamed, so they are written as the LAST top-level keys (JSON
    object key order is semantically irrelevant); the O(1)-memory
    XOR-fold (:func:`_fold_manifest_entry`) computes the whole-key-set
    root digest without ever holding the full (point_id, digest) pair
    set in memory, preserving finding #8's bounded-memory property.
    """
    manifest_path = _content_manifest_path(collection_dir)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(collection_dir), suffix=".tmp")
    fd_owned = False
    try:
        try:
            tmp_f = os.fdopen(tmp_fd, "w")
            fd_owned = True
            with tmp_f:
                tmp_f.write(f'{{"version": {_MANIFEST_SCHEMA_VERSION}, "records": {{')
                accumulator = _empty_fold_accumulator()
                entry_count = 0
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
                        accumulator = _fold_manifest_entry(
                            accumulator, point_id, digest
                        )
                        entry_count += 1
                tmp_f.write("}, ")
                tmp_f.write(f'"expected_count": {entry_count}, ')
                tmp_f.write(f'"root_digest": {json.dumps(accumulator.hex())}')
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


def _write_authoritative_vector_count(collection_dir: Path, count: int) -> None:
    """Bug #1486 Critical Finding 2: durably record ``count`` into
    ``collection_meta.json``'s ``vector_count`` field -- an INDEPENDENT
    authoritative source (a separate file, written at a different time
    than the manifest) the manifest's own self-declared
    ``expected_count`` can be cross-checked against on a later resume.
    Closes the confirmed incident where an internally self-consistent
    but entirely wrong (empty) manifest was trusted blindly, since
    self-consistency alone can never detect "the whole manifest is
    uniformly wrong". Read-modify-write, atomic + durable (reuses
    :func:`_atomic_write_json`'s exact pattern).

    Raises:
        ConsolidationVerificationError: ``collection_meta.json`` could
            not be read/parsed, or is not a JSON object. The
            discriminator has not been written yet at this call site
            (called from the fresh path only), so this failure simply
            leaves the collection safely SHARDED_JSON, retryable.
    """
    meta_path = collection_dir / "collection_meta.json"
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConsolidationVerificationError(
            f"Migration refused for {collection_dir}: could not read "
            f"{meta_path} to durably record the authoritative "
            f"{_VECTOR_COUNT_META_KEY} cross-check field ({exc}) -- "
            f"refusing to proceed without it"
        ) from exc
    if not isinstance(meta, dict):
        raise ConsolidationVerificationError(
            f"Migration refused for {collection_dir}: {meta_path} does "
            f"not contain a JSON object -- cannot record the "
            f"authoritative {_VECTOR_COUNT_META_KEY} cross-check field"
        )
    meta[_VECTOR_COUNT_META_KEY] = count
    _atomic_write_json(meta_path, meta)


def _read_authoritative_vector_count(collection_dir: Path) -> Optional[int]:
    """Bug #1486 Critical Finding 2: read the independent authoritative
    ``vector_count`` cross-check field, or None if
    ``collection_meta.json`` is unreadable/malformed or the field is
    absent/non-integer -- a collection migrated by a version of this
    code before this field existed simply has nothing to cross-check
    against (degrades to self-validation alone, never fails solely for
    the field's absence)."""
    meta_path = collection_dir / "collection_meta.json"
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    value = meta.get(_VECTOR_COUNT_META_KEY)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _parse_manifest_json(collection_dir: Path) -> Any:
    """Bug #1486 Critical Finding 2: read+parse the manifest file's raw
    JSON, or raise :class:`UnrecoverableConsolidationCorruptionError`
    immediately -- an unreadable/missing/malformed manifest can never
    be trusted for a destructive decision."""
    manifest_path = _content_manifest_path(collection_dir)
    try:
        with open(manifest_path) as f:
            return json.load(f)
    except FileNotFoundError:
        raise UnrecoverableConsolidationCorruptionError(
            f"Resume-cleanup refused for {collection_dir}: the "
            f"content-integrity manifest is missing entirely (the "
            f"chunks_db discriminator is set, so a manifest MUST exist) "
            f"-- refusing to treat this collection as verified without it"
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise UnrecoverableConsolidationCorruptionError(
            f"Resume-cleanup refused for {collection_dir}: the "
            f"content-integrity manifest at {manifest_path} could not "
            f"be read/parsed ({exc}) -- refusing to trust an unreadable "
            f"manifest for a destructive decision"
        ) from exc


def _validate_manifest_envelope(
    collection_dir: Path, raw: Any
) -> "tuple[Dict[str, Any], int, str]":
    """Bug #1486 Critical Finding 2: structural self-validation of the
    versioned envelope -- required keys present, version matches,
    ``records`` is an object, ``expected_count`` is an int equal to
    ``len(records)``, ``root_digest`` is a string. Returns
    ``(records, expected_count, root_digest)`` on success; raises
    :class:`UnrecoverableConsolidationCorruptionError` otherwise."""
    if not isinstance(raw, dict) or any(
        key not in raw for key in _REQUIRED_MANIFEST_ENVELOPE_KEYS
    ):
        raise UnrecoverableConsolidationCorruptionError(
            f"Resume-cleanup refused for {collection_dir}: the "
            f"content-integrity manifest is malformed (missing required "
            f"envelope key(s) {_REQUIRED_MANIFEST_ENVELOPE_KEYS}) -- "
            f"refusing to trust it"
        )
    version = raw["version"]
    records = raw["records"]
    expected_count = raw["expected_count"]
    root_digest = raw["root_digest"]

    if version != _MANIFEST_SCHEMA_VERSION:
        raise UnrecoverableConsolidationCorruptionError(
            f"Resume-cleanup refused for {collection_dir}: the "
            f"content-integrity manifest's version {version!r} does not "
            f"match the expected {_MANIFEST_SCHEMA_VERSION!r}"
        )
    if not isinstance(records, dict):
        raise UnrecoverableConsolidationCorruptionError(
            f"Resume-cleanup refused for {collection_dir}: the "
            f"content-integrity manifest's 'records' field is not an "
            f"object"
        )
    if not isinstance(expected_count, int) or isinstance(expected_count, bool):
        raise UnrecoverableConsolidationCorruptionError(
            f"Resume-cleanup refused for {collection_dir}: the "
            f"content-integrity manifest's 'expected_count' is not an "
            f"integer"
        )
    if len(records) != expected_count:
        raise UnrecoverableConsolidationCorruptionError(
            f"Resume-cleanup refused for {collection_dir}: the "
            f"content-integrity manifest declares expected_count="
            f"{expected_count} but actually contains {len(records)} "
            f"record(s) -- truncated or tampered manifest, refusing to "
            f"trust it"
        )
    if not isinstance(root_digest, str):
        raise UnrecoverableConsolidationCorruptionError(
            f"Resume-cleanup refused for {collection_dir}: the "
            f"content-integrity manifest's 'root_digest' is not a string"
        )
    return records, expected_count, root_digest


def _verify_manifest_root_digest(
    collection_dir: Path, records: Dict[str, Any], root_digest: str
) -> None:
    """Bug #1486 Critical Finding 2: recompute the whole-key-set
    XOR-fold digest over ``records`` and compare against the persisted
    ``root_digest`` -- catches a manifest whose records were tampered/
    truncated in a way that preserved the count but not the actual
    content. Raises :class:`UnrecoverableConsolidationCorruptionError`
    on any digest value that is not a string, or on a mismatch."""
    accumulator = _empty_fold_accumulator()
    for point_id, digest in records.items():
        if not isinstance(digest, str):
            raise UnrecoverableConsolidationCorruptionError(
                f"Resume-cleanup refused for {collection_dir}: manifest "
                f"record {point_id!r} has a non-string digest value"
            )
        accumulator = _fold_manifest_entry(accumulator, point_id, digest)
    recomputed_root_digest = accumulator.hex()
    if recomputed_root_digest != root_digest:
        raise UnrecoverableConsolidationCorruptionError(
            f"Resume-cleanup refused for {collection_dir}: the "
            f"content-integrity manifest's root_digest mismatch "
            f"(recomputed {recomputed_root_digest!r} != stored "
            f"{root_digest!r}) -- the manifest's own record set has "
            f"been tampered with or corrupted; refusing to trust it"
        )


def _cross_check_manifest_count(
    collection_dir: Path,
    expected_count: int,
    require_authoritative_count: bool = False,
) -> None:
    """Bug #1486 Critical Finding 2: cross-check the manifest's own
    ``expected_count`` against the INDEPENDENT authoritative
    ``vector_count`` recorded in ``collection_meta.json`` (when
    present) -- closes the confirmed incident where an entirely empty
    manifest was completely self-consistent yet catastrophically wrong.
    A collection migrated before this cross-check field existed simply
    has nothing to disagree with (never fails solely for absence).

    Bug #1486 Finding B: ``require_authoritative_count`` hardens the
    corrupt-chunks.db resume path against the ONE self-validation blind
    spot the caller's recoverability logic cannot cover -- an EMPTY
    manifest. On that path
    ``unrecoverable_ids = manifest_keys - still_present_keys`` is the
    safety net, and it is structurally VACUOUS when ``manifest_keys`` is
    empty: an empty manifest bypasses the whole check, so a
    corrupt-but-substantial chunks.db would be silently "rebuilt" as
    empty (the confirmed ``evolution`` incident). A NON-empty manifest
    is still checked against legacy presence (recoverable when every
    record's source survives, unrecoverable otherwise), so it needs no
    extra guard. Therefore, when this flag is set, the manifest is empty
    (``expected_count == 0``), AND no independent authoritative
    ``vector_count`` exists to PROVE the collection was genuinely empty,
    this raises :class:`UnrecoverableConsolidationCorruptionError`
    rather than trusting an unprovable "nothing was here". A genuinely
    empty collection that recorded ``vector_count == 0`` cross-checks
    cleanly and is unaffected. The healthy-chunks.db call site passes
    ``False`` (the readable vectors are the independent proof via
    per-row digest verification, and an empty manifest there is already
    caught by that path's row-SET mismatch check)."""
    authoritative_count = _read_authoritative_vector_count(collection_dir)
    if authoritative_count is None:
        if require_authoritative_count and expected_count == 0:
            raise UnrecoverableConsolidationCorruptionError(
                f"Resume-cleanup refused for {collection_dir}: the "
                f"content-integrity manifest is EMPTY (expected_count=0) "
                f"and collection_meta.json carries NO independent "
                f"authoritative {_VECTOR_COUNT_META_KEY} to prove the "
                f"collection was genuinely empty -- on a "
                f"discriminator-committed collection whose "
                f"{CHUNKS_DB_FILENAME} has already failed the "
                f"durability/integrity gate, an empty manifest bypasses "
                f"the recoverability safety net entirely and cannot be "
                f"trusted to mean 'nothing was here'; refusing to "
                f"silently rebuild an empty index -- UNRECOVERABLE, "
                f"requires manual intervention"
            )
        return
    if authoritative_count != expected_count:
        raise UnrecoverableConsolidationCorruptionError(
            f"Resume-cleanup refused for {collection_dir}: the "
            f"content-integrity manifest's expected_count="
            f"{expected_count} disagrees with the independent "
            f"authoritative {_VECTOR_COUNT_META_KEY}="
            f"{authoritative_count} recorded in collection_meta.json -- "
            f"refusing to trust a self-consistent-but-independently-"
            f"contradicted manifest"
        )


def _is_legacy_flat_manifest(raw: Any) -> bool:
    """Bug #1486 Finding D: detect the ROUND-1 flat manifest shape -- a
    bare ``{point_id: digest}`` JSON object written before the versioned
    self-validating envelope (``version``/``records``/``expected_count``/
    ``root_digest``) existed. A non-empty ``dict`` that carries NONE of
    the envelope keys and whose every value is a string (a hex digest)
    is a legacy flat manifest.

    An EMPTY ``{}`` is deliberately NOT treated as flat -- it is
    ambiguous (indistinguishable from a truncated/uniformly-wrong
    manifest) and must fall through to the envelope validator, which
    rejects it as malformed. This keeps the confirmed empty-manifest
    data-loss incident unrecoverable rather than silently "upgradeable".
    """
    if not isinstance(raw, dict) or not raw:
        return False
    if any(key in raw for key in _REQUIRED_MANIFEST_ENVELOPE_KEYS):
        return False
    return all(isinstance(value, str) for value in raw.values())


def _extract_manifest_flat_view(raw: Any) -> Optional[Dict[str, str]]:
    """Issue #1503: normalize EITHER manifest shape into a plain
    ``{point_id: digest}`` view, for the subset-upgrade check below.

    Two cases:
      1. A legacy ROUND-1 flat manifest (:func:`_is_legacy_flat_manifest`)
         -- returned as-is.
      2. An already-ENVELOPE-shaped manifest whose ``records`` field is a
         non-empty ``dict`` of ``str -> str`` -- returned as that
         ``records`` dict, WITHOUT requiring the other envelope keys
         (``version``/``expected_count``/``root_digest``) to be
         internally self-consistent yet. That is deliberately not
         checked here: the whole point of the subset-upgrade path is to
         independently re-derive everything from live chunks.db rather
         than trust the envelope's own say-so.

    Returns ``None`` if ``raw`` matches neither shape (e.g. a malformed
    envelope missing ``records`` entirely, or a genuinely empty ``{}``)
    -- the caller falls through to the strict envelope pipeline.
    """
    if _is_legacy_flat_manifest(raw):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, dict):
        records = raw.get("records")
        if (
            isinstance(records, dict)
            # Deliberate: an EMPTY manifest ({} / empty records) is
            # NEVER treated as a vacuously-true subset here, even though
            # set() <= anything holds mathematically. An empty manifest
            # was the exact shape of a prior real data-loss incident
            # (Bug #1486's own motivating case) -- auto-trusting it would
            # reopen that vulnerability. This exclusion is intentional,
            # confirmed by independent dual review (Issue #1503) -- do
            # not "fix" it into accepting an empty manifest.
            and records
            and all(isinstance(v, str) for v in records.values())
        ):
            return {str(k): str(v) for k, v in records.items()}
    return None


def _attempt_manifest_subset_upgrade(
    collection_dir: Path,
    chunks_db_path: Path,
    flat_records: Dict[str, str],
    *,
    allow_write: bool = True,
    read_only: bool = False,
) -> Optional[Dict[str, str]]:
    """Issue #1503: validate a flat-view manifest (legacy flat, or an
    already-envelope-shaped-but-STALE manifest normalized via
    :func:`_extract_manifest_flat_view`) against a HEALTHY chunks.db as
    a SUBSET -- not the pre-#1503 exact bijection -- and, only if every
    covered entry checks out, upgrade/regenerate it to cover the FULL
    current live id set.

    A manifest's digest values are independently recomputable from the
    live chunks.db row (:func:`_compute_record_content_digest`), so
    there is nothing to trust blindly. Accepted iff:
      (a) EVERY manifest key has a matching point_id in chunks.db's real
          ``all_point_ids()`` (the manifest's key set is a SUBSET of the
          live id set -- never the reverse: a manifest key with no live
          backing at all is a phantom key and is ALWAYS a hard refusal);
      (b) EVERY one of those covered entries' stored digest EXACTLY
          matches the digest freshly recomputed from the live row.

    Ids in chunks.db that have NO manifest entry (the routine "an
    ordinary refresh added rows since the manifest was last written"
    case) are trusted by definition once every manifested entry checks
    out -- there is no other source of truth to check them against, and
    that is the intended design (Issue #1503).

    On ANY violation of (a) or (b), or if chunks.db cannot be opened/
    read (corrupt), returns ``None`` -- the caller falls through to the
    strict envelope validator, which stays fail-closed exactly as
    before. This call site is only ever reached AFTER chunks.db has
    passed the durability/integrity gate, so a read error here means
    genuine corruption, not a transient.

    On success, rewrites the manifest via :func:`_write_content_manifest`
    to cover the FULL current live id set (not merely the old manifest's
    keys) -- closing the staleness permanently -- and records the
    independent authoritative ``vector_count`` via
    :func:`_write_authoritative_vector_count`, ONLY when ``allow_write``
    is True AND ``read_only`` is NOT True (the genuine migration path,
    which holds the cluster-wide repo write lock; the read-only
    completeness oracle passes ``allow_write=False``/``read_only=True``
    so it can validate without mutating anything -- closing both the
    read-only-contract violation and a cross-node TOCTOU). Requiring
    BOTH conditions (rather than ``allow_write`` alone) is defense-in-
    depth against a hypothetical future caller passing the contradictory
    ``read_only=True`` + ``allow_write=True`` combination -- the primary
    guard against that contradiction lives in
    :func:`_load_verified_manifest`, which raises immediately rather
    than reaching this function at all (Issue #1503 dual-review
    Finding 1). Returns the FULL per-live-id digest dict either way, so
    downstream exact-set-equality logic keeps working unmodified once
    the staleness has been closed.

    Raises:
        ConsolidationVerificationError: propagated unchanged when the
            manifest-rewrite write step itself raises one (e.g. a
            missing live row on a fresh reopen -- see
            :func:`_write_content_manifest`).
        UnrecoverableConsolidationCorruptionError: propagated unchanged
            for the same reason.
        ConsolidationDurabilityError: raised (chained from the
            original) when the write step raises any OTHER exception
            (e.g. a real ``OSError``/``PermissionError`` from disk-full
            or a read-only filesystem). Issue #1503 dual-review
            Finding 2: this is DELIBERATELY distinct from this
            function's own ``None`` return (a validation DECLINE) -- a
            write failure after validation already passed must never be
            conflated with "not upgradable", since
            :func:`_load_verified_manifest` treats only a ``None``
            return as license to fall back to the original
            strict-pipeline failure. A write failure must instead
            propagate as its own retryable error.
    """
    try:
        # Bug #1486 Defect A: the read-only completeness oracle passes
        # read_only=True so this inspection opens the store IMMUTABLE
        # (never the mutable default, which CREATES a missing chunks.db as
        # a side effect of a pure predicate).
        with ChunkStore(chunks_db_path, immutable=read_only) as store:
            actual_ids = set(store.all_point_ids())
            if not set(flat_records.keys()) <= actual_ids:
                # A manifest key with NO live backing at all -- either the
                # manifest is lying, or a row was silently deleted. Always
                # a hard refusal, never acceptable (Issue #1503).
                return None
            result: Dict[str, str] = {}
            for point_id in actual_ids:
                stored = store.read(point_id)
                if stored is None:
                    return None
                digest = _compute_record_content_digest(stored)
                if point_id in flat_records and digest != flat_records[point_id]:
                    # Genuine corruption/tampering of an already-covered
                    # entry -- never tolerated, distinct from mere
                    # staleness.
                    return None
                result[point_id] = digest
    except (sqlite3.Error, OSError):
        return None

    # Validation passed against a healthy chunks.db -- see
    # _attempt_manifest_subset_upgrade's docstring for the allow_write/
    # read_only read-only-oracle-vs-migration-path rationale (identical
    # to Bug #1486 Codex Finding #3's existing contract). Issue #1503
    # dual-review Finding 1: requires BOTH conditions, never allow_write
    # alone -- defense-in-depth against a contradictory caller (the
    # primary guard is in _load_verified_manifest, which never reaches
    # this function at all with a contradictory combination).
    if allow_write and not read_only:
        try:
            _write_content_manifest(collection_dir, chunks_db_path, actual_ids)
            _write_authoritative_vector_count(collection_dir, len(actual_ids))
        except (
            ConsolidationVerificationError,
            UnrecoverableConsolidationCorruptionError,
        ):
            # Already a typed, accurate failure signal -- re-raise as-is
            # (mirrors _write_manifest_and_count_or_clean's exact
            # convention).
            raise
        except Exception as exc:
            # Issue #1503 dual-review Finding 2 (Codex HIGH #7 + Claude
            # MEDIUM): a genuine write/IO failure (e.g. disk-full,
            # read-only filesystem) after validation already succeeded
            # must propagate as its OWN distinct, retryable error --
            # never returned as None (which _load_verified_manifest
            # would misinterpret as "not upgradable" and silently fall
            # back to re-raising the ORIGINAL strict-pipeline failure,
            # permanently branding a perfectly-recoverable collection
            # unrecoverable).
            raise ConsolidationDurabilityError(
                f"Manifest subset-upgrade write failed for {collection_dir} "
                f"after validation against a healthy {CHUNKS_DB_FILENAME} "
                f"already succeeded ({type(exc).__name__}: {exc}) -- this "
                f"is a transient write/IO failure, not a corruption "
                f"verdict; safe to retry."
            ) from exc
    return result


def _load_verified_manifest(
    collection_dir: Path,
    chunks_db_path: Path,
    *,
    require_authoritative_count: bool,
    allow_manifest_upgrade: bool = True,
    read_only: bool = False,
) -> Dict[str, str]:
    """Bug #1486 Critical Finding 2: the SOLE, fail-closed manifest
    reader authorized for any DESTRUCTIVE decision -- never
    :func:`_read_content_manifest` directly, which performs zero
    validation. Composes :func:`_parse_manifest_json` (read),
    :func:`_validate_manifest_envelope` (structural self-validation),
    :func:`_verify_manifest_root_digest` (whole-key-set digest self-
    validation), and :func:`_cross_check_manifest_count` (independent
    cross-check) -- each raises
    :class:`UnrecoverableConsolidationCorruptionError` on its own
    failure mode; this function never swallows or downgrades any of
    them.

    Issue #1503: the STRICT envelope pipeline is tried FIRST (cheap --
    self-validation against the manifest's own persisted fields, no
    chunks.db row reads) so the common already-consistent case (Story
    #1488's O(N) perf gate) never touches chunks.db content at all. ONLY
    when that raises :class:`UnrecoverableConsolidationCorruptionError`
    is EITHER manifest shape (legacy ROUND-1 flat, or an already-
    envelope-shaped-but-STALE manifest) normalized via
    :func:`_extract_manifest_flat_view` and offered to
    :func:`_attempt_manifest_subset_upgrade` (which DOES read every live
    chunks.db row -- an O(N) cost, but only paid when the cheap strict
    path has already proven a rewrite is actually needed) -- if its key
    set is a SUBSET of chunks.db's real ids and every covered digest
    matches, it is accepted and REGENERATED to cover the full live id
    set, and its validated records returned directly. If it does not
    match (a phantom key, a digest mismatch, or the raw manifest matches
    neither normalizable shape at all), the ORIGINAL strict-pipeline
    failure is re-raised unchanged -- fail-closed exactly as before.

    Bug #1486 Finding B: ``require_authoritative_count`` is threaded to
    :func:`_cross_check_manifest_count` -- ``True`` on the corrupt-
    chunks.db resume path (manifest is the only surviving truth, so an
    un-cross-checkable manifest fails closed), ``False`` on the healthy-
    chunks.db path (the readable vectors are the independent proof).

    Returns the validated ``point_id -> digest`` records dict on
    success.

    Raises:
        ValueError: Issue #1503 dual-review Finding 1 (Codex CRITICAL):
            ``read_only=True`` and ``allow_manifest_upgrade=True`` is a
            contradictory combination -- a caller declaring a read-only
            contract must never simultaneously request write permission
            for the manifest-upgrade path. Raised immediately, before any
            manifest read/validation is attempted, rather than silently
            resolving the contradiction either way.
    """
    if read_only and allow_manifest_upgrade:
        raise ValueError(
            f"_load_verified_manifest: contradictory flags for "
            f"{collection_dir} -- read_only=True and "
            f"allow_manifest_upgrade=True were both passed. A read-only "
            f"caller must never also request manifest-upgrade write "
            f"permission; pass allow_manifest_upgrade=False for a "
            f"read-only call, or read_only=False for a write-permitted "
            f"call."
        )
    raw = _parse_manifest_json(collection_dir)
    try:
        records, expected_count, root_digest = _validate_manifest_envelope(
            collection_dir, raw
        )
        _verify_manifest_root_digest(collection_dir, records, root_digest)
        _cross_check_manifest_count(
            collection_dir, expected_count, require_authoritative_count
        )
        return {str(k): str(v) for k, v in records.items()}
    except UnrecoverableConsolidationCorruptionError as strict_error:
        flat_view = _extract_manifest_flat_view(raw)
        upgraded: Optional[Dict[str, str]] = None
        if flat_view is not None:
            try:
                upgraded = _attempt_manifest_subset_upgrade(
                    collection_dir,
                    chunks_db_path,
                    flat_view,
                    allow_write=allow_manifest_upgrade,
                    read_only=read_only,
                )
            except (
                ConsolidationVerificationError,
                UnrecoverableConsolidationCorruptionError,
            ):
                # Issue #1503 dual-review Finding 2: validation already
                # succeeded -- this is a genuine failure of the
                # manifest-rewrite WRITE step itself (e.g. a real
                # OSError/PermissionError translated to
                # ConsolidationDurabilityError, or an already-typed
                # failure re-raised as-is by
                # _attempt_manifest_subset_upgrade). This is a DISTINCT,
                # retryable failure -- it must propagate directly, never
                # be swallowed here and replaced with the ORIGINAL
                # strict-pipeline failure below, which would misdiagnose
                # a transient write failure as permanent unrecoverable
                # corruption.
                raise
            except Exception:
                # A genuinely unexpected failure attempting the subset
                # upgrade VALIDATION (never the write step -- that is
                # handled by the dedicated clause above) is logged
                # loudly, but the ORIGINAL strict-pipeline failure below
                # is what the caller sees -- this manifest is not
                # upgradable, and the original error already correctly
                # diagnoses why.
                logger.exception(
                    "_load_verified_manifest: subset-upgrade attempt "
                    "raised unexpectedly for %s -- treating the manifest "
                    "as not upgradable",
                    collection_dir,
                )
                upgraded = None
        if upgraded is not None:
            return upgraded
        # Does NOT match chunks.db as a subset (or is not normalizable at
        # all) -- re-raise the ORIGINAL strict-pipeline failure unchanged.
        raise strict_error


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


class ConsolidationDurabilityError(ConsolidationVerificationError):
    """Bug #1486 (CRITICAL, data loss): raised when the freshly-written
    chunks.db fails a fresh-connection ``PRAGMA integrity_check`` after
    an explicit durable fsync -- BEFORE the discriminator is flipped and
    BEFORE the legacy source is deleted.

    Confirmed production root cause this closes: the pre-existing
    read-back verification (``_write_and_verify_batch`` / the exact-set
    check) reads chunks.db through the SAME NFS client that had just
    written it -- on NFS, that read can report "correct" even though the
    write has not yet reached the NFS SERVER durably. By the time this
    is raised, the legacy source has NOT been touched and the
    discriminator has NOT been written -- the collection is left
    resolving as SHARDED_JSON, safe to retry.

    A subclass of :class:`ConsolidationVerificationError` so every
    existing catch of that type keeps behaving identically; this is a
    MORE SPECIFIC signal (durability/corruption, not a field-level
    mismatch) that callers may distinguish if they need to."""


class UnrecoverableConsolidationCorruptionError(Exception):
    """Bug #1486 (CRITICAL, data loss): raised when chunks.db is
    genuinely corrupt/unopenable AND at least one previously-migrated
    record's ONLY remaining source was inside that now-inaccessible
    store (its legacy ``vector_*.json`` is already gone).

    This is PERMANENT, UNRECOVERABLE data loss for those records --
    retrying the exact same migration attempt can never succeed (this is
    the confirmed real-world incident: the ``evolution`` golden repo was
    retried on every single scheduler tick, 481 consecutive failures,
    because nothing distinguished this terminal state from an ordinary
    retryable verification failure).

    Deliberately NOT a :class:`ConsolidationVerificationError` subclass:
    callers (``FleetMigrationScheduler``) must be able to distinguish
    this terminal state from an ordinary, retryable verification failure
    and surface it as a distinct, non-retried condition rather than
    looping forever."""


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
    #: Story #1460 AC1/AC2 rollout-safety gate: True iff a REAL legacy file
    #: existed on disk AND destructive cleanup (step 5) was WITHHELD this
    #: call because the caller passed ``deletion_authorized=False`` --
    #: physical-truth, mirroring ``old_files_deleted``: an already-clean
    #: collection (nothing left to delete) is False regardless of the
    #: flag, and cleanup that genuinely ran is always False too.
    deletion_gated: bool = False


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

    # Bug #1486 Round 3 Finding C: the migration WRITE connection must be
    # opened durable_synchronous=True, consistent with this whole bug's
    # durability principle -- the read-only fresh-reopen VERIFY store
    # immediately below stays at its default (unaffected).
    with ChunkStore(chunks_db_path, durable_synchronous=True) as write_store:
        write_store.write_batch(records)

    # Finding #3: a FRESH ChunkStore instance (new connection), never the
    # handle that just performed the write -- proves the batch is actually
    # durable on disk, not merely reflected in an in-process cache.
    with ChunkStore(chunks_db_path) as verify_store:
        for point_id, original in batch_originals.items():
            stored = verify_store.read(point_id)
            _verify_record_field_for_field(point_id, original, stored)


def _check_integrity_fresh_connection(chunks_db_path: Path) -> "tuple[bool, str]":
    """Bug #1486: open ``chunks_db_path`` via a genuinely NEW,
    READ-ONLY sqlite3 connection (never an already-open handle, and
    never a mode that could CREATE a missing file) and run
    ``PRAGMA integrity_check``. Returns ``(True, "ok")`` iff the check
    reports exactly one row equal to ``"ok"``; otherwise
    ``(False, <detail>)`` -- including when the file cannot even be
    opened/queried (a ``sqlite3.Error``, e.g. "file is not a database"),
    which is folded into the same False-with-detail contract rather than
    propagating a raw sqlite3 exception to callers.

    A missing file is checked explicitly and short-circuited BEFORE any
    connection attempt: a bare ``sqlite3.connect()`` in its default
    read-write-create mode would otherwise silently CREATE an empty
    database at a non-existent path and then report ``PRAGMA
    integrity_check`` as "ok" for that freshly-created empty file --
    exactly the kind of false-positive "healthy" signal this gate exists
    to prevent. The read-only URI is built via ``Path.resolve().as_uri()``
    (mirrors ``sqlite_chunk_store.py``'s ``chunk_store_has_real_data``)
    so path components containing URI-special characters are correctly
    percent-encoded.
    """
    path = Path(chunks_db_path)
    if not path.exists():
        return False, "file does not exist"

    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, str(exc)

    if len(rows) == 1 and rows[0][0] == "ok":
        return True, "ok"
    return False, "; ".join(str(row[0]) for row in rows)


def _best_effort_remove_untrusted_chunks_db(chunks_db_path: Path) -> None:
    """Bug #1486 Defect C: remove an untrusted/undurable chunks.db,
    best-effort. A removal failure is logged loudly but NEVER masks the
    original durability failure (the caller raises
    :class:`ConsolidationDurabilityError` regardless). The legacy source is
    left untouched, so even if this removal fails the next attempt still
    re-evaluates (and discards if still corrupt/stale) the leftover -- no
    data is ever lost by a failure here.
    """
    try:
        chunks_db_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as unlink_exc:
        logger.error(
            "consolidate_collection_in_place: could not remove the untrusted "
            "chunks.db %s after a durability/integrity failure (%s) -- it "
            "will be re-evaluated (and discarded if still corrupt/stale) on "
            "the next attempt; the legacy source remains authoritative",
            chunks_db_path,
            unlink_exc,
        )


def _force_durable_and_integrity_check(
    collection_dir: Path, chunks_db_path: Path
) -> None:
    """Bug #1486 (CRITICAL, data loss): force the freshly-written
    chunks.db to be DURABLE on the actual backing store, then re-verify
    its integrity via a genuinely FRESH connection -- BEFORE the
    discriminator is flipped and BEFORE the legacy source is deleted.

    Root cause this closes: the read-back verification that already ran
    (``_write_and_verify_batch`` / the exact-set check) reads chunks.db
    through the SAME NFS client that just wrote it -- on NFS, a fresh
    read through that client's page cache can report "correct" even
    though the write has not yet reached the NFS SERVER durably. This
    function (a) explicitly fsyncs chunks.db and its containing
    directory via :meth:`ChunkStore.flush_durable`, and (b) opens a
    genuinely NEW connection and runs ``PRAGMA integrity_check``, which
    must report exactly ``ok``.

    On ANY failure (integrity_check reports corruption, or the file
    cannot even be opened/queried): the just-written chunks.db is
    deleted (never left on disk as a misleading, possibly-corrupt
    artifact) and :class:`ConsolidationDurabilityError` is raised. The
    discriminator has not been written yet at this call site, so the
    collection is automatically left resolving as SHARDED_JSON, and the
    legacy source is never touched by this function -- safe to retry.
    """
    try:
        with ChunkStore(chunks_db_path, durable_synchronous=True) as store:
            store.flush_durable()
        ok, detail = _check_integrity_fresh_connection(chunks_db_path)
    except Exception as exc:
        # Bug #1486 Defect C: the forced-durable flush sequence
        # (open, commit, file-fsync, dir-fsync) can raise -- e.g. an
        # OSError on NFS -- and the pre-fix code let that leak RAW,
        # leaving the untrusted chunks.db on disk and handing the caller
        # an OSError instead of the typed ConsolidationDurabilityError.
        # On ANY exception: discard the untrusted chunks.db (best-effort),
        # leave the legacy source untouched (this function never touches
        # it), and raise the typed error chained from the original.
        logger.error(
            "consolidate_collection_in_place: the forced-durable flush/"
            "integrity sequence RAISED for %s (%s) -- discarding the "
            "untrusted chunks.db; the legacy source is left untouched and "
            "the collection remains SHARDED_JSON",
            chunks_db_path,
            exc,
        )
        _best_effort_remove_untrusted_chunks_db(chunks_db_path)
        raise ConsolidationDurabilityError(
            f"Durability/integrity flush failed for {collection_dir}: the "
            f"forced-durable flush or its fresh-connection integrity check "
            f"raised {exc!r} for {chunks_db_path} -- refusing to flip the "
            f"discriminator or delete the legacy source over a chunks.db "
            f"whose durability could not be established on the actual "
            f"backing store."
        ) from exc

    if ok:
        return

    logger.error(
        "consolidate_collection_in_place: durability/integrity "
        "re-verification FAILED for %s after an explicit durable fsync "
        "(%s) -- discarding the bad chunks.db; the legacy source is "
        "left untouched and the collection remains SHARDED_JSON",
        chunks_db_path,
        detail,
    )
    _best_effort_remove_untrusted_chunks_db(chunks_db_path)
    raise ConsolidationDurabilityError(
        f"Durability/integrity re-verification failed for {collection_dir}: "
        f"a FRESH connection's PRAGMA integrity_check reported {detail!r} "
        f"(expected 'ok') after an explicit durable fsync of {chunks_db_path} "
        f"-- refusing to flip the discriminator or delete the legacy "
        f"source over a chunks.db that is not durably correct on the "
        f"actual backing store."
    )


def _discard_corrupt_leftover_chunks_db(
    chunks_db_path: Path, expected_ids: "Optional[set]" = None
) -> None:
    """Bug #1486 Fix A item 4 + Defect B: a pre-existing chunks.db left
    over from an earlier INTERRUPTED fresh-path attempt (the discriminator
    was never flipped, so this collection's full legacy source is
    guaranteed complete and authoritative by construction of the caller's
    branch) is untrustworthy for RESUME in two ways, BOTH always safe to
    discard here (nothing is unrecoverable yet -- the legacy source is
    fully intact regardless):

    1. It may be structurally corrupt (fails the fresh-connection
       integrity gate).
    2. Bug #1486 Defect B: it may be perfectly HEALTHY but STALE -- its
       point-id set no longer matches the current authoritative legacy
       source, because a normal ``cidx index`` added/removed points
       between the interrupted attempt and this retry. The subsequent
       INSERT-OR-REPLACE write loop would leave the leftover's stale extra
       rows in place, and the exact-set verification would then raise on
       EVERY retry forever (non-idempotent). ``expected_ids`` (the current
       legacy id_map's key set) is compared against the leftover's actual
       point ids via a READ-ONLY immutable open; on any difference the
       leftover is discarded so the write loop rebuilds it cleanly.

    A no-op when ``chunks_db_path`` does not exist, or exists, is healthy,
    AND (when ``expected_ids`` is given) already holds exactly the current
    legacy id set -- a legitimate resume of an interrupted batch write
    (``write_batch`` is INSERT OR REPLACE, so leaving a healthy, matching
    partial file in place is safe and avoids redundant work).
    """
    if not chunks_db_path.exists():
        return
    ok, detail = _check_integrity_fresh_connection(chunks_db_path)
    if not ok:
        logger.warning(
            "consolidate_collection_in_place: discarding a corrupt leftover "
            "%s from an earlier interrupted attempt (%s) -- the legacy "
            "source is fully intact, so this is safe (Bug #1486)",
            chunks_db_path,
            detail,
        )
        chunks_db_path.unlink()
        return

    if expected_ids is None:
        return

    # Bug #1486 Defect B: healthy but possibly STALE -- compare its actual
    # id set against the current authoritative legacy source via a
    # read-only immutable open (never a mutable open that could touch the
    # file). A genuine corruption slipping past the integrity gate here is
    # still safe to discard (legacy is authoritative), so a read failure
    # discards too.
    try:
        with ChunkStore(chunks_db_path, immutable=True) as store:
            leftover_ids = set(store.all_point_ids())
    except (sqlite3.Error, OSError) as exc:
        logger.warning(
            "consolidate_collection_in_place: discarding a leftover %s whose "
            "point ids could not be read despite passing the integrity gate "
            "(%s) -- the legacy source is authoritative, so this is safe "
            "(Bug #1486 Defect B)",
            chunks_db_path,
            exc,
        )
        chunks_db_path.unlink()
        return

    if leftover_ids != set(expected_ids):
        logger.warning(
            "consolidate_collection_in_place: discarding a healthy but STALE "
            "leftover %s -- its %d point id(s) no longer match the current "
            "authoritative legacy source (%d id(s)); the legacy source "
            "changed since the interrupted attempt, so rebuilding cleanly "
            "(Bug #1486 Defect B)",
            chunks_db_path,
            len(leftover_ids),
            len(set(expected_ids)),
        )
        chunks_db_path.unlink()


def _handle_corrupt_chunks_db_on_resume(
    collection_dir: Path,
    chunks_db_path: Path,
    still_present_id_map: Dict[str, Path],
    failure_detail: str,
    *,
    allow_manifest_upgrade: bool = True,
) -> None:
    """Bug #1486 Critical Findings 1 & 2: on resume (discriminator
    already set), chunks.db failed the mandatory durability+integrity
    gate -- either it cannot be opened/queried at all (including a
    MISSING file), OR it opens fine (even answering simple queries
    plausibly) but a fresh-connection PRAGMA integrity_check found
    structural corruption (a real, empirically-reproduced "subtly
    corrupt but openable" SQLite class). Distinguishes a RECOVERABLE
    case (every previously-migrated record's legacy source is still on
    disk, per the FULLY self-validated + cross-checked manifest -- safe
    to discard and fully rebuild) from an UNRECOVERABLE one (at least
    one record's ONLY source was inside the now-untrustworthy
    chunks.db, and its legacy source is already gone) -- never
    conflates the two, and never silently loops forever on the
    unrecoverable case (this is the confirmed real-world incident: the
    ``evolution`` golden repo was retried on every single scheduler
    tick, 481 consecutive failures, for exactly this reason).

    Returns normally (having discarded and fully rebuilt chunks.db, with
    a durability+integrity re-check already applied) when recoverable.

    Raises:
        UnrecoverableConsolidationCorruptionError: propagated
            unchanged from :func:`_load_verified_manifest` (missing/
            malformed/incomplete/tampered/cross-check-mismatched
            manifest -- ALWAYS fail closed on a discriminator-committed
            collection, never assume recoverable), or when at least one
            manifested record has no remaining legacy source.
    """
    manifest = _load_verified_manifest(
        collection_dir,
        chunks_db_path,
        require_authoritative_count=True,
        allow_manifest_upgrade=allow_manifest_upgrade,
    )
    still_present_keys = set(still_present_id_map.keys())

    manifest_keys = set(manifest.keys())
    unrecoverable_ids = manifest_keys - still_present_keys
    if unrecoverable_ids:
        sample = sorted(unrecoverable_ids)[:_MAX_MISSING_SAMPLE_SIZE]
        raise UnrecoverableConsolidationCorruptionError(
            f"Resume-cleanup refused for {collection_dir}: "
            f"{CHUNKS_DB_FILENAME} failed the mandatory durability/"
            f"integrity gate ({failure_detail}) and "
            f"{len(unrecoverable_ids)} previously-migrated record(s) "
            f"have no remaining legacy source (sample: {sample}) -- "
            f"this data is UNRECOVERABLE and requires manual "
            f"intervention. This repo must be excluded from automatic "
            f"retry."
        )

    logger.warning(
        "consolidate_collection_in_place: %s failed the mandatory "
        "durability/integrity gate (%s) but every previously-migrated "
        "record's legacy source is still present on disk -- discarding "
        "the untrustworthy store and rebuilding it fully (Bug #1486)",
        chunks_db_path,
        failure_detail,
    )

    # Codex Finding 3 (HIGH): CLEAR the committed discriminator BEFORE the
    # destructive unlink+rebuild. If any rebuild step below then fails, the
    # collection is left resolving as SHARDED_JSON -- readers correctly fall
    # back to the fully-intact legacy source instead of a partial/missing
    # chunks.db that a still-committed CHUNKS_DB discriminator would point
    # at. The discriminator is re-committed only after the rebuild passes
    # its own durability+integrity gate.
    _clear_chunks_db_discriminator(collection_dir)

    try:
        chunks_db_path.unlink()
    except FileNotFoundError:
        pass

    rebuild_items = [(pid, still_present_id_map[pid]) for pid in still_present_keys]
    for batch in _batched(rebuild_items, _MIGRATION_BATCH_SIZE):
        _write_and_verify_batch(chunks_db_path, batch)
    _force_durable_and_integrity_check(collection_dir, chunks_db_path)

    # Rebuild proven durable+correct -- re-commit the discriminator so the
    # collection resolves as CHUNKS_DB again (the manifest + authoritative
    # vector_count from the original migration survive the clear above,
    # since only the chunks_db key was removed from collection_meta.json).
    write_chunks_db_discriminator(collection_dir)


def _open_gate_or_repair(
    collection_dir: Path,
    chunks_db_path: Path,
    still_present_id_map: Dict[str, Path],
    *,
    allow_manifest_upgrade: bool = True,
    read_only: bool = False,
) -> None:
    """Bug #1486 Critical Finding 1: run the SAME mandatory durability+
    integrity gate the fresh path enforces, BEFORE any resume-path
    inspection/decision ever touches chunks.db's rows. Detects BOTH
    "cannot open at all" (including a MISSING file -- Codex's own
    literal repro started from a resume that trusted the discriminator
    flag alone even though chunks.db was gone) and "opens fine, even
    answers simple queries plausibly, but a fresh PRAGMA integrity_check
    finds structural corruption" (a real, empirically-reproduced SQLite
    corruption class) as ONE unified signal -- never two divergent
    checks that could disagree about whether a chunks.db is
    trustworthy.

    Delegates the recoverable-vs-unrecoverable classification to
    :func:`_handle_corrupt_chunks_db_on_resume` when the gate fails.
    Returns normally (having repaired, if the failure was recoverable)
    when the gate ultimately passes.
    """
    ok, detail = _check_integrity_fresh_connection(chunks_db_path)
    if ok:
        return
    if read_only:
        # Bug #1486 Defect A: the read-only completeness oracle
        # (verify_collection_fully_migrated) runs lock-free and must NEVER
        # repair -- deleting/rebuilding/creating/flushing from a pure
        # predicate mutates storage and races real migrations on other
        # nodes. A failed gate simply means "not fully migrated": report it
        # (as a ConsolidationDurabilityError the oracle already catches)
        # WITHOUT any repair attempt.
        raise ConsolidationDurabilityError(
            f"Read-only completeness check for {collection_dir}: "
            f"{CHUNKS_DB_FILENAME} failed the fresh-connection integrity "
            f"gate ({detail}) -- reporting NOT fully migrated without any "
            f"repair (the read-only oracle must not mutate storage)."
        )
    _handle_corrupt_chunks_db_on_resume(
        collection_dir,
        chunks_db_path,
        still_present_id_map,
        detail,
        allow_manifest_upgrade=allow_manifest_upgrade,
    )


def _rebuild_missing_or_mismatched_still_present_records(
    collection_dir: Path,
    chunks_db_path: Path,
    still_present_id_map: Dict[str, Path],
    actual_ids: "set[str]",
) -> "tuple[set[str], bool]":
    """Codex Finding #2 (CRITICAL): for every legacy record STILL ON
    DISK (``still_present_id_map``), verify BOTH (a) an entry exists in
    chunks.db, AND (b) that entry's field content matches the legacy
    source exactly (a present-but-corrupted record is exactly as
    dangerous as a missing one). A still-present legacy record that is
    missing or field-mismatched is RECOVERABLE (the legacy source is
    right there): rebuilds exactly those records (reusing
    :func:`_write_and_verify_batch`) before refusing.

    Returns ``(actual_ids, rebuilt_any)`` where ``actual_ids`` is the
    (possibly updated, via a fresh re-scan after any rebuild) point-id
    set for the caller to continue verifying against, and ``rebuilt_any``
    is True iff at least one still-present record was actually rebuilt
    into chunks.db this call (NEW Finding round 3: the caller uses this
    to reconcile the persisted content manifest + authoritative
    vector_count with the now-changed chunks.db BEFORE any legacy file is
    deleted). Raises :class:`ConsolidationVerificationError` if a rebuild
    attempt still leaves a still-present record without a chunks.db
    counterpart.
    """
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
    if not to_rebuild:
        return actual_ids, False

    logger.warning(
        "_rebuild_missing_or_mismatched_still_present_records: %d "
        "still-present legacy record(s) missing/mismatched in %s "
        "(missing=%d, mismatched=%d) -- attempting rebuild from the "
        "still-intact legacy source before refusing cleanup",
        len(to_rebuild),
        chunks_db_path,
        len(missing),
        len(mismatched),
    )
    rebuild_items = [(pid, still_present_id_map[pid]) for pid in to_rebuild]
    # Reuses Finding #3's exact fresh-reopen write+verify mechanism -- a
    # failure here (e.g. a rebuild item's legacy JSON is itself
    # unreadable) propagates as ConsolidationVerificationError, an
    # unrecoverable hard failure.
    _write_and_verify_batch(chunks_db_path, rebuild_items)

    # Explicit LHS annotation works around a pre-existing project mypy
    # module-identity quirk (see _scan_or_fail_on_rejected_records's own
    # analogous workaround comment).
    recomputed_ids: "set[str]"
    with ChunkStore(chunks_db_path) as recheck_store:
        recomputed_ids = recheck_store.all_point_ids()
    still_missing = set(still_present_id_map.keys()) - recomputed_ids
    if still_missing:
        sample = sorted(still_missing)[:_MAX_MISSING_SAMPLE_SIZE]
        raise ConsolidationVerificationError(
            f"Resume-cleanup refused for {collection_dir}: rebuild "
            f"attempt still left {len(still_missing)} legacy record(s) "
            f"without a {CHUNKS_DB_FILENAME} counterpart (sample: "
            f"{sample}) -- refusing to delete legacy files that have "
            f"no verified consolidated counterpart"
        )
    return recomputed_ids, True


def _verify_unrecoverable_records_against_manifest(
    collection_dir: Path,
    chunks_db_path: Path,
    actual_ids: "set[str]",
    still_present_keys: "set[str]",
    *,
    allow_manifest_upgrade: bool = True,
    read_only: bool = False,
) -> None:
    """Codex CRITICAL finding (round 4/5), Bug #1486 Critical Finding 1:
    every id with NO still-present legacy source is UNRECOVERABLE if its
    stored content is wrong -- there is nothing left to rebuild from.
    Key-presence alone is not enough; verifies against the fail-closed,
    self-validating, cross-checked content manifest
    (:func:`_load_verified_manifest`).

    UNCONDITIONAL (never gated on "is there something currently present
    to flag"): a per-key digest check that only iterates over ids
    CURRENTLY in chunks.db has two blind spots -- (a) a row that
    vanishes ENTIRELY is simply absent from that iteration; (b) if the
    ONLY unrecoverable row that ever existed is deleted, the
    "unrecoverable" set computed purely from ``actual_ids`` collapses to
    empty too. This function therefore always requires AND validates
    the manifest: exact set-equality between "what the manifest
    promises is unrecoverable" and "what chunks.db actually has that's
    unrecoverable", THEN per-row digest verification for that set.

    Bug #1486 Critical Finding 1: both failure modes below raise
    :class:`UnrecoverableConsolidationCorruptionError` (NOT the generic
    :class:`ConsolidationVerificationError`) -- by construction, every
    record this function inspects has NO remaining legacy source, so
    ANY discrepancy found here is, by definition, unrecoverable data
    loss, never a bare retryable verification failure the scheduler
    would otherwise loop on forever.
    """
    actual_unrecoverable = actual_ids - still_present_keys
    manifest = _load_verified_manifest(
        collection_dir,
        chunks_db_path,
        require_authoritative_count=False,
        allow_manifest_upgrade=allow_manifest_upgrade,
        read_only=read_only,
    )

    manifest_keys = set(manifest.keys())
    manifest_unrecoverable = manifest_keys - still_present_keys
    if manifest_unrecoverable != actual_unrecoverable:
        missing_rows = manifest_unrecoverable - actual_unrecoverable
        extra_rows = actual_unrecoverable - manifest_unrecoverable
        sample_missing = sorted(missing_rows)[:_MAX_MISSING_SAMPLE_SIZE]
        sample_extra = sorted(extra_rows)[:_MAX_MISSING_SAMPLE_SIZE]
        raise UnrecoverableConsolidationCorruptionError(
            f"Resume-cleanup refused for {collection_dir}: manifest/"
            f"{CHUNKS_DB_FILENAME} row-SET mismatch for unrecoverable "
            f"records -- {len(missing_rows)} manifested record(s) are "
            f"missing from {CHUNKS_DB_FILENAME} (sample: {sample_missing}), "
            f"{len(extra_rows)} record(s) in {CHUNKS_DB_FILENAME} are not "
            f"accounted for in the manifest (sample: {sample_extra}) -- "
            f"these records have no remaining legacy source, so this is "
            f"UNRECOVERABLE and requires manual intervention"
        )

    corrupted: list = []
    if actual_unrecoverable:
        # Bug #1486 Defect A: the read-only oracle inspects row content via
        # an IMMUTABLE open (never the mutable default).
        with ChunkStore(chunks_db_path, immutable=read_only) as content_store:
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
        raise UnrecoverableConsolidationCorruptionError(
            f"Resume-cleanup refused for {collection_dir}: {len(corrupted)} "
            f"record(s) in {CHUNKS_DB_FILENAME} failed content-integrity "
            f"verification against the persisted manifest (sample: "
            f"{sample}) -- their legacy source is already gone, so this "
            f"corruption is UNRECOVERABLE and requires manual "
            f"intervention"
        )


def _persisted_manifest_record_keys(collection_dir: Path) -> "Optional[set[str]]":
    """NEW Finding (round 3): best-effort read of the persisted content
    manifest's record KEY-SET -- the envelope's ``records`` object keys, or a
    legacy ROUND-1 flat manifest's keys -- or ``None`` if the manifest is
    absent/unreadable/unrecognized.

    Tolerant and never-raising: used ONLY as a cheap "is a manifest rewrite
    needed" hint on the resume path (a divergence from chunks.db's actual
    point-ids forces a rewrite even when no rebuild happened THIS pass, e.g.
    after an earlier reconcile that failed mid-write). ``None`` conservatively
    forces a rewrite (never a false "already matches"). This is NOT a
    substitute for :func:`_load_verified_manifest` -- it performs zero
    integrity validation and must never gate a destructive decision on its
    own; the authoritative re-validation is done by
    :func:`_verify_unrecoverable_records_against_manifest` after the rewrite.
    """
    raw = _read_content_manifest(collection_dir)
    if not isinstance(raw, dict):
        return None
    records = raw.get("records")
    if isinstance(records, dict):
        return {str(k) for k in records.keys()}
    if _is_legacy_flat_manifest(raw):
        return {str(k) for k in raw.keys()}
    return None


def _reconcile_manifest_after_resume_rebuild(
    collection_dir: Path,
    chunks_db_path: Path,
    still_present_id_map: Dict[str, Path],
    rebuilt_any: bool,
    *,
    deletion_authorized: bool,
) -> None:
    """NEW Finding (round 3, HIGH): after a resume rebuilds one or more
    still-present legacy records into chunks.db (either the targeted
    :func:`_rebuild_missing_or_mismatched_still_present_records` repair, or
    the corrupt-DB full rebuild in :func:`_handle_corrupt_chunks_db_on_resume`),
    the persisted content manifest + authoritative ``vector_count`` still
    reflect the PRE-rebuild contents. Once cleanup then deletes every legacy
    source, a rebuilt record becomes "unrecoverable" yet is absent from (or
    carries a stale digest in) the stale manifest -- so the exact-set /
    per-row-digest check in
    :func:`_verify_unrecoverable_records_against_manifest` would reject the
    collection PERMANENTLY (``verify_collection_fully_migrated()`` False
    forever; the next automatic retry raising the terminal
    :class:`UnrecoverableConsolidationCorruptionError`).

    This rewrites the FULL manifest + authoritative count from the ACTUAL,
    now-verified chunks.db contents -- reusing :func:`_write_content_manifest`
    / :func:`_write_authoritative_vector_count`, which derive from chunks.db
    itself (never a hand-maintained delta) -- then re-validates the fresh
    envelope against the current chunks.db. It runs ONLY on the lock-held
    migration path, AFTER the final durability/integrity gate and BEFORE any
    legacy file is deleted, so the collection ends with a manifest that
    matches chunks.db exactly and completion is reachable.

    Codex Finding D (permanent completion-loss + irreplaceable legacy
    delete): the correct invariant is that BEFORE any legacy source is
    deleted on resume, the persisted manifest MUST be proven to match
    chunks.db EXACTLY. The prior cheap gate (rewrite only on ``rebuilt_any``
    OR a key-set divergence) let a matching-key-set-but-STALE-DIGEST manifest
    slip through: a retry after a crash between a row's rebuild-commit and
    the manifest rewrite sees ``rebuilt_any=False`` (chunks.db already holds
    the corrected row) and an UNCHANGED key-set, so it skipped the rewrite
    while the manifest still carried the PRE-rebuild (stale) digest for that
    row -- cleanup then deleted the (irreplaceable) legacy source over the
    stale manifest, and ``verify_collection_fully_migrated()`` failed on the
    resulting digest mismatch FOREVER (the next automatic retry raising the
    terminal :class:`UnrecoverableConsolidationCorruptionError`).

    Codex New-High perf gate (Story #1488): the unconditional re-derive is
    required for DESTRUCTIVE safety ONLY when legacy is ACTUALLY about to be
    deleted this pass -- ``deletion_authorized`` AND legacy still present --
    NOT merely "legacy present". This reconcile runs from inside
    :func:`_verify_chunks_db_before_resume_cleanup`, which the resume path
    calls BEFORE its ``deletion_authorized`` gate; keying the force on "legacy
    present" alone therefore performed the full O(N) re-derive (one
    ``ChunkStore.read`` + vector decompress + content hash PER POINT, an O(N)
    manifest write + fsync) on EVERY Story #1460 gated/bake-window resume
    (``deletion_authorized=False``), where NO legacy deletion ever happens --
    pure waste. So:

      * ``about_to_delete_legacy`` (``deletion_authorized`` AND legacy still
        present): re-derive + rewrite the FULL manifest + authoritative count
        from the ACTUAL chunks.db contents UNCONDITIONALLY, BEFORE the caller
        deletes any legacy source -- structurally eliminating the
        matching-key-set-but-stale-digest class (Finding D). Only ever an
        O(one manifest write) on the rare authorized-resume-with-legacy path.
      * gated resume (``deletion_authorized=False``) WITH legacy still present:
        nothing is about to be deleted, so the stale-manifest-before-delete
        hazard cannot bite -- SKIP the full re-derive ENTIRELY. The collection
        legitimately stays mixed-layout (bake window); the next AUTHORIZED
        pass re-derives+rewrites before it deletes, fully preserving Finding D.
      * NO legacy source remains this pass (either flag): nothing is about to
        be deleted, so a rewrite is a completion HINT only -- it still runs
        when a record was rebuilt this pass (``rebuilt_any``) OR the persisted
        manifest's key-set diverges from chunks.db's actual point-ids (a resume
        after an earlier reconcile that never landed); otherwise it is a no-op,
        and a clean, already-consistent resume never rewrites the manifest.

    Unlike the fresh path's :func:`_write_manifest_and_count_or_clean`, this
    NEVER removes chunks.db on a write failure: the discriminator is already
    committed and chunks.db is durably correct here, so a rewrite failure must
    propagate LOUDLY (Messi Rule #13) and abort BEFORE cleanup -- leaving the
    legacy source fully intact and the whole operation safely retryable --
    rather than destroying the authoritative store.
    """
    with ChunkStore(chunks_db_path) as store:
        actual_ids = store.all_point_ids()

    # Codex New-High perf gate + Finding D: the O(N) unconditional re-derive
    # is required for DESTRUCTIVE safety ONLY when legacy is ACTUALLY about to
    # be deleted this pass -- ``deletion_authorized`` AND legacy still present
    # -- never merely "legacy present". Distinguish the two states explicitly:
    #   * "legacy exists"                      -> may skip the rewrite
    #   * "legacy exists AND about to delete"  -> MUST rewrite+revalidate first
    legacy_still_present = bool(still_present_id_map)
    about_to_delete_legacy = deletion_authorized and legacy_still_present

    if not about_to_delete_legacy:
        if legacy_still_present:
            # Gated/bake-window resume (deletion_authorized=False, legacy
            # present): nothing is about to be deleted, so the Finding D
            # stale-manifest-before-delete hazard cannot bite. Skip the full
            # O(N) re-derive ENTIRELY -- the collection legitimately stays
            # mixed-layout and the next AUTHORIZED pass re-derives+rewrites
            # (below) before it deletes, fully preserving Finding D.
            return
        # No legacy remains this pass: nothing is about to be deleted, so a
        # rewrite is a completion HINT only -- run it when a record was rebuilt
        # this pass (``rebuilt_any``) OR the persisted manifest's key-set
        # diverges from chunks.db's actual point-ids (a resume after an earlier
        # reconcile that never landed); otherwise it is a no-op.
        if (
            not rebuilt_any
            and _persisted_manifest_record_keys(collection_dir) == actual_ids
        ):
            return

    # about_to_delete_legacy (authorized + legacy present), OR the no-legacy
    # completion-hint fired above: re-derive and rewrite the FULL manifest +
    # authoritative count from the ACTUAL chunks.db contents BEFORE the caller
    # deletes any legacy source -- structurally eliminating the
    # matching-key-set-but-stale-digest class the prior cheap gate let slip
    # through (Codex Finding D).
    _write_content_manifest(collection_dir, chunks_db_path, actual_ids)
    _write_authoritative_vector_count(collection_dir, len(actual_ids))

    # Re-validate the freshly-written envelope against the actual chunks.db
    # (the same unrecoverable-record gate the resume path already trusts) so
    # the rewrite can never itself enshrine a manifest inconsistent with the
    # store it was just derived from.
    _verify_unrecoverable_records_against_manifest(
        collection_dir,
        chunks_db_path,
        actual_ids,
        set(still_present_id_map.keys()),
    )


def _verify_chunks_db_before_resume_cleanup(
    collection_dir: Path,
    still_present_id_map: Dict[str, Path],
    *,
    allow_manifest_upgrade: bool = True,
    read_only: bool = False,
    deletion_authorized: bool = True,
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

    _open_gate_or_repair(
        collection_dir,
        chunks_db_path,
        still_present_id_map,
        allow_manifest_upgrade=allow_manifest_upgrade,
        read_only=read_only,
    )

    # Bug #1486 Defect A: the read-only oracle inspects via an IMMUTABLE
    # open (never the mutable default, which can touch journal/WAL files).
    with ChunkStore(chunks_db_path, immutable=read_only) as store:
        actual_ids = store.all_point_ids()

    rebuilt_any = False
    if not read_only:
        # A rebuild is a WRITE -- never performed on the read-only oracle
        # path. still_present_id_map is empty there anyway (the oracle
        # already proved zero legacy files remain), so this is also a
        # no-op semantically, but skipping it explicitly keeps read_only
        # provably mutation-free.
        actual_ids, rebuilt_any = _rebuild_missing_or_mismatched_still_present_records(
            collection_dir, chunks_db_path, still_present_id_map, actual_ids
        )

    # Codex CRITICAL finding (round 4/5), Bug #1486 Critical Finding 1:
    # every id with NO still-present legacy source is UNRECOVERABLE if
    # its stored content is wrong -- there is nothing left to rebuild
    # from. This ALWAYS runs (never gated on "is there something
    # currently present to flag" -- see _verify_unrecoverable_records_
    # against_manifest's own docstring for why an unconditional check is
    # required), using the fail-closed, self-validating, cross-checked
    # manifest loader.
    still_present_keys = set(still_present_id_map.keys())
    _verify_unrecoverable_records_against_manifest(
        collection_dir,
        chunks_db_path,
        actual_ids,
        still_present_keys,
        allow_manifest_upgrade=allow_manifest_upgrade,
        read_only=read_only,
    )

    if read_only:
        # Bug #1486 Defect A: the read-only completeness oracle stops here.
        # The final durable flush_durable()+re-check below is a WRITE
        # (fsync of chunks.db + its parent directory is a side effect on
        # NFS) and is pointless read-side: the gate above already ran a
        # fresh-connection integrity check, and read_only never rebuilt
        # anything that would need re-verifying.
        return

    # Bug #1486 Critical Finding 1: THE mandatory destructive-action
    # gate re-runs here too, immediately before the caller is authorized
    # to call _cleanup_old_sharded_files() -- covers "after any targeted
    # resume repair" (the rebuild above may have written fresh data that
    # itself needs a final durability+integrity confirmation, never
    # merely trusted because an earlier check happened to pass).
    #
    # Bug #1486 Round 3 Finding A (CRITICAL): flush_durable() MUST run
    # BEFORE this final integrity check -- the original ordering ran the
    # check first, which only ever validated pre-fsync/cached state.
    # Corruption that only manifests once the write is forced durable
    # (the exact NFS close-to-open race this whole bug is about) was
    # therefore invisible here, and the caller went on to authorize
    # legacy-file deletion over a chunks.db that was never actually
    # re-verified post-flush.
    with ChunkStore(chunks_db_path, durable_synchronous=True) as store:
        store.flush_durable()

    final_ok, final_detail = _check_integrity_fresh_connection(chunks_db_path)
    if not final_ok:
        # Never fall through to authorize cleanup on a bare durability
        # failure here -- route through the SAME recoverable-vs-
        # unrecoverable classification the initial resume-path gate
        # uses, so a corruption revealed only after this final durable
        # flush is never silently treated as clean.
        _handle_corrupt_chunks_db_on_resume(
            collection_dir,
            chunks_db_path,
            still_present_id_map,
            final_detail,
            allow_manifest_upgrade=allow_manifest_upgrade,
        )
        # If it returned (recoverable), it discarded and FULLY rebuilt
        # chunks.db from the legacy source -- a rebuild that likewise may have
        # left the persisted manifest stale (e.g. a since-added still-present
        # record). Force the manifest reconcile below.
        rebuilt_any = True

    # NEW Finding (round 3, HIGH): reconcile the persisted content manifest +
    # authoritative vector_count with the (possibly rebuilt) chunks.db BEFORE
    # the caller deletes any legacy source. Without this, a rebuilt record
    # becomes "unrecoverable" post-cleanup while absent from / stale in the
    # manifest, and _verify_unrecoverable_records_against_manifest would reject
    # the collection FOREVER. Runs after the durability/integrity gate above,
    # so it only ever rewrites over a chunks.db proven durably correct.
    _reconcile_manifest_after_resume_rebuild(
        collection_dir,
        chunks_db_path,
        still_present_id_map,
        rebuilt_any,
        deletion_authorized=deletion_authorized,
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


#: Bug #1562: how often (in deletions) the deletion loop below ticks its
#: progress_callback. This loop is FAST relative to the write phase
#: (~2181 files/sec on the real staging incident), so ticking on every
#: single deletion would be needless overhead for no visible benefit;
#: this interval still produces many ticks even for a large fleet repo
#: (343,561 files / 500 = ~687 ticks).
_DELETE_PROGRESS_TICK_INTERVAL = 500


def _cleanup_old_sharded_files(
    collection_dir: Path,
    verified_paths: "Iterable[Path]",
    progress_callback: Optional[ProgressCallback] = None,
) -> int:
    """AC3 step 5 / AC4 resume-cleanup: delete every VERIFIED legacy
    ``vector_*.json`` file individually, unlink the retired
    ``id_index.bin`` if present, then remove now-empty shard
    subdirectories. Idempotent -- safe to call on a collection that has
    already been fully cleaned up (no-op).

    Bug #1562: `progress_callback` (if given) is ticked with a LOCAL
    0-100 "deleting_legacy_files" phase value every
    `_DELETE_PROGRESS_TICK_INTERVAL` deletions, plus once more at the end
    -- so this fast-but-real phase is visibly distinguishable from the
    (much longer) write phase rather than silently invisible.

    Codex Finding 1(b) (CRITICAL, AC7 data loss): this function deletes
    ONLY the exact ``verified_paths`` captured in the consolidation's
    original ``id_map`` snapshot -- NEVER a blind fresh ``rglob`` of
    ``vector_*.json``. If any ``vector_*.json`` is present on disk that
    was NOT part of the verified snapshot (e.g. a concurrent foreground
    ``cidx index`` wrote a NEW legacy point after the scan), this function
    deletes NOTHING and raises :class:`ConsolidationCleanupError` --
    surfacing the anomaly for operator investigation rather than silently
    destroying UNVERIFIED data. The collection is already
    CHUNKS_DB-authoritative, so leaving an extra legacy file on disk is a
    harmless orphan, whereas deleting a point whose payload was never
    consolidated is unrecoverable data loss.

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
    verified = {Path(p).resolve() for p in verified_paths}
    total_to_delete = len(verified)

    # Codex Finding 1(b): any vector_*.json on disk that is NOT part of the
    # verified snapshot is UNVERIFIED data -- abort touching anything and
    # surface it, never delete it.
    found = {p.resolve() for p in collection_dir.rglob("vector_*.json")}
    unexpected = found - verified
    if unexpected:
        sample = sorted(str(p) for p in unexpected)[:_MAX_MISSING_SAMPLE_SIZE]
        raise ConsolidationCleanupError(
            f"Cleanup ANOMALY for {collection_dir}: {len(unexpected)} legacy "
            f"vector_*.json file(s) are present on disk that were NOT part of "
            f"the verified consolidation snapshot (sample: {sample}) -- "
            f"refusing to delete UNVERIFIED data (it may be a concurrently-"
            f"written point whose payload was never consolidated into "
            f"chunks.db). No file was deleted; chunks.db is already "
            f"authoritative and these files are left for investigation."
        )

    deleted = 0
    failed_paths: list = []
    for json_path in verified:
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
        if total_to_delete > 0 and deleted % _DELETE_PROGRESS_TICK_INTERVAL == 0:
            _emit_progress(
                progress_callback,
                int(100 * deleted / total_to_delete),
                phase="deleting_legacy_files",
                detail=f"{deleted}/{total_to_delete} legacy files deleted",
            )
    if total_to_delete > 0:
        _emit_progress(
            progress_callback,
            100,
            phase="deleting_legacy_files",
            detail=f"{deleted}/{total_to_delete} legacy files deleted",
        )

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


def _clear_chunks_db_discriminator(collection_dir: Path) -> None:
    """Codex Finding 3 (HIGH): atomically + durably REMOVE the ``chunks_db``
    discriminator key from ``collection_meta.json`` so the collection
    reverts to resolving as ``SHARDED_JSON`` (readers fall back to the
    intact legacy source).

    Used by the corrupt-chunks.db resume-repair path IMMEDIATELY BEFORE it
    unlinks + rebuilds the store: if the rebuild then fails, the collection
    is left SHARDED_JSON with its legacy source intact and retryable --
    never a committed ``CHUNKS_DB`` pointing at a partial/missing DB. Raises
    loudly (fail-closed, Messi Rule #13) on a read/parse failure so the
    caller aborts before any destructive step. Reuses
    :func:`_atomic_write_json`'s durable pattern (this file holds the
    load-bearing HNSW ``id_mapping``).
    """
    meta_path = collection_dir / _COLLECTION_META_FILENAME
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConsolidationVerificationError(
            f"Resume-repair refused for {collection_dir}: could not read "
            f"{meta_path} to CLEAR the chunks_db discriminator before the "
            f"destructive rebuild ({exc})"
        ) from exc
    if not isinstance(meta, dict):
        raise ConsolidationVerificationError(
            f"Resume-repair refused for {collection_dir}: {meta_path} does "
            f"not contain a JSON object -- cannot clear the discriminator"
        )
    if _CHUNKS_DB_DISCRIMINATOR_KEY in meta:
        del meta[_CHUNKS_DB_DISCRIMINATOR_KEY]
        _atomic_write_json(meta_path, meta)


def _write_verified_chunks_db(
    collection_dir: Path,
    chunks_db_path: Path,
    id_map: Dict[str, Path],
    progress_callback: Optional[ProgressCallback] = None,
) -> "set":
    """Codex Finding 4(a) inner build: discard any corrupt/stale leftover,
    create the store, write+verify every batch via a fresh reopen, then a
    global exact-set comparison. Returns ``expected_ids``. Raises the raw
    engine/sqlite errors as-is -- the typed-envelope translation belongs to
    :func:`_build_fresh_chunks_db_verified`.

    Bug #1562: this is the dominant long-running loop (the real staging
    incident's ~2h11m scan+write phase). `progress_callback`, if given, is
    ticked once per batch with a LOCAL 0-100 value reflecting
    records-written / total-records so far -- the only granular signal
    available for a multi-hour run, since the total record count is known
    up front from `id_map`.
    """
    # A leftover chunks.db from an earlier interrupted attempt may be corrupt
    # OR healthy-but-STALE -- discard either (safe: discriminator not yet set).
    _discard_corrupt_leftover_chunks_db(chunks_db_path, expected_ids=set(id_map.keys()))
    # Ensure chunks.db (schema) exists even when id_map is empty.
    ChunkStore(chunks_db_path, durable_synchronous=True).close()

    total_records = len(id_map)
    expected_ids: set = set()
    for batch in _batched(list(id_map.items()), _MIGRATION_BATCH_SIZE):
        _write_and_verify_batch(chunks_db_path, batch)
        expected_ids.update(point_id for point_id, _json_path in batch)
        if total_records > 0:
            written = len(expected_ids)
            _emit_progress(
                progress_callback,
                int(100 * written / total_records),
                phase="writing",
                detail=f"{written}/{total_records} records written",
            )

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
    return expected_ids


def _build_fresh_chunks_db_verified(
    collection_dir: Path,
    chunks_db_path: Path,
    id_map: Dict[str, Path],
    progress_callback: Optional[ProgressCallback] = None,
) -> "set":
    """Codex Finding 4(a) (HIGH): build+verify+force-durable a FRESH
    chunks.db from ``id_map`` inside a TYPED envelope covering the ENTIRE
    pre-discriminator lifecycle, so NO raw sqlite/open/PRAGMA/commit/fsync
    error (or an invalid-legacy-record error) ever escapes. Any failure
    removes the partial chunks.db (best-effort) and leaves the legacy
    source untouched (the discriminator is not written until after this
    returns). Returns ``expected_ids`` on success.

    Bug #1562: `progress_callback` (if given) is forwarded unchanged into
    :func:`_write_verified_chunks_db`, the only phase of this function
    with a granular records-processed/total signal to report.
    """
    try:
        expected_ids = _write_verified_chunks_db(
            collection_dir, chunks_db_path, id_map, progress_callback
        )
        _force_durable_and_integrity_check(collection_dir, chunks_db_path)
        return expected_ids
    except (ConsolidationVerificationError, UnrecoverableConsolidationCorruptionError):
        # Already typed (ConsolidationDurabilityError is a subclass caught
        # here too) -- these paths already leave a safe state.
        raise
    except (InvalidVectorError, NonFiniteVectorError) as exc:
        _best_effort_remove_untrusted_chunks_db(chunks_db_path)
        raise ConsolidationVerificationError(
            f"Migration refused for {collection_dir}: a legacy record could "
            f"not be consolidated into {chunks_db_path} -- "
            f"{type(exc).__name__}: {exc}. The partial chunks.db was removed; "
            f"the legacy source is untouched."
        ) from exc
    except Exception as exc:
        _best_effort_remove_untrusted_chunks_db(chunks_db_path)
        raise ConsolidationDurabilityError(
            f"Durability/build failure for {collection_dir}: constructing "
            f"{chunks_db_path} raised {type(exc).__name__}: {exc} -- the "
            f"partial chunks.db was removed and the legacy source is left "
            f"untouched (discriminator never written); safe to retry."
        ) from exc


def _write_manifest_and_count_or_clean(
    collection_dir: Path, chunks_db_path: Path, expected_ids: "set"
) -> None:
    """Codex Finding 4 (round 3, HIGH): extend the typed-failure envelope
    PAST the chunks.db BUILD to also cover the two remaining
    pre-discriminator writes -- the content-manifest write AND the
    authoritative-vector-count write. Previously a raw error here (e.g. an
    ``IsADirectoryError`` raised by ``_write_content_manifest``'s
    ``os.replace`` when the content-manifest path pre-exists as a DIRECTORY)
    escaped uncaught AND left the freshly-built chunks.db on disk, breaking
    the pre-discriminator atomic-or-clean invariant.

    On ANY failure in this span the freshly-built chunks.db is removed
    (best-effort), the legacy source is left untouched, and the
    discriminator is never committed (the collection stays SHARDED_JSON,
    retryable). A raw exception is translated to the typed
    :class:`ConsolidationDurabilityError` chained from the original; an
    already-typed failure (:class:`ConsolidationVerificationError` /
    :class:`UnrecoverableConsolidationCorruptionError`, e.g. a row missing
    from the fresh reopen, or an unreadable ``collection_meta.json``) is
    re-raised as-is -- but STILL only after removing the uncommitted
    chunks.db, so the whole pre-discriminator lifecycle is atomic-or-clean
    regardless of the failure's type.
    """
    try:
        _write_content_manifest(collection_dir, chunks_db_path, expected_ids)
        _write_authoritative_vector_count(collection_dir, len(expected_ids))
    except (
        ConsolidationVerificationError,
        UnrecoverableConsolidationCorruptionError,
    ):
        # Already a typed, accurate failure signal -- re-raise as-is, but
        # only AFTER discarding the uncommitted chunks.db so a retry
        # rebuilds it cleanly from the intact legacy source.
        _best_effort_remove_untrusted_chunks_db(chunks_db_path)
        raise
    except Exception as exc:
        _best_effort_remove_untrusted_chunks_db(chunks_db_path)
        raise ConsolidationDurabilityError(
            f"Durability failure for {collection_dir}: persisting the "
            f"pre-discriminator content manifest / authoritative "
            f"{_VECTOR_COUNT_META_KEY} raised {type(exc).__name__}: {exc} -- "
            f"the freshly-built chunks.db was removed and the legacy source "
            f"is left untouched (discriminator never written); safe to retry."
        ) from exc


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

    if _is_natively_built_chunks_db(collection_dir):
        # Built directly in the consolidated layout: no manifest can exist
        # and nothing was ever migrated, so the manifest-requiring resume
        # verifier below would reject a perfectly consolidated collection.
        # Still PROVE the store itself is intact, via the same
        # fresh-connection integrity check the resume path relies on.
        integrity_ok, _detail = _check_integrity_fresh_connection(
            collection_dir / CHUNKS_DB_FILENAME
        )
        return integrity_ok

    try:
        # Bug #1486 Codex Finding #3: this predicate is side-effect-free and
        # runs WITHOUT the repo write lock -- it must VALIDATE a legacy flat
        # manifest without performing the upgrade WRITE (which belongs only
        # to the lock-held migration path).
        _verify_chunks_db_before_resume_cleanup(
            collection_dir,
            still_present_id_map,
            allow_manifest_upgrade=False,
            read_only=True,
        )
    except (ConsolidationVerificationError, UnrecoverableConsolidationCorruptionError):
        # Bug #1486: a genuinely unrecoverable-corrupt collection is
        # obviously "not fully migrated" from this pure read-only
        # predicate's point of view -- it must NEVER propagate an
        # exception here (that contract is documented above and relied
        # upon by get_stats()/is_repo_already_migrated()). The higher-
        # level "this repo can never succeed via automatic retry"
        # handling belongs to the caller that actually RUNS the
        # migration (FleetMigrationScheduler), not this predicate.
        return False

    return True


def consolidate_collection_in_place(
    collection_dir: Union[str, Path],
    *,
    deletion_authorized: bool = True,
    # Story #1560 AC20/AC21: optional query-tracker quiescing/drain
    # threaded straight through to repair_duplicate_and_shifted_points.
    # Typed Any (mirroring collection_dedup_repair.py's own convention)
    # so this storage/shared module -- imported by the CLI/solo path --
    # never needs to import the concrete QueryTracker class; None (the
    # default) is a fail-open no-op for every pre-existing caller.
    query_tracker: Optional[Any] = None,
    refcount_key: Optional[str] = None,
    drain_max_wait_seconds: Optional[float] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> ConsolidationResult:
    """Consolidate ONE collection's sharded ``vector_*.json`` layout into a
    ``chunks.db`` written into the SAME directory, in place.

    Discriminator-driven and idempotent (AC4): if the collection already
    resolves to ``ChunkLayout.CHUNKS_DB`` on entry, steps 1-4 are NEVER
    redone -- only step 5's cleanup runs (a no-op if already clean).

    Bug #1562: ``progress_callback``, if given, is invoked with
    ``(progress: int, phase: Optional[str], detail: Optional[str])``
    checkpoints at each real phase transition (scan -> write -> verify ->
    flip -> delete legacy files), with genuine intra-phase ticks during
    the write and deletion loops (the two phases with a known total up
    front) -- this is what makes a genuinely advancing multi-hour
    migration distinguishable from a hang. ``None`` (the default) is a
    no-op, byte-identical to every pre-existing caller.

    Args:
        collection_dir: The collection directory to consolidate (a
            collection inside the target repo's MUTABLE BASE CLONE, per
            AC3 -- this function itself is location-agnostic and simply
            operates on whatever path it is given).
        deletion_authorized: Story #1460 AC1/AC2 rollout-safety gate.
            Defaults to True (Story #1458's original unconditional
            behavior -- byte-identical for every pre-existing caller that
            does not pass this parameter). When False, steps 1-4 (the
            PURE-ADDITION build/verify/durable-flip sequence) still run in
            full -- producing the AC1 "bake window" mixed-layout state,
            where both an old sharded-JSON-only reader (via the untouched
            legacy files) and a new dual-layout-aware reader (via the now
            -committed ``chunks.db``) see correct data simultaneously --
            but step 5's DESTRUCTIVE legacy-file deletion is withheld
            entirely. The real production caller
            (``run_fleet_migration_for_repo``, server/services/
            fleet_migration/orchestrator.py) resolves this from the
            operator-controlled, ``get_config_service()``-backed
            ``fleet_migration_config.enabled`` flag (default OFF) --
            never an env var.

    Returns:
        A :class:`ConsolidationResult` describing what happened.
        ``deletion_gated`` is True iff a REAL legacy file existed on disk
        AND was withheld this call because ``deletion_authorized`` was
        False -- an already-clean collection (nothing left to delete)
        reports ``deletion_gated=False`` regardless of the flag, mirroring
        ``old_files_deleted``'s own physical-truth contract.

    Raises:
        ConsolidationVerificationError: read-back verification (step 3)
            found a mismatch. The discriminator is NEVER set in this case.
    """
    collection_dir = Path(collection_dir)

    if resolve_chunk_layout(collection_dir) == ChunkLayout.CHUNKS_DB:
        if _is_natively_built_chunks_db(collection_dir):
            # Built directly in the consolidated layout (never migrated):
            # zero legacy files to clean up and no manifest can exist, so
            # the resume verification below would raise
            # UnrecoverableConsolidationCorruptionError over a perfectly
            # healthy collection. Nothing to migrate -- but still PROVE the
            # store is intact first, so a corrupt chunks.db is never
            # silently accepted as "already consolidated".
            integrity_ok, integrity_detail = _check_integrity_fresh_connection(
                collection_dir / CHUNKS_DB_FILENAME
            )
            if not integrity_ok:
                raise UnrecoverableConsolidationCorruptionError(
                    f"Refusing to treat {collection_dir} as consolidated: its "
                    f"chunks.db was built natively (no migration manifest "
                    f"exists) but does NOT open cleanly -- {integrity_detail}"
                )
            return ConsolidationResult(status="already_consolidated")
        # Resume path. Finding #4: fail loudly on any rejected legacy
        # record found during the fresh re-scan (never silently proceed).
        still_present_id_map = _scan_or_fail_on_rejected_records(collection_dir)
        # Bug #1562: a resume attempt can legitimately have a large number
        # of still-present legacy files (a crash between the durable flip
        # and cleanup completing) -- emit a checkpoint here so this is
        # visibly distinct from a fresh-path scan.
        _emit_progress(
            progress_callback,
            50,
            phase="resuming_verified_chunks_db",
            detail=f"{len(still_present_id_map)} legacy record(s) still present",
        )
        # Finding #2: never trust the discriminator flag alone -- reopen
        # chunks.db fresh and verify it actually has every still-present
        # legacy record before deleting anything.
        _verify_chunks_db_before_resume_cleanup(
            collection_dir,
            still_present_id_map,
            deletion_authorized=deletion_authorized,
        )
        # Codex cleanup finding 6: a stale Bug #1502 repair marker can
        # only ever survive from an OLDER, pre-remediation partial run --
        # repair_duplicate_and_shifted_points itself never runs on this
        # resume path. Clear it now that the resumed CHUNKS_DB state has
        # been verified consolidated, so it can never linger unmanaged
        # and confuse a future pass.
        if clear_stale_repair_marker(collection_dir):
            logger.info(
                "consolidate_collection_in_place: cleared a stale Bug "
                "#1502 repair marker for %s on the verified resume path",
                collection_dir,
            )
        if not deletion_authorized:
            # Physical-truth principle (Codex review finding): a REAL
            # deletion target must actually exist for this to count as
            # "gated" -- an already-clean collection (still_present_id_map
            # empty) has nothing to withhold, so deletion_gated must be
            # False even though deletion_authorized is False this call.
            had_legacy_files = bool(still_present_id_map)
            if had_legacy_files:
                logger.info(
                    "consolidate_collection_in_place: resume-path cleanup "
                    "WITHHELD for %s -- deletion_authorized=False (rollout "
                    "gate closed); legacy sharded files remain on disk",
                    collection_dir,
                )
            return ConsolidationResult(
                status="already_consolidated",
                old_files_deleted=0,
                deletion_gated=had_legacy_files,
            )
        # Bug #1562: rescale the deletion loop's own LOCAL 0-100 progress
        # into the remaining half of this resume pass, so a large resumed
        # cleanup (e.g. a crash right before the fresh path's own delete
        # step) is visibly advancing rather than silent.
        resume_delete_callback = _phase_scoped_callback(
            progress_callback, base=50, span=49, phase_prefix="deleting_legacy_files"
        )
        deleted = _cleanup_old_sharded_files(
            collection_dir,
            set(still_present_id_map.values()),
            resume_delete_callback,
        )
        return ConsolidationResult(
            status="already_consolidated", old_files_deleted=deleted
        )

    # Step 0 (Bug #1502): metadata-only dedup + canonical renumber repair,
    # BEFORE the scan below -- which would otherwise raise
    # DuplicateSourceIdError on any duplicate point_id left behind by the
    # (now-fixed) enumerate()-over-survivors chunk_index bug, or silently
    # consolidate stale, line-order-shifted labels. A collection with zero
    # duplicates and already-canonical labels is an identity transform
    # (no-op) here. Never run on the resume path above -- duplicates were
    # already resolved during the ORIGINAL fresh run that set the
    # discriminator, so the still-present legacy files being scanned
    # there are already dedup-clean by construction.
    try:
        repair_result = repair_duplicate_and_shifted_points(
            collection_dir,
            deletion_authorized=deletion_authorized,
            query_tracker=query_tracker,
            refcount_key=refcount_key,
            drain_max_wait_seconds=drain_max_wait_seconds,
        )
    except DedupRepairAmbiguousError as exc:
        # Unify with this module's own pre-existing, general-purpose
        # "refuse to flip, collection stays SHARDED_JSON" exception type
        # -- preserves the contract other callers (e.g. the malformed-
        # legacy-record test) already depend on. DuplicateSourceIdError
        # is deliberately left UNWRAPPED (its own, separately-preserved
        # contract -- see collection_dedup_repair.py's _plan_dedup).
        raise ConsolidationVerificationError(
            f"Migration refused for {collection_dir}: the Bug #1502 "
            f"dedup/renumber repair step could not safely proceed "
            f"({exc})"
        ) from exc

    # Story #1560 AC19: with the Story #1460 rollout gate CLOSED and
    # duplicate groups genuinely present, repair performed ZERO mutation
    # -- the collection still has an unresolvable/ambiguous point_id
    # population, so the scan below could not build an unambiguous
    # chunks.db even if we let it try. Return a DISTINCT, explicitly
    # retryable status IMMEDIATELY -- never fall through into steps 1-5,
    # and never report "consolidated"/"already_consolidated" (that would
    # be a false claim of success). This is deliberately a SEPARATE,
    # clearly-distinguishable outcome from "skipped_insufficient_disk"
    # (a different cause entirely, from the disk-headroom preflight
    # below) and from a clean no-duplicates pass (whose
    # repair_result.duplicate_groups == 0 never reaches here).
    if repair_result.deletion_gated:
        logger.warning(
            "consolidate_collection_in_place: %s has %d duplicate "
            "point_id group(s) but deletion_authorized=False (Story "
            "#1460 rollout gate closed) -- consolidation DEFERRED, no "
            "mutation performed. Will retry automatically once the gate "
            "opens.",
            collection_dir,
            repair_result.duplicate_groups,
        )
        return ConsolidationResult(
            status="dedup_deletion_gated",
            detail=(
                f"{repair_result.duplicate_groups} duplicate point_id "
                f"group(s) detected; deletion withheld pending "
                f"deletion_authorized=True"
            ),
            extra={"dedup_repair": repair_result},
        )

    if not repair_result.gate_passed:
        logger.info(
            "consolidate_collection_in_place: Bug #1502 repair passed "
            "%s through UNTOUCHED -- whole-collection identity gate "
            "rejected it (foreign/missing/self-inconsistent unique_key "
            "found on at least one record); migration proceeds exactly "
            "as it did before this repair existed.",
            collection_dir,
        )
    elif repair_result.duplicate_groups or repair_result.records_renumbered:
        # Story #1560: `records_before`/`collection_total` are computed
        # on the FILE-SCAN basis (every vector_*.json record found by
        # the raw rglob scan), NEVER id_index.bin's entry count -- on a
        # real production collection these can genuinely differ (a
        # measured staging example: id_index.bin held 343,357 entries
        # while the file scan found 343,604 records -- a ~204-record gap
        # of pre-existing, never-duplicated, never-indexed orphans,
        # unrelated to this repair). id_index.bin is consulted ONLY to
        # resolve which copy is the winner within an already-detected
        # duplicate group; it is never the base population count.
        logger.info(
            "consolidate_collection_in_place: Bug #1502/#1560 repair for "
            "%s -- %d duplicate group(s) (file-scan basis: "
            "records_before=%d, collection_total=%d), %d record(s) "
            "deleted (%d winner-kept groups, %d whole-group-deleted "
            "groups), %d record(s) renumbered, id_index_rebuilt=%s, "
            "hnsw_rebuilt=%s",
            collection_dir,
            repair_result.duplicate_groups,
            repair_result.records_before,
            repair_result.collection_total,
            repair_result.records_deleted,
            repair_result.winner_kept_groups,
            repair_result.whole_group_deleted_groups,
            repair_result.records_renumbered,
            repair_result.id_index_rebuilt,
            repair_result.hnsw_rebuilt,
        )

    # Step 1: side-effect-free scan -- trustworthy point_id -> json_path
    # map. Finding #4: fail loudly on any rejected record, never flip.
    id_map = _scan_or_fail_on_rejected_records(collection_dir)
    # Bug #1562: the scan itself has no internal progress signal (it is a
    # single call into IDIndexManager), so it is reported as one
    # before/after checkpoint rather than an intra-phase tick series.
    _emit_progress(
        progress_callback,
        _PROGRESS_AFTER_SCAN,
        phase="scanning_legacy_records",
        detail=f"{len(id_map)} legacy record(s) found",
    )

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
    # Bug #1562: the write+verify loop is the dominant long-running phase
    # (the real staging incident's ~2h11m scan+write phase) -- rescale its
    # own LOCAL 0-100 records-written/total-records ticks into this
    # repo's overall pass.
    write_callback = _phase_scoped_callback(
        progress_callback,
        base=_PROGRESS_WRITE_PHASE_BASE,
        span=_PROGRESS_WRITE_PHASE_SPAN,
        phase_prefix="writing_chunks_db",
    )
    # Bug #1486 + Codex Finding 4(a): build+verify+force-durable the whole
    # pre-discriminator chunks.db lifecycle inside ONE typed envelope --
    # discard any corrupt/stale leftover, create the store, batched
    # write+verify (bounded memory), a GLOBAL exact-set comparison, then the
    # forced-durable + fresh-connection integrity gate (which closes the NFS
    # close-to-open durability race). ANY failure removes the partial
    # chunks.db and leaves the legacy source untouched, raising a TYPED
    # error (never a raw sqlite/open/PRAGMA/fsync/invalid-record leak); the
    # discriminator is not written until this returns, so a failure always
    # leaves the collection resolving as SHARDED_JSON, safe to retry.
    expected_ids = _build_fresh_chunks_db_verified(
        collection_dir, chunks_db_path, id_map, write_callback
    )

    # Codex CRITICAL finding (round 4): persist the crash-durable content
    # manifest BEFORE the flip -- a set discriminator therefore always
    # guarantees the manifest exists (a crash between these lines leaves the
    # flag unset, so a retry safely redoes this whole fresh path rather than
    # resuming into cleanup with no manifest).
    #
    # Bug #1486 Critical Finding 2: this ALSO durably records the INDEPENDENT
    # authoritative vector_count cross-check field -- without it a later
    # resume's manifest self-validation has no external source to detect a
    # manifest that is entirely, uniformly wrong (e.g. genuinely empty) yet
    # still internally self-consistent.
    #
    # Codex Finding 4 (round 3): both writes run inside the SAME typed
    # atomic-or-clean envelope as the build above -- a raw failure here (e.g.
    # the manifest path pre-existing as a DIRECTORY) no longer escapes
    # untyped, and never leaves the freshly-built chunks.db behind.
    _write_manifest_and_count_or_clean(collection_dir, chunks_db_path, expected_ids)

    # Step 4: durable discriminator flip -- only after full verification.
    write_chunks_db_discriminator(collection_dir)
    # Bug #1562: the write phase is done and the collection is already
    # durably CHUNKS_DB at this point -- checkpoint before the (fast)
    # deletion phase starts.
    _emit_progress(
        progress_callback, _PROGRESS_AFTER_FINALIZE, phase="finalizing_chunks_db"
    )

    # Step 5: delete the old files individually -- gated by Story #1460's
    # rollout-safety flag. When withheld, the collection is left in the
    # deliberate AC1 "bake window" mixed-layout state: chunks.db is fully
    # built/verified/committed (a new dual-layout-aware reader already
    # sees ChunkLayout.CHUNKS_DB) while the legacy sharded files remain
    # physically present (an old/un-upgraded reader still finds them).
    if not deletion_authorized:
        # Physical-truth principle (Codex review finding): id_map is the
        # exact set of legacy files step 5 would otherwise delete -- an
        # empty id_map (a genuinely empty collection) means there is
        # nothing to withhold, so deletion_gated must be False even
        # though deletion_authorized is False this call.
        had_legacy_files = bool(id_map)
        if had_legacy_files:
            logger.info(
                "consolidate_collection_in_place: fresh-path cleanup "
                "WITHHELD for %s -- deletion_authorized=False (rollout "
                "gate closed); chunks.db is built+verified+committed but "
                "legacy sharded files remain on disk",
                collection_dir,
            )
        return ConsolidationResult(
            status="consolidated",
            records_written=len(id_map),
            old_files_deleted=0,
            deletion_gated=had_legacy_files,
        )

    # Bug #1562: rescale the deletion loop's own LOCAL 0-100 progress into
    # the final percentage points of this repo's overall pass.
    delete_callback = _phase_scoped_callback(
        progress_callback,
        base=_PROGRESS_DELETE_PHASE_BASE,
        span=_PROGRESS_DELETE_PHASE_SPAN,
        phase_prefix="deleting_legacy_files",
    )
    deleted = _cleanup_old_sharded_files(
        collection_dir, set(id_map.values()), delete_callback
    )

    return ConsolidationResult(
        status="consolidated",
        records_written=len(id_map),
        old_files_deleted=deleted,
    )
